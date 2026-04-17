"""Application controller coordinating UI, state, workers, and broker."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
import logging

from PySide6.QtCore import QObject, QThread, Slot

from config.models import AppConfig
from models.task_models import TaskMessage
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
        self._publish_exchange = ""
        self._publish_routing_key = ""

        self._max_active_open_folders = max(1, int(config.publish.max_active_open_folders))
        self._max_initial_open_folders = max(
            1,
            min(int(config.publish.initial_open_folders), self._max_active_open_folders),
        )
        self._next_open_threshold = 50.0
        self._folder_message_batches: list[tuple[str, list[TaskMessage]]] = []
        self._next_folder_batch_index = 0
        self._opened_folder_paths: set[str] = set()
        self._active_folder_paths: set[str] = set()
        self._scheduled_request_ids: set[str] = set()
        self._planned_publish_total = 0
        self._published_count = 0
        self._last_dispatch_skip_reason: str | None = None

        self._wire_signals()
        self._view.set_running_state(False)
        self._check_connection_once()

    def _wire_signals(self) -> None:
        self._view.add_folder_requested.connect(self.on_add_folder_requested)
        self._view.add_subfolders_requested.connect(self.on_add_subfolders_requested)
        self._view.delete_folders_requested.connect(self.on_delete_folders_requested)
        self._view.clear_requested.connect(self.on_clear_requested)
        self._view.start_requested.connect(self.on_start_requested)
        self._view.stop_requested.connect(self.on_stop_requested)
        self._view.reset_requested.connect(self.on_reset_requested)
        self._view.folder_row_selected.connect(self.on_folder_row_selected)
        self._view.mq_preview_requested.connect(self.on_mq_preview_requested)

        self._store.folder_group_added.connect(self._on_folder_group_changed)
        self._store.folder_group_updated.connect(self._on_folder_group_changed)
        self._store.folder_group_removed.connect(self._on_folder_group_removed)
        self._store.task_updated.connect(self._on_task_updated)
        self._store.store_reset.connect(self._on_store_reset)
        self._store.overall_updated.connect(self._view.set_overall_stats)

    @Slot(list)
    def on_add_folder_requested(self, folder_paths: list[str]) -> None:
        """Scan selected folders and register image tasks."""

        self._register_selected_folders(folder_paths=folder_paths, include_subfolders=False)

    @Slot(list)
    def on_add_subfolders_requested(self, folder_paths: list[str]) -> None:
        """Scan selected roots' subfolders (or recursive) and register tasks."""

        self._register_selected_folders(folder_paths=folder_paths, include_subfolders=True)

    @Slot(list)
    def on_delete_folders_requested(self, folder_paths: list[str]) -> None:
        """Delete selected folders only when every task is still PENDING."""

        deduped_paths: list[str] = []
        seen: set[str] = set()
        for folder_path in folder_paths:
            if folder_path in seen:
                continue
            seen.add(folder_path)
            deduped_paths.append(folder_path)

        if not deduped_paths:
            return

        removed_folders, blocked_folders, removed_request_ids, removed_task_count = (
            self._store.remove_pending_only_folders(deduped_paths)
        )

        if removed_folders:
            self._prune_scheduling_for_removed_folders(
                removed_folder_paths=removed_folders,
                removed_request_ids=set(removed_request_ids),
            )
            if self._selected_folder and self._selected_folder in removed_folders:
                self._selected_folder = None
                self._view.set_image_tasks([])
            self._log(
                f"선택 삭제 완료 - 폴더 {len(removed_folders)}개, 이미지 {removed_task_count}개"
            )
            if self._active:
                self._maybe_dispatch_next_folder_batch()

        if blocked_folders:
            self._log(
                "삭제 차단 - 전송 중이거나 처리된 폴더는 삭제할 수 없습니다: "
                + ", ".join(blocked_folders)
            )
        if not removed_folders and not blocked_folders:
            self._log("선택 삭제 대상이 없습니다.")

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

        action, recipe_path, polling_interval, priority = self._view.current_runtime_settings()
        if not action:
            self._log("Action 값이 비어 있습니다.")
            return
        if not recipe_path:
            self._log("Recipe Path 값이 비어 있습니다.")
            return

        queue_name = self._config.rabbitmq.result_queue_base
        grouped_messages = self._store.build_pending_messages_by_folder(
            action,
            queue_name,
            recipe_path,
            priority=priority,
        )

        if not grouped_messages:
            if self._store.has_inflight_tasks():
                self._start_polling_only_resume(
                    queue_name=queue_name,
                    polling_interval=polling_interval,
                )
            else:
                self._log("전송할 PENDING 작업이 없습니다.")
            return

        self._reset_publish_schedule_state()
        self._folder_message_batches = grouped_messages
        self._scheduled_request_ids = {
            message.request_id
            for _, messages in grouped_messages
            for message in messages
        }
        self._planned_publish_total = sum(len(messages) for _, messages in grouped_messages)

        self._active_timeout_seconds = self._config.publish.timeout_seconds
        self._pending_polling_interval = polling_interval
        self._publish_finished = False
        self._active = True
        self._active_result_queue = queue_name
        self._view.set_active_result_queue(queue_name)
        self._view.set_running_state(True)

        self._publish_exchange, self._publish_routing_key = resolve_publish_route(self._config.rabbitmq)
        self._set_expected_messages_for_groups(grouped_messages)

        initial_messages, opened_folders = self._take_next_folder_batches(self._max_initial_open_folders)
        if not initial_messages:
            self._log("초기 전송 대상 메시지를 구성하지 못했습니다.")
            self._active = False
            self._view.set_running_state(False)
            return

        self._start_publish_worker(
            messages=initial_messages,
            publish_exchange=self._publish_exchange,
            publish_routing_key=self._publish_routing_key,
        )
        self._log(
            f"전송 시작 - 총 {self._planned_publish_total}건, 초기 개방 폴더 {len(opened_folders)}개 "
            f"(정책 최대 {self._max_initial_open_folders}개)"
        )
        self._log(
            f"밸런싱 정책 - 개방 폴더 중 진행률 {self._next_open_threshold:.0f}% 도달 시 다음 폴더 1개 추가 개방 "
            f"(동시 active cap={self._max_active_open_folders})"
        )

    def _start_polling_only_resume(self, queue_name: str, polling_interval: int) -> None:
        """Resume monitoring for already-sent tasks when no pending publish exists."""

        self._reset_publish_schedule_state()
        self._active_timeout_seconds = self._config.publish.timeout_seconds
        self._pending_polling_interval = polling_interval
        self._publish_finished = True
        self._active = True
        self._active_result_queue = queue_name
        self._view.set_active_result_queue(queue_name)
        self._view.set_running_state(True)
        self._view.set_connection_status(True, f"결과 모니터링 재개 ({queue_name})")
        self._start_polling_worker(
            queue_name=queue_name,
            polling_interval=polling_interval,
        )
        self._log(
            "전송 대기 작업은 없고 SENT/RUNNING 작업이 있어 결과 모니터링만 재개합니다."
        )

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

        runtime_action, runtime_recipe_path, _, runtime_priority = self._view.current_runtime_settings()
        preview = self._store.build_mq_preview(
            request_id=request_id,
            app_config=self._config,
            active_result_queue=self._active_result_queue,
            runtime_action=runtime_action,
            runtime_recipe_path=runtime_recipe_path,
            runtime_priority=runtime_priority,
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
        messages: list[TaskMessage],
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
        self._store.set_task_published_message(request_id=request_id, payload=safe_payload, meta=dict(safe_meta))
        _ = index
        _ = total
        self._published_count += 1
        publish_total = self._planned_publish_total if self._planned_publish_total else self._published_count
        self._log(f"전송 완료 {self._published_count}/{publish_total} - {request_id}")

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

        self._maybe_dispatch_next_folder_batch()

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
    def _on_folder_group_removed(self, _folder_path: str) -> None:
        """Refresh folder tables after folder deletion events."""

        self._view.set_folder_rows(self._store.get_folder_summaries())

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
        self._reset_publish_schedule_state()

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

    def _register_selected_folders(self, folder_paths: list[str], include_subfolders: bool) -> None:
        """Scan/register folders from multi-selection and optionally enqueue during run."""

        normalized_paths = self._dedupe_paths(folder_paths)
        if not normalized_paths:
            self._log("선택된 폴더가 없습니다.")
            return

        folder_map = self._scan_selected_folder_map(
            folder_paths=normalized_paths,
            include_subfolders=include_subfolders,
        )
        if not folder_map:
            if include_subfolders:
                self._log(
                    f"선택한 {len(normalized_paths)}개 경로의 하위 폴더에서 이미지를 찾지 못했습니다."
                )
            else:
                self._log(f"선택한 {len(normalized_paths)}개 폴더에서 이미지를 찾지 못했습니다.")
            return

        added_folders, added_images = self._store.register_folder_map(folder_map)
        mode_label = "sub_folder" if include_subfolders else "폴더"
        self._log(
            f"{mode_label} 등록 완료 - 스캔 대상 {len(normalized_paths)}개, "
            f"신규 폴더 {added_folders}개, 신규 이미지 {added_images}개"
        )

        if not self._active or added_images <= 0:
            return

        runtime_action, runtime_recipe_path, _, runtime_priority = self._view.current_runtime_settings()
        runtime_action = runtime_action or self._config.publish.default_action
        runtime_recipe_path = runtime_recipe_path or self._config.recipe_config.default_path
        runtime_queue = self._active_result_queue or self._config.rabbitmq.result_queue_base
        grouped_messages = self._store.build_pending_messages_for_folders(
            action=runtime_action,
            result_queue_name=runtime_queue,
            recipe_path=runtime_recipe_path,
            folder_paths=list(folder_map.keys()),
            priority=runtime_priority,
            exclude_request_ids=self._scheduled_request_ids,
        )
        if not grouped_messages:
            self._log("실행 중 추가 - 신규 전송 대상이 없어 스케줄 편입을 생략합니다.")
            return

        self._append_folder_batches(grouped_messages)
        self._set_expected_messages_for_groups(grouped_messages)
        added_messages = sum(len(messages) for _, messages in grouped_messages)
        self._planned_publish_total += added_messages

        for _, messages in grouped_messages:
            for message in messages:
                self._scheduled_request_ids.add(message.request_id)

        self._log(
            f"실행 중 추가 편입 - 폴더 {len(grouped_messages)}개, 메시지 {added_messages}건 "
            f"(active={len(self._active_folder_paths)}, remaining_batches="
            f"{len(self._folder_message_batches) - self._next_folder_batch_index})"
        )
        self._maybe_dispatch_next_folder_batch()

    def _scan_selected_folder_map(
        self,
        folder_paths: list[str],
        include_subfolders: bool,
    ) -> dict[str, list[str]]:
        """Scan all selected paths and merge image maps without duplicates."""

        merged_map: dict[str, list[str]] = {}
        merged_seen: dict[str, set[str]] = {}

        for folder_path in folder_paths:
            if include_subfolders:
                scanned_map = self._scanner.scan_subfolders(
                    folder_path,
                    mode=self._config.publish.scan_mode,
                )
            else:
                scanned_map = self._scanner.scan_single_folder(folder_path)

            for group_path, image_paths in scanned_map.items():
                if not image_paths:
                    continue
                bucket = merged_map.setdefault(group_path, [])
                seen_images = merged_seen.setdefault(group_path, set())
                for image_path in image_paths:
                    if image_path in seen_images:
                        continue
                    seen_images.add(image_path)
                    bucket.append(image_path)

        return merged_map

    @staticmethod
    def _dedupe_paths(paths: list[str]) -> list[str]:
        """Deduplicate selected paths while preserving original order."""

        ordered: list[str] = []
        seen: set[str] = set()
        for path in paths:
            normalized = path.strip()
            if not normalized:
                continue
            if normalized in seen:
                continue
            seen.add(normalized)
            ordered.append(normalized)
        return ordered

    def _set_expected_messages_for_groups(
        self,
        grouped_messages: list[tuple[str, list[TaskMessage]]],
    ) -> None:
        """Store expected publish payload/meta snapshots for MQ preview."""

        for _, messages in grouped_messages:
            for message in messages:
                self._store.set_task_expected_message(
                    request_id=message.request_id,
                    payload=message.to_dict(),
                    meta={
                        "exchange": self._publish_exchange,
                        "routing_key": self._publish_routing_key,
                        "reply_to": message.QUEUE_NAME,
                        "message_id": message.request_id,
                        "correlation_id": message.request_id,
                        "content_type": "application/json",
                        "priority": message.priority,
                    },
                )

    def _append_folder_batches(
        self,
        grouped_messages: list[tuple[str, list[TaskMessage]]],
    ) -> None:
        """Append new folder batches to dispatch queue, merging by folder path."""

        batch_index_by_folder: dict[str, int] = {}
        for idx in range(self._next_folder_batch_index, len(self._folder_message_batches)):
            folder_path, _ = self._folder_message_batches[idx]
            if folder_path not in batch_index_by_folder:
                batch_index_by_folder[folder_path] = idx

        for folder_path, messages in grouped_messages:
            if not messages:
                continue
            existing_idx = batch_index_by_folder.get(folder_path)
            if existing_idx is None:
                self._folder_message_batches.append((folder_path, list(messages)))
                batch_index_by_folder[folder_path] = len(self._folder_message_batches) - 1
                continue
            self._folder_message_batches[existing_idx][1].extend(messages)

    def _prune_scheduling_for_removed_folders(
        self,
        removed_folder_paths: list[str],
        removed_request_ids: set[str],
    ) -> None:
        """Prune removed folders from scheduling state and pending batches."""

        removed_folder_set = set(removed_folder_paths)
        old_batches = list(self._folder_message_batches)
        old_next_index = self._next_folder_batch_index

        new_batches: list[tuple[str, list[TaskMessage]]] = []
        new_next_index = 0
        for idx, (folder_path, messages) in enumerate(old_batches):
            if folder_path in removed_folder_set:
                continue
            kept_messages = [m for m in messages if m.request_id not in removed_request_ids]
            if not kept_messages:
                continue
            new_batches.append((folder_path, kept_messages))
            if idx < old_next_index:
                new_next_index += 1

        self._folder_message_batches = new_batches
        self._next_folder_batch_index = min(new_next_index, len(new_batches))
        self._opened_folder_paths.difference_update(removed_folder_set)
        self._active_folder_paths.difference_update(removed_folder_set)
        self._scheduled_request_ids.difference_update(removed_request_ids)
        if removed_request_ids:
            self._planned_publish_total = max(0, self._planned_publish_total - len(removed_request_ids))
        self._last_dispatch_skip_reason = None

    def _reset_publish_schedule_state(self) -> None:
        """Reset folder-based dispatch scheduling state."""

        self._folder_message_batches = []
        self._next_folder_batch_index = 0
        self._opened_folder_paths.clear()
        self._active_folder_paths.clear()
        self._scheduled_request_ids.clear()
        self._planned_publish_total = 0
        self._published_count = 0
        self._publish_exchange = ""
        self._publish_routing_key = ""
        self._last_dispatch_skip_reason = None

    def _take_next_folder_batches(
        self,
        max_folder_count: int,
    ) -> tuple[list[TaskMessage], list[str]]:
        """Take next folder batches and flatten to one message list."""

        selected_messages: list[TaskMessage] = []
        opened_folders: list[str] = []

        while (
            len(opened_folders) < max_folder_count
            and self._next_folder_batch_index < len(self._folder_message_batches)
        ):
            folder_path, messages = self._folder_message_batches[self._next_folder_batch_index]
            self._next_folder_batch_index += 1
            opened_folders.append(folder_path)
            self._opened_folder_paths.add(folder_path)
            self._active_folder_paths.add(folder_path)
            selected_messages.extend(messages)

        return selected_messages, opened_folders

    def _maybe_dispatch_next_folder_batch(self) -> None:
        """Dispatch next folder batches using refill-first, threshold-second policy."""

        self._refresh_active_folder_paths()
        remaining_batches = len(self._folder_message_batches) - self._next_folder_batch_index
        if self._next_folder_batch_index >= len(self._folder_message_batches):
            self._last_dispatch_skip_reason = None
            return
        if self._is_publish_worker_running():
            self._log_dispatch_skip(
                reason="publish worker running",
                remaining_batches=remaining_batches,
                available_slots=self._available_open_slots(),
            )
            return

        available_slots = self._available_open_slots()
        if available_slots <= 0:
            self._log_dispatch_skip(
                reason=f"active cap reached({self._max_active_open_folders})",
                remaining_batches=remaining_batches,
                available_slots=available_slots,
            )
            return

        if self._should_backfill_slots():
            self._dispatch_folder_batches(
                folder_count=available_slots,
                trigger="slot refill",
            )
            return

        if not self._should_expand_by_threshold():
            self._log_dispatch_skip(
                reason="threshold unmet",
                remaining_batches=remaining_batches,
                available_slots=available_slots,
            )
            return

        self._dispatch_folder_batches(
            folder_count=1,
            trigger=f"threshold met({self._next_open_threshold:.0f}%)",
        )

    def _refresh_active_folder_paths(self) -> None:
        """Keep only non-terminal folders in active tracking set."""

        stale_paths: list[str] = []
        for folder_path in self._active_folder_paths:
            summary = self._store.get_folder_summary(folder_path)
            if summary is None or summary.status.is_done:
                stale_paths.append(folder_path)

        if not stale_paths:
            return

        for folder_path in stale_paths:
            self._active_folder_paths.discard(folder_path)

    def _is_publish_worker_running(self) -> bool:
        """Return whether one publish worker thread is still active."""

        if self._publish_thread is None:
            return False
        try:
            return self._publish_thread.isRunning()
        except RuntimeError:
            return False

    def _available_open_slots(self) -> int:
        """Return how many additional active folders can be opened right now."""

        return max(0, self._max_active_open_folders - len(self._active_folder_paths))

    def _should_backfill_slots(self) -> bool:
        """Return whether completed folders freed slots that should be refilled immediately."""

        return len(self._opened_folder_paths) > len(self._active_folder_paths)

    def _should_expand_by_threshold(self) -> bool:
        """Return whether ramp-up should open one more folder via progress threshold."""

        if self._max_initial_open_folders >= self._max_active_open_folders:
            return False
        if len(self._opened_folder_paths) != len(self._active_folder_paths):
            return False

        for folder_path in self._active_folder_paths:
            summary = self._store.get_folder_summary(folder_path)
            if summary is None:
                continue
            if summary.progress >= self._next_open_threshold:
                return True
        return False

    def _dispatch_folder_batches(self, folder_count: int, trigger: str) -> None:
        """Open the next N folder batches and start one publish worker for them."""

        next_messages, opened_folders = self._take_next_folder_batches(folder_count)
        if not next_messages:
            self._last_dispatch_skip_reason = None
            return

        self._last_dispatch_skip_reason = None
        self._publish_finished = False
        self._start_publish_worker(
            messages=next_messages,
            publish_exchange=self._publish_exchange,
            publish_routing_key=self._publish_routing_key,
        )
        self._log(
            f"밸런싱 개방 - {trigger} "
            f"(opened_now={len(opened_folders)}, "
            f"active_count={len(self._active_folder_paths)}, "
            f"opened_count={len(self._opened_folder_paths)}, "
            f"remaining_batches={len(self._folder_message_batches) - self._next_folder_batch_index}, "
            f"available_slots={self._available_open_slots()})"
        )

    def _log_dispatch_skip(self, reason: str, remaining_batches: int, available_slots: int) -> None:
        """Log dispatch skip reason with deduplication to avoid noisy logs."""

        dedupe_key = (
            f"{reason}|active={len(self._active_folder_paths)}|opened={len(self._opened_folder_paths)}"
            f"|remaining={remaining_batches}|slots={available_slots}"
        )
        if dedupe_key == self._last_dispatch_skip_reason:
            return
        self._last_dispatch_skip_reason = dedupe_key
        self._log(
            "밸런싱 대기 - "
            f"{reason} (active_count={len(self._active_folder_paths)}, "
            f"opened_count={len(self._opened_folder_paths)}, "
            f"remaining_batches={remaining_batches}, "
            f"available_slots={available_slots})"
        )
