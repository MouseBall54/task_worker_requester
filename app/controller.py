"""Application controller coordinating UI, state, workers, and broker."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
import logging

from PySide6.QtCore import QObject, QThread, Slot

from config.models import AppConfig
from services.broker import AbstractBrokerClient, BrokerResultEnvelope
from services.broker.routing import resolve_publish_route
from services.folder_scanner import FolderScanner
from services.result_parser import parse_task_result
from services.workers import PollingWorker, PublishWorker
from state.task_store import TaskStore
from ui.main_window import MainWindow


class TaskController(QObject):
    """Main orchestration layer for all UI actions and worker events."""

    def __init__(
        self,
        config: AppConfig,
        view: MainWindow,
        store: TaskStore,
        broker_provider: Callable[[], AbstractBrokerClient],
        logger: logging.Logger,
    ) -> None:
        super().__init__()
        self._config = config
        self._view = view
        self._store = store
        self._broker_provider = broker_provider
        self._logger = logger
        self._scanner = FolderScanner(config.publish.image_extensions)

        self._selected_folder: str | None = None

        self._publish_thread: QThread | None = None
        self._publish_worker: PublishWorker | None = None
        self._poll_thread: QThread | None = None
        self._poll_worker: PollingWorker | None = None

        self._publish_finished = True
        self._active = False
        self._active_timeout_seconds = config.publish.timeout_seconds
        self._pending_polling_interval = config.publish.polling_interval_seconds
        self._active_result_queue: str | None = None

        self._wire_signals()
        self._view.set_running_state(False)
        self._check_connection_once()

    def _wire_signals(self) -> None:
        self._view.add_folder_requested.connect(self.on_add_folder_requested)
        self._view.add_subfolders_requested.connect(self.on_add_subfolders_requested)
        self._view.clear_requested.connect(self.on_clear_requested)
        self._view.start_requested.connect(self.on_start_requested)
        self._view.stop_requested.connect(self.on_stop_requested)
        self._view.reset_requested.connect(self.on_reset_requested)
        self._view.folder_row_selected.connect(self.on_folder_row_selected)
        self._view.mq_preview_requested.connect(self.on_mq_preview_requested)

        self._store.folder_group_added.connect(self._on_folder_group_changed)
        self._store.folder_group_updated.connect(self._on_folder_group_changed)
        self._store.task_updated.connect(self._on_task_updated)
        self._store.store_reset.connect(self._on_store_reset)
        self._store.overall_updated.connect(self._view.set_overall_stats)

    @Slot(str)
    def on_add_folder_requested(self, folder_path: str) -> None:
        """Scan one folder and register image tasks."""

        folder_map = self._scanner.scan_single_folder(folder_path)
        if not folder_map:
            self._log(f"이미지 파일이 없습니다: {folder_path}")
            return

        added_folders, added_images = self._store.register_folder_map(folder_map)
        self._log(f"폴더 등록 완료 - 폴더 {added_folders}개, 이미지 {added_images}개")

    @Slot(str)
    def on_add_subfolders_requested(self, folder_path: str) -> None:
        """Scan direct subfolders (or recursive if configured) and register tasks."""

        folder_map = self._scanner.scan_subfolders(folder_path, mode=self._config.publish.scan_mode)
        if not folder_map:
            self._log(f"하위 폴더에서 이미지를 찾지 못했습니다: {folder_path}")
            return

        added_folders, added_images = self._store.register_folder_map(folder_map)
        self._log(
            f"sub_folder 등록 완료 - 대상 {len(folder_map)}개, 신규 폴더 {added_folders}개, 이미지 {added_images}개"
        )

    @Slot()
    def on_clear_requested(self) -> None:
        """Clear only current detail selection, not registered tasks."""

        self._selected_folder = None
        self._view.set_image_tasks([])

    @Slot(str)
    def on_folder_row_selected(self, folder_path: str) -> None:
        """Load selected folder's image details into bottom table."""

        self._selected_folder = folder_path
        self._view.set_image_tasks(self._store.get_image_tasks(folder_path))

    @Slot()
    def on_start_requested(self) -> None:
        """Create workers and start publish/polling flow."""

        if self._active:
            self._log("이미 작업이 실행 중입니다.")
            return

        action, recipe_path, polling_interval = self._view.current_runtime_settings()
        if not action:
            self._log("Action 값이 비어 있습니다.")
            return
        if not recipe_path:
            self._log("Recipe Path 값이 비어 있습니다.")
            return

        queue_name = self._config.rabbitmq.result_queue_base
        messages = self._store.build_pending_messages(action, queue_name, recipe_path)

        if not messages:
            self._log("전송할 PENDING 작업이 없습니다.")
            return

        self._active_timeout_seconds = self._config.publish.timeout_seconds
        self._pending_polling_interval = polling_interval
        self._publish_finished = False
        self._active = True
        self._active_result_queue = queue_name
        self._view.set_active_result_queue(queue_name)
        self._view.set_running_state(True)

        publish_exchange, publish_routing_key = resolve_publish_route(self._config.rabbitmq)
        for message in messages:
            self._store.set_task_expected_message(
                request_id=message.request_id,
                payload=message.to_dict(),
                meta={
                    "exchange": publish_exchange,
                    "routing_key": publish_routing_key,
                    "reply_to": message.QUEU_NAME,
                    "message_id": message.request_id,
                    "correlation_id": message.request_id,
                    "content_type": "application/json",
                },
            )

        self._start_publish_worker(
            messages=messages,
            publish_exchange=publish_exchange,
            publish_routing_key=publish_routing_key,
        )
        self._log(f"전송 시작 - 총 {len(messages)}건")

    @Slot()
    def on_stop_requested(self) -> None:
        """Stop publish/poll workers gracefully."""

        self._stop_workers("사용자 중지 요청")

    @Slot()
    def on_reset_requested(self) -> None:
        """Stop everything then clear registered tasks."""

        if not self._view.confirm_reset():
            return

        # Clear visible rows first so reset feedback is immediate to users.
        self._view.clear_progress_views()
        self._selected_folder = None

        try:
            self._stop_workers("초기화를 위해 워커를 중지합니다.")
        except Exception as exc:  # pylint: disable=broad-except
            self._log(f"워커 중지 중 오류가 발생했지만 초기화를 계속 진행합니다: {exc}")

        self._store.reset()
        self._active_result_queue = None
        self._view.set_active_result_queue(None)
        self._log("작업 상태를 초기화했습니다.")

    @Slot(str)
    def on_mq_preview_requested(self, request_id: str) -> None:
        """Open MQ preview dialog for selected image task row."""

        runtime_action, runtime_recipe_path, _ = self._view.current_runtime_settings()
        preview = self._store.build_mq_preview(
            request_id=request_id,
            app_config=self._config,
            active_result_queue=self._active_result_queue,
            runtime_action=runtime_action,
            runtime_recipe_path=runtime_recipe_path,
        )
        if preview is None:
            self._log(f"MQ 미리보기 대상을 찾지 못했습니다: {request_id}")
            return
        self._view.show_mq_preview(preview)

    def shutdown(self) -> None:
        """Application shutdown hook to avoid orphan threads."""

        self._stop_workers("프로그램 종료로 워커를 중지합니다.")

    def _start_publish_worker(
        self,
        messages: list,
        publish_exchange: str,
        publish_routing_key: str,
    ) -> None:
        """Spin up publish worker and attach thread cleanup signals."""

        self._publish_thread = QThread(self)
        self._publish_worker = PublishWorker(
            broker_provider=self._broker_provider,
            messages=messages,
            result_queue_base=self._config.rabbitmq.result_queue_base,
            publish_exchange=publish_exchange,
            publish_routing_key=publish_routing_key,
            max_retries=self._config.publish.max_publish_retries,
            retry_backoff_seconds=self._config.publish.publish_retry_backoff_seconds,
        )
        self._publish_worker.moveToThread(self._publish_thread)

        self._publish_thread.started.connect(self._publish_worker.run)
        self._publish_worker.queue_ready.connect(self._on_queue_ready)
        self._publish_worker.message_published.connect(self._on_message_published)
        self._publish_worker.message_failed.connect(self._on_message_failed)
        self._publish_worker.log.connect(self._log)
        self._publish_worker.finished.connect(self._on_publish_finished)

        self._publish_worker.finished.connect(self._publish_thread.quit)
        self._publish_worker.finished.connect(self._publish_worker.deleteLater)
        self._publish_thread.finished.connect(self._publish_thread.deleteLater)
        self._publish_thread.start()

    def _start_polling_worker(self, queue_name: str, polling_interval: int) -> None:
        """Spin up polling worker that tracks request results."""

        self._poll_thread = QThread(self)
        self._poll_worker = PollingWorker(
            broker_provider=self._broker_provider,
            queue_name=queue_name,
            polling_interval_seconds=polling_interval,
            max_messages_per_poll=self._config.publish.max_messages_per_poll,
        )
        self._poll_worker.moveToThread(self._poll_thread)

        self._poll_thread.started.connect(self._poll_worker.run)
        self._poll_worker.result_received.connect(self._on_result_received)
        self._poll_worker.poll_cycle.connect(self._on_poll_cycle)
        self._poll_worker.log.connect(self._log)
        self._poll_worker.finished.connect(self._on_poll_finished)

        self._poll_worker.finished.connect(self._poll_thread.quit)
        self._poll_worker.finished.connect(self._poll_worker.deleteLater)
        self._poll_thread.finished.connect(self._poll_thread.deleteLater)
        self._poll_thread.start()

    @Slot(str)
    def _on_queue_ready(self, queue_name: str) -> None:
        """Start polling only after result queue declaration is confirmed."""

        if self._poll_worker is None:
            self._start_polling_worker(
                queue_name=queue_name,
                polling_interval=self._pending_polling_interval,
            )
        self._active_result_queue = queue_name
        self._view.set_active_result_queue(queue_name)
        self._view.set_connection_status(True, f"결과 큐 준비 ({queue_name})")

    @Slot(str, int, int, object, object)
    def _on_message_published(
        self,
        request_id: str,
        index: int,
        total: int,
        payload: object,
        meta: object,
    ) -> None:
        self._store.mark_task_sent(request_id)
        safe_payload = payload if isinstance(payload, dict) else {}
        safe_meta = meta if isinstance(meta, dict) else {}
        self._store.set_task_published_message(
            request_id=request_id,
            payload=safe_payload,
            meta={str(key): str(value) for key, value in safe_meta.items()},
        )
        self._log(f"전송 완료 {index}/{total} - {request_id}")

    @Slot(str, str)
    def _on_message_failed(self, request_id: str, error: str) -> None:
        self._store.mark_task_error(request_id, error)
        self._log(f"전송 실패 - {request_id}: {error}")

    @Slot(object)
    def _on_result_received(self, envelope: object) -> None:
        if not isinstance(envelope, BrokerResultEnvelope):
            return

        try:
            parsed = parse_task_result(
                payload=envelope.payload,
                correlation_id=envelope.correlation_id,
                message_id=envelope.message_id,
            )
        except ValueError as exc:
            self._log(f"결과 파싱 실패: {exc}")
            return

        payload_request_id = str(envelope.payload.get("request_id", "")).strip()
        if payload_request_id:
            matched_by = "payload.request_id"
        elif envelope.correlation_id:
            matched_by = "correlation_id"
        elif envelope.message_id:
            matched_by = "message_id"
        else:
            matched_by = "unknown"

        self._store.set_task_received_message(
            request_id=parsed.request_id,
            payload=envelope.payload,
            meta={
                "message_id": str(envelope.message_id or ""),
                "correlation_id": str(envelope.correlation_id or ""),
                "matched_by": matched_by,
                "received_at": datetime.now().astimezone().isoformat(),
            },
        )

        changed = self._store.apply_result(parsed)
        if not changed:
            return

        status_label = "성공" if parsed.is_success else "실패"
        self._log(f"결과 반영 - {parsed.request_id}: {status_label}")

    @Slot(int)
    def _on_poll_cycle(self, received_count: int) -> None:
        self._log(f"Polling 수행 - 수신 {received_count}건")

        running_count = self._store.mark_inflight_running()
        if running_count:
            self._log(f"진행 중 상태 반영 - {running_count}건")

        timed_out_ids = self._store.mark_timeouts(self._active_timeout_seconds)
        for request_id in timed_out_ids:
            self._log(f"타임아웃 처리 - {request_id}")

        if self._publish_finished and self._store.all_tasks_terminal():
            self._stop_polling_only("모든 작업이 완료되어 polling을 종료합니다.")

    @Slot()
    def _on_publish_finished(self) -> None:
        self._publish_finished = True
        self._log("전송 워커 종료")

        if self._store.all_tasks_terminal():
            self._stop_polling_only("전송 종료 후 완료 상태 확인됨")

    @Slot()
    def _on_poll_finished(self) -> None:
        self._log("Polling 워커 종료")
        if self._publish_finished:
            self._active = False
            self._view.set_running_state(False)

    @Slot(str)
    def _on_folder_group_changed(self, folder_path: str) -> None:
        summary = self._store.get_folder_summary(folder_path)
        if summary is None:
            return
        self._view.upsert_folder_row(summary)

    @Slot(str)
    def _on_task_updated(self, request_id: str) -> None:
        task = self._store.get_task(request_id)
        if not task:
            return
        if self._selected_folder and task.folder_path == self._selected_folder:
            self._view.update_image_task(task)

    @Slot()
    def _on_store_reset(self) -> None:
        self._view.clear_progress_views()

    def _stop_polling_only(self, reason: str) -> None:
        if self._poll_worker:
            self._poll_worker.stop()
            self._log(reason)

    def _stop_workers(self, reason: str) -> None:
        self._safe_stop_worker(self._publish_worker)
        self._safe_stop_worker(self._poll_worker)

        self._safe_quit_thread(self._publish_thread)
        self._safe_quit_thread(self._poll_thread)

        # Prevent stale references to already deleted Qt objects.
        self._publish_worker = None
        self._poll_worker = None
        self._publish_thread = None
        self._poll_thread = None

        self._active = False
        self._publish_finished = True
        self._active_result_queue = None
        self._view.set_active_result_queue(None)
        self._view.set_running_state(False)
        self._log(reason)

    @staticmethod
    def _safe_stop_worker(worker: object | None) -> None:
        """Stop worker if available, ignoring deleted-object runtime errors."""

        if worker is None:
            return
        try:
            stop = getattr(worker, "stop", None)
            if callable(stop):
                stop()
        except RuntimeError:
            return

    @staticmethod
    def _safe_quit_thread(thread: QThread | None) -> None:
        """Quit/wait thread safely, tolerating deleted Qt wrappers."""

        if thread is None:
            return
        try:
            if thread.isRunning():
                thread.quit()
                thread.wait(1500)
        except RuntimeError:
            return

    def _check_connection_once(self) -> None:
        """Best-effort broker connectivity check for initial status badge."""

        broker = self._broker_provider()
        try:
            broker.connect()
            self._view.set_connection_status(True, "연결 가능")
            self._log("RabbitMQ 연결 확인 성공")
        except Exception as exc:  # pylint: disable=broad-except
            self._view.set_connection_status(False, "연결 실패")
            self._log(f"RabbitMQ 연결 확인 실패: {exc}")
        finally:
            try:
                broker.close()
            except Exception:  # pragma: no cover
                pass

    def _log(self, message: str) -> None:
        """Write timestamped logs to UI and logger."""

        timestamped = f"[{datetime.now().strftime('%H:%M:%S')}] {message}"
        self._view.append_log(timestamped)
        self._logger.info(message)
