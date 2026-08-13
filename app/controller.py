"""Application controller coordinating UI, state, workers, and broker."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
import logging
from pathlib import Path
import time

from PySide6.QtCore import QObject, QThread, QTimer, Slot

from config.models import AppConfig
from models.task_models import TaskMessage, TaskStatus
from services.broker import AbstractBrokerClient, BrokerResultEnvelope
from services.broker.result_queue import resolve_local_ipv4, resolve_result_queue_name
from services.broker.routing import resolve_publish_route
from services.folder_scanner import FolderScanner
from services.result_parser import parse_task_result
from services.workers import FolderDiscoveryWorker, PollingWorker, PublishWorker, ScanWorker
from services.workers.queue_metrics_worker import QueueMetricsWorker
from state.task_store import TaskStore
from state.sqlite_task_store import SqliteTaskStore
from ui.main_window import MainWindow


class TaskController(QObject):
    """Main orchestration layer for all UI actions and worker events."""

    def __init__(
        self,
        config: AppConfig,
        view: MainWindow,
        store: TaskStore | SqliteTaskStore,
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
        self._lazy_mode = bool(getattr(store, "supports_lazy_loading", False))

        self._selected_folder: str | None = None

        self._publish_thread: QThread | None = None
        self._publish_worker: PublishWorker | None = None
        self._poll_thread: QThread | None = None
        self._poll_worker: PollingWorker | None = None
        self._queue_metrics_thread: QThread | None = None
        self._queue_metrics_worker: QueueMetricsWorker | None = None
        self._scan_threads: dict[str, QThread] = {}
        self._scan_workers: dict[str, ScanWorker] = {}
        self._inventory_preparing = False
        self._discovery_thread: QThread | None = None
        self._discovery_worker: FolderDiscoveryWorker | None = None
        self._start_after_discovery = False

        self._publish_finished = True
        self._active = False
        self._runtime_options_locked = False
        self._active_timeout_seconds = config.publish.timeout_seconds
        self._pending_polling_interval = config.publish.polling_interval_seconds
        self._active_result_queue: str | None = None
        self._resolved_local_ipv4: str | None = None
        self._resolved_result_queue: str | None = None
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
        self._last_publish_progress_log_at = 0.0
        self._last_publish_progress_count = 0
        self._result_applied_count = 0
        self._last_result_progress_log_at = 0.0
        self._last_result_progress_count = 0
        self._last_dispatch_skip_reason: str | None = None
        self._last_queue_message_count: int | None = None
        self._last_worker_count: int | None = None
        self._lazy_action = ""
        self._lazy_priority = 0
        self._dirty_folder_paths: set[str] = set()
        self._dirty_task_ids: set[str] = set()
        self._latest_overall_stats: dict[str, float | int | None] | None = None
        self._duplicate_folder_paths: dict[str, None] = {}
        self._ui_refresh_timer = QTimer(self)
        self._ui_refresh_timer.setInterval(max(100, int(config.publish.ui_refresh_interval_ms)))
        self._ui_refresh_timer.timeout.connect(self._flush_ui_updates)
        self._ui_refresh_timer.start()

        self._wire_signals()
        if self._lazy_mode:
            self._view.set_folder_rows(self._store.get_folder_summaries())
            self._view.set_overall_stats(self._store.overall_stats())
        self._view.set_running_state(False)
        self._sync_runtime_options_enabled()
        self._check_connection_once()
        if not getattr(self._view, "_disable_queue_metrics_monitor", False):
            self._start_queue_metrics_monitor()
        if self._lazy_mode and self._store.should_auto_resume():  # type: ignore[attr-defined]
            QTimer.singleShot(0, self._resume_persisted_lazy_session)

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
        page_signal = getattr(self._view, "image_page_requested", None)
        if page_signal is not None:
            page_signal.connect(self._on_image_page_requested)

        self._store.folder_group_added.connect(self._on_folder_group_changed)
        self._store.folder_group_updated.connect(self._on_folder_group_changed)
        self._store.folder_group_removed.connect(self._on_folder_group_removed)
        self._store.duplicate_folders_detected.connect(self._on_duplicate_folders_detected)
        self._store.task_updated.connect(self._on_task_updated)
        self._store.store_reset.connect(self._on_store_reset)
        self._store.overall_updated.connect(self._on_overall_updated)

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
            self._safe_update_poll_tracked_ids(remove_request_ids=removed_request_ids)
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
        tasks = self._store.get_image_tasks(folder_path)
        if self._lazy_mode and hasattr(self._view, "set_image_task_page"):
            summary = self._store.get_folder_summary(folder_path)
            self._view.set_image_task_page(
                tasks,
                total_count=summary.total if summary is not None else len(tasks),
                append=False,
            )
        else:
            self._view.set_image_tasks(tasks)

    @Slot(int)
    def _on_image_page_requested(self, offset: int) -> None:
        if not self._lazy_mode or not self._selected_folder:
            return
        tasks = self._store.get_image_tasks(self._selected_folder, offset=offset, limit=500)
        summary = self._store.get_folder_summary(self._selected_folder)
        self._view.set_image_task_page(
            tasks,
            total_count=summary.total if summary is not None else offset + len(tasks),
            append=True,
        )

    @Slot()
    def on_start_requested(self) -> None:
        """Create workers and start publish/polling flow."""

        if self._active:
            self._log("이미 작업이 실행 중입니다.")
            return
        if self._lazy_mode:
            self._start_lazy_session()
            return

        action, recipe_path, polling_interval, priority = self._view.current_runtime_settings()
        if not action:
            self._log("Action 값이 비어 있습니다.")
            return
        if not recipe_path:
            self._log("Recipe Path 값이 비어 있습니다.")
            return
        self._warn_if_recipe_file_missing(recipe_path)

        try:
            queue_name = self._ensure_resolved_result_queue()
        except RuntimeError as exc:
            self._view.set_connection_status(False, "결과 큐 결정 실패")
            self._log(f"결과 큐 이름 결정 실패: {exc}")
            return

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
                self._set_runtime_options_locked(False)
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
        self._set_runtime_options_locked(True)
        if self._resolved_local_ipv4:
            self._log(
                f"결과 큐 결정 - base={self._config.rabbitmq.result_queue_base}, "
                f"local_ipv4={self._resolved_local_ipv4}, resolved={queue_name}"
            )

        self._publish_exchange, self._publish_routing_key = resolve_publish_route(self._config.rabbitmq)
        self._set_expected_messages_for_groups(grouped_messages)

        initial_messages, opened_folders = self._take_next_folder_batches(self._max_initial_open_folders)
        if not initial_messages:
            self._log("초기 전송 대상 메시지를 구성하지 못했습니다.")
            self._active = False
            self._view.set_running_state(False)
            self._set_runtime_options_locked(False)
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
        self._set_runtime_options_locked(True)
        self._view.set_connection_status(True, f"결과 모니터링 재개 ({queue_name})")
        self._start_polling_worker(
            queue_name=queue_name,
            polling_interval=polling_interval,
            tracked_request_ids=self._store.get_known_request_ids(),
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
        self._set_runtime_options_locked(False)
        self._log("작업 상태를 초기화했습니다.")

    @Slot(str)
    def on_mq_preview_requested(self, request_id: str) -> None:
        """Open MQ preview dialog for selected image task row."""

        runtime_action, runtime_recipe_path, _, runtime_priority = self._view.current_runtime_settings()
        resolved_local_ipv4 = self._resolve_local_ipv4_for_preview()
        preview = self._store.build_mq_preview(
            request_id=request_id,
            app_config=self._config,
            active_result_queue=self._active_result_queue,
            runtime_action=runtime_action,
            runtime_recipe_path=runtime_recipe_path,
            runtime_priority=runtime_priority,
            resolved_local_ipv4=resolved_local_ipv4,
        )
        if preview is None:
            self._log(f"MQ 미리보기 대상을 찾지 못했습니다: {request_id}")
            return
        self._view.show_mq_preview(preview)

    def shutdown(self) -> None:
        """Application shutdown hook to avoid orphan threads."""

        self._ui_refresh_timer.stop()
        self._stop_workers("프로그램 종료로 워커를 중지합니다.")
        self._stop_queue_metrics_monitor()

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
            result_queue_name=self._active_result_queue or self._ensure_resolved_result_queue(),
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
        self._publish_thread.finished.connect(self._on_publish_thread_finished)
        self._publish_thread.finished.connect(self._publish_thread.deleteLater)
        self._publish_thread.start()

    def _start_polling_worker(
        self,
        queue_name: str,
        polling_interval: int,
        tracked_request_ids: set[str] | None = None,
    ) -> None:
        """Spin up polling worker that tracks request results."""

        self._poll_thread = QThread(self)
        self._poll_worker = PollingWorker(
            broker_provider=self._broker_provider,
            queue_name=queue_name,
            polling_interval_seconds=polling_interval,
            max_messages_per_poll=self._config.publish.max_messages_per_poll,
            tracked_request_ids=tracked_request_ids,
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

    def _start_queue_metrics_monitor(self) -> None:
        """Start a background monitor for request queue consumer/message counters."""

        if self._queue_metrics_thread is not None:
            try:
                if self._queue_metrics_thread.isRunning():
                    return
            except RuntimeError:
                self._queue_metrics_thread = None
                self._queue_metrics_worker = None

        self._queue_metrics_thread = QThread(self)
        self._queue_metrics_worker = QueueMetricsWorker(
            broker_provider=self._broker_provider,
            queue_name=self._config.rabbitmq.request_queue,
            interval_seconds=5,
        )
        self._queue_metrics_worker.moveToThread(self._queue_metrics_thread)

        self._queue_metrics_thread.started.connect(self._queue_metrics_worker.run)
        self._queue_metrics_worker.metrics_updated.connect(self._on_queue_metrics_updated)
        self._queue_metrics_worker.log.connect(self._log)
        self._queue_metrics_worker.finished.connect(self._on_queue_metrics_finished)

        self._queue_metrics_worker.finished.connect(self._queue_metrics_thread.quit)
        self._queue_metrics_worker.finished.connect(self._queue_metrics_worker.deleteLater)
        self._queue_metrics_thread.finished.connect(self._queue_metrics_thread.deleteLater)
        self._queue_metrics_thread.start()
        self._log(
            f"Queue metrics 모니터 시작 - queue={self._config.rabbitmq.request_queue}, interval=5s"
        )

    @Slot(str)
    def _on_queue_ready(self, queue_name: str) -> None:
        """Start polling only after result queue declaration is confirmed."""

        if not self._is_poll_worker_running():
            self._start_polling_worker(
                queue_name=queue_name,
                polling_interval=self._pending_polling_interval,
                tracked_request_ids=(
                    None if self._lazy_mode else self._store.get_known_request_ids()
                ),
            )
        self._active_result_queue = queue_name
        self._view.set_active_result_queue(queue_name)
        self._view.set_connection_status(True, "연결 성공")

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
        if self._lazy_mode and self._last_queue_message_count is not None:
            self._last_queue_message_count += 1
        publish_total = self._planned_publish_total if self._planned_publish_total else self._published_count
        now = time.monotonic()
        should_log_progress = (
            self._published_count == publish_total
            or self._published_count - self._last_publish_progress_count >= 100
            or now - self._last_publish_progress_log_at >= 1.0
        )
        if should_log_progress:
            recent_count = self._published_count - self._last_publish_progress_count
            self._log(
                f"전송 진행 {self._published_count}/{publish_total} "
                f"· 최근 구간 {recent_count}건"
            )
            self._last_publish_progress_count = self._published_count
            self._last_publish_progress_log_at = now

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

        self._result_applied_count += 1
        if parsed.is_success:
            now = time.monotonic()
            if (
                self._result_applied_count - self._last_result_progress_count >= 100
                or now - self._last_result_progress_log_at >= 1.0
                or self._store.all_tasks_terminal()
            ):
                recent_count = self._result_applied_count - self._last_result_progress_count
                self._log(
                    f"결과 반영 {self._result_applied_count}건 · 최근 구간 {recent_count}건"
                )
                self._last_result_progress_count = self._result_applied_count
                self._last_result_progress_log_at = now
        else:
            self._log(f"결과 반영 - {parsed.request_id}: 실패")
        if self._lazy_mode:
            self._maybe_dispatch_lazy()

    @Slot(int)
    def _on_poll_cycle(self, received_count: int) -> None:
        self._log(f"Polling 수행 - 수신 {received_count}건")

        running_count = self._store.mark_inflight_running()
        if running_count:
            self._log(f"진행 중 상태 반영 - {running_count}건")

        if self._lazy_mode:
            self._maybe_dispatch_lazy()
        else:
            self._maybe_dispatch_next_folder_batch()

        timed_out_ids = self._store.mark_timeouts(self._active_timeout_seconds)
        for request_id in timed_out_ids:
            self._log(f"타임아웃 처리 - {request_id}")

        if self._publish_finished and self._store.all_tasks_terminal():
            self._stop_polling_only("모든 작업이 완료되어 polling을 종료합니다.")

    @Slot()
    def _on_publish_finished(self) -> None:
        sender_worker = self.sender()
        if sender_worker is not None and sender_worker is not self._publish_worker:
            # Ignore stale finish signals from previously replaced workers.
            return

        self._publish_finished = True
        self._log("전송 워커 종료")

        if self._lazy_mode:
            return

        if self._store.all_tasks_terminal():
            self._stop_polling_only("전송 종료 후 완료 상태 확인됨")

    @Slot()
    def _on_publish_thread_finished(self) -> None:
        """Release a bounded chunk left unpublished and continue after thread cleanup."""

        self._publish_thread = None
        self._publish_worker = None
        if not self._lazy_mode:
            return
        released = self._store.release_claimed()  # type: ignore[attr-defined]
        if released:
            self._log(f"미발행 작업 {released}건을 전송 대기로 복구했습니다.")
            return
        self._maybe_dispatch_lazy()

    @Slot()
    def _on_poll_finished(self) -> None:
        self._log("Polling 워커 종료")
        self._poll_worker = None
        self._poll_thread = None
        if self._publish_finished:
            self._active = False
            self._view.set_running_state(False)
            if self._store.all_tasks_terminal():
                if self._lazy_mode:
                    self._store.disable_auto_resume()  # type: ignore[attr-defined]
                self._set_runtime_options_locked(False)

    @Slot(int, int)
    def _on_queue_metrics_updated(self, worker_count: int, message_count: int) -> None:
        """Reflect request-queue metrics in connection area."""

        if worker_count < 0 or message_count < 0:
            self._last_worker_count = None
            self._last_queue_message_count = None
            self._view.set_queue_metrics(None, None)
            if self._lazy_mode:
                self._maybe_dispatch_lazy()
            return
        self._last_worker_count = worker_count
        self._last_queue_message_count = message_count
        self._view.set_queue_metrics(worker_count, message_count)
        if self._lazy_mode:
            self._maybe_dispatch_lazy()

    @Slot()
    def _on_queue_metrics_finished(self) -> None:
        """Cleanup metrics-monitor references when thread exits."""

        self._queue_metrics_worker = None
        self._queue_metrics_thread = None

    @Slot(str)
    def _on_folder_group_changed(self, folder_path: str) -> None:
        self._dirty_folder_paths.add(folder_path)

    @Slot(str)
    def _on_folder_group_removed(self, _folder_path: str) -> None:
        """Refresh folder tables after folder deletion events."""

        self._view.set_folder_rows(self._store.get_folder_summaries())

    @Slot(list)
    def _on_duplicate_folders_detected(self, folder_paths: list[str]) -> None:
        """Accumulate duplicate paths so one user action produces one notice."""

        for folder_path in folder_paths:
            normalized = str(folder_path or "").strip()
            if normalized:
                self._duplicate_folder_paths.setdefault(normalized, None)

    @Slot(str)
    def _on_task_updated(self, request_id: str) -> None:
        self._dirty_task_ids.add(request_id)

    @Slot(dict)
    def _on_overall_updated(self, stats: dict[str, float | int | None]) -> None:
        self._latest_overall_stats = dict(stats)

    @Slot()
    def _flush_ui_updates(self) -> None:
        folder_paths = self._dirty_folder_paths
        self._dirty_folder_paths = set()
        for folder_path in folder_paths:
            summary = self._store.get_folder_summary(folder_path)
            if summary is not None:
                self._view.upsert_folder_row(summary)

        task_ids = self._dirty_task_ids
        self._dirty_task_ids = set()
        if self._selected_folder:
            for request_id in task_ids:
                task = self._store.get_task(request_id)
                if task is not None and task.folder_path == self._selected_folder:
                    self._view.update_image_task(task)

        if self._latest_overall_stats is not None:
            self._view.set_overall_stats(self._latest_overall_stats)
            self._latest_overall_stats = None

    @Slot()
    def _on_store_reset(self) -> None:
        self._duplicate_folder_paths.clear()
        self._view.clear_progress_views()

    def _stop_polling_only(self, reason: str) -> None:
        if self._poll_worker:
            self._poll_worker.stop()
            self._log(reason)

    def _stop_workers(self, reason: str) -> None:
        self._safe_stop_worker(self._discovery_worker)
        self._safe_quit_thread(self._discovery_thread)
        self._discovery_worker = None
        self._discovery_thread = None
        for worker in list(self._scan_workers.values()):
            self._safe_stop_worker(worker)
        for thread in list(self._scan_threads.values()):
            self._safe_quit_thread(thread)
        self._scan_workers.clear()
        self._scan_threads.clear()
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
        if self._lazy_mode:
            self._store.release_claimed()  # type: ignore[attr-defined]

        self._active = False
        self._publish_finished = True
        self._active_result_queue = None
        self._view.set_active_result_queue(None)
        self._view.set_running_state(False)
        self._sync_runtime_options_enabled()
        self._log(reason)

    def _stop_queue_metrics_monitor(self) -> None:
        """Stop request-queue metrics monitor thread gracefully."""

        self._safe_stop_worker(self._queue_metrics_worker)
        self._safe_quit_thread(self._queue_metrics_thread)
        self._queue_metrics_worker = None
        self._queue_metrics_thread = None

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

    def _ensure_resolved_result_queue(self) -> str:
        """Resolve and cache the final result queue name for this app session."""

        if self._resolved_result_queue:
            return self._resolved_result_queue

        if self._config.mock_mode:
            try:
                local_ipv4 = resolve_local_ipv4(self._config.rabbitmq)
            except RuntimeError:
                local_ipv4 = "127.0.0.1"
        else:
            local_ipv4 = resolve_local_ipv4(self._config.rabbitmq)

        resolved_queue = resolve_result_queue_name(
            result_queue_base=self._config.rabbitmq.result_queue_base,
            local_ipv4=local_ipv4,
        )
        self._resolved_local_ipv4 = local_ipv4
        self._resolved_result_queue = resolved_queue
        return resolved_queue

    def _resolve_local_ipv4_for_preview(self) -> str | None:
        """Best-effort local IPv4 lookup for MQ preview before a session starts."""

        if self._resolved_local_ipv4:
            return self._resolved_local_ipv4

        try:
            self._ensure_resolved_result_queue()
        except RuntimeError:
            return "127.0.0.1" if self._config.mock_mode else None
        return self._resolved_local_ipv4

    def _safe_update_poll_tracked_ids(
        self,
        add_request_ids: list[str] | None = None,
        remove_request_ids: list[str] | None = None,
    ) -> None:
        """Safely synchronize tracked request IDs with the polling worker if it exists."""

        if self._poll_worker is None:
            return
        try:
            if add_request_ids:
                self._poll_worker.add_tracked_request_ids(add_request_ids)
            if remove_request_ids:
                self._poll_worker.remove_tracked_request_ids(remove_request_ids)
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

    def _set_runtime_options_locked(self, locked: bool) -> None:
        """Persist and propagate recipe/priority edit lock state."""

        self._runtime_options_locked = bool(locked)
        self._sync_runtime_options_enabled()

    def _sync_runtime_options_enabled(self) -> None:
        """Reflect current lock state to view controls."""

        self._view.set_runtime_options_enabled(not self._runtime_options_locked)

    def _register_selected_folders(self, folder_paths: list[str], include_subfolders: bool) -> None:
        """Scan/register folders from multi-selection and optionally enqueue during run."""

        normalized_paths = self._dedupe_paths(folder_paths)
        if not normalized_paths:
            self._log("선택된 폴더가 없습니다.")
            return

        recipe_selections = self._current_recipe_selections()
        if not recipe_selections:
            self._log("선택된 Recipe가 없습니다. Recipe를 하나 이상 선택한 뒤 폴더를 추가하세요.")
            return

        if self._lazy_mode:
            self._register_lazy_folders(
                normalized_paths,
                recipe_selections,
                include_subfolders=include_subfolders,
            )
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

        added_folders, added_images = self._store.register_folder_map(
            folder_map,
            recipe_selections=recipe_selections,
        )
        mode_label = "sub_folder" if include_subfolders else "폴더"
        self._log(
            f"{mode_label} 등록 완료 - 스캔 대상 {len(normalized_paths)}개, "
            f"Recipe {len(recipe_selections)}개, 신규 폴더 {added_folders}개, 신규 작업 {added_images}개"
        )
        self._show_duplicate_folder_notice()

        if not self._active or added_images <= 0:
            return

        runtime_action, runtime_recipe_path, _, runtime_priority = self._view.current_runtime_settings()
        runtime_action = runtime_action or self._config.publish.default_action
        runtime_recipe_path = runtime_recipe_path or self._config.recipe_config.default_path
        runtime_queue = self._active_result_queue or self._resolved_result_queue
        if not runtime_queue:
            try:
                runtime_queue = self._ensure_resolved_result_queue()
            except RuntimeError as exc:
                self._log(f"실행 중 추가를 위한 결과 큐 이름 결정 실패: {exc}")
                return
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
        self._safe_update_poll_tracked_ids(
            add_request_ids=[
                message.request_id
                for _, messages in grouped_messages
                for message in messages
            ]
        )

        self._log(
            f"실행 중 추가 편입 - 폴더 {len(grouped_messages)}개, 메시지 {added_messages}건 "
            f"(active={len(self._active_folder_paths)}, remaining_batches="
            f"{len(self._folder_message_batches) - self._next_folder_batch_index})"
        )
        self._maybe_dispatch_next_folder_batch()

    def _register_lazy_folders(
        self,
        folder_paths: list[str],
        recipe_selections: list[tuple[str, str]],
        include_subfolders: bool,
    ) -> None:
        """Register folder descriptors only; image enumeration happens when opened."""

        if include_subfolders:
            self._start_folder_discovery(folder_paths, recipe_selections)
            return

        descriptor_paths: list[str] = []
        seen: set[str] = set()
        for folder_path in folder_paths:
            candidates = [folder_path] if Path(folder_path).is_dir() else []
            for candidate in candidates:
                normalized = str(Path(candidate))
                if normalized in seen:
                    continue
                seen.add(normalized)
                descriptor_paths.append(normalized)

        if not descriptor_paths:
            self._log("등록 가능한 이미지 폴더를 찾지 못했습니다.")
            return

        added = self._store.register_folder_descriptors(  # type: ignore[attr-defined]
            descriptor_paths,
            recipe_selections,
        )
        self._log(
            f"폴더 대기열 등록 완료 - 폴더 {added}개, "
            f"Recipe {len(recipe_selections)}개 (이미지는 활성 시점에 분할 스캔)"
        )
        self._show_duplicate_folder_notice()
        if self._active and added:
            self._open_next_lazy_folders(self._available_open_slots())
            self._maybe_dispatch_lazy()

    def _start_folder_discovery(
        self,
        root_paths: list[str],
        recipe_selections: list[tuple[str, str]],
    ) -> None:
        if self._discovery_thread is not None:
            try:
                if self._discovery_thread.isRunning():
                    self._log("하위 폴더 탐색이 이미 진행 중입니다.")
                    return
            except RuntimeError:
                self._discovery_thread = None
                self._discovery_worker = None

        thread = QThread(self)
        worker = FolderDiscoveryWorker(
            scanner=self._scanner,
            root_paths=root_paths,
            mode=self._config.publish.scan_mode,
            recipe_selections=recipe_selections,
            register_batch=self._store.register_folder_descriptors,  # type: ignore[attr-defined]
            batch_size=100,
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.batch_registered.connect(self._on_discovery_batch_registered)
        worker.discovery_completed.connect(self._on_discovery_completed)
        worker.discovery_failed.connect(self._on_discovery_failed)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._on_discovery_thread_finished)
        self._discovery_thread = thread
        self._discovery_worker = worker
        thread.start()
        self._log("하위 이미지 폴더 탐색을 백그라운드에서 시작했습니다.")

    @Slot(int, int)
    def _on_discovery_batch_registered(self, added: int, total_added: int) -> None:
        _ = added
        if self._active:
            self._open_next_lazy_folders(self._available_open_slots())
            self._maybe_dispatch_lazy()
        if total_added and total_added % 500 == 0:
            self._log(f"하위 폴더 탐색 진행 - {total_added}개 등록")

    @Slot(int)
    def _on_discovery_completed(self, total_added: int) -> None:
        self._log(f"하위 폴더 탐색 완료 - 신규 폴더 {total_added}개")
        self._show_duplicate_folder_notice()
        if self._start_after_discovery:
            self._start_after_discovery = False
            self._start_lazy_session()
            return
        if self._active:
            self._open_next_lazy_folders(self._available_open_slots())
            self._maybe_dispatch_lazy()

    @Slot(str)
    def _on_discovery_failed(self, error: str) -> None:
        self._log(f"하위 폴더 탐색 실패: {error}")
        self._show_duplicate_folder_notice()

    def _show_duplicate_folder_notice(self) -> None:
        """Show accumulated duplicates with their current folder-table location."""

        if not self._duplicate_folder_paths:
            return

        rows: list[tuple[str, str]] = []
        for folder_path in self._duplicate_folder_paths:
            summary = self._store.get_folder_summary(folder_path)
            location = (
                "완료된 폴더"
                if summary is not None and summary.status.is_done
                else "진행중/대기 폴더"
            )
            rows.append((folder_path, location))

        self._duplicate_folder_paths.clear()
        self._log(f"중복 폴더 {len(rows)}개는 이미 등록되어 추가하지 않았습니다.")
        self._view.show_duplicate_folders(rows)

    @Slot()
    def _on_discovery_thread_finished(self) -> None:
        self._discovery_thread = None
        self._discovery_worker = None

    def _start_lazy_session(self, runtime_override: dict[str, object] | None = None) -> None:
        """Start disk-backed scanning and chunked publishing without prebuilding messages."""

        folder_paths = self._store.get_folder_paths()
        if not folder_paths and not self._store.has_inflight_tasks():
            if self._discovery_thread is not None:
                try:
                    if self._discovery_thread.isRunning():
                        self._start_after_discovery = True
                        self._log("하위 폴더 탐색 완료 후 전송을 자동 시작합니다.")
                        return
                except RuntimeError:
                    pass
            self._log("전송할 폴더가 없습니다.")
            return

        action, recipe_path, polling_interval, priority = self._view.current_runtime_settings()
        restored_queue = ""
        if runtime_override:
            action = str(runtime_override.get("action") or action)
            polling_interval = int(runtime_override.get("polling_interval") or polling_interval)
            priority = int(runtime_override.get("priority") or 0)
            restored_queue = str(runtime_override.get("result_queue") or "")
        if not action:
            self._log("Action 값이 비어 있습니다.")
            return
        if not recipe_path:
            self._log("Recipe Path 값이 비어 있습니다.")
            return
        try:
            queue_name = restored_queue or self._ensure_resolved_result_queue()
        except RuntimeError as exc:
            self._view.set_connection_status(False, "결과 큐 결정 실패")
            self._log(f"결과 큐 이름 결정 실패: {exc}")
            return

        self._reset_publish_schedule_state()
        self._lazy_action = action
        self._lazy_priority = priority
        self._active_timeout_seconds = self._config.publish.timeout_seconds
        self._pending_polling_interval = polling_interval
        self._active_result_queue = queue_name
        self._store.save_runtime_settings(  # type: ignore[attr-defined]
            action,
            queue_name,
            priority,
            polling_interval,
        )
        self._publish_exchange, self._publish_routing_key = resolve_publish_route(self._config.rabbitmq)
        self._active = True
        self._publish_finished = True
        self._view.set_active_result_queue(queue_name)
        self._view.set_running_state(True)
        self._set_runtime_options_locked(True)

        if self._store.has_inflight_tasks() and not self._is_poll_worker_running():
            self._start_polling_worker(
                queue_name=queue_name,
                polling_interval=polling_interval,
                tracked_request_ids=None,
            )

        self._log(
            f"전체 작업 집계 시작 - 최대 동시 스캔 {self._max_active_open_folders}개, "
            f"메모리 적재 없이 SQLite에 청크 저장"
        )
        self._inventory_preparing = True
        self._continue_lazy_inventory()

    @Slot()
    def _resume_persisted_lazy_session(self) -> None:
        """Automatically continue a disk-backed session restored at application startup."""

        if self._active or not self._store.should_auto_resume():  # type: ignore[attr-defined]
            return
        self._log("미완료 대용량 세션을 복구하여 자동으로 재개합니다.")
        self._start_lazy_session(self._store.get_runtime_settings())  # type: ignore[attr-defined]

    def _open_next_lazy_folders(self, count: int) -> None:
        """Activate the next already-inventoried folders in display order."""

        if count <= 0:
            return
        candidates = self._store.get_active_candidate_folder_paths()  # type: ignore[attr-defined]
        opened = 0
        for folder_path in candidates:
            if opened >= count:
                break
            if folder_path in self._opened_folder_paths:
                continue
            summary = self._store.get_folder_summary(folder_path)
            if summary is None or summary.status.is_done or summary.total <= 0:
                continue
            self._opened_folder_paths.add(folder_path)
            self._active_folder_paths.add(folder_path)
            opened += 1

    def _continue_lazy_inventory(self) -> None:
        """Scan every waiting folder with bounded concurrency before publishing."""

        if not self._inventory_preparing or not self._active:
            return

        while True:
            waiting = self._store.get_waiting_folder_paths()  # type: ignore[attr-defined]
            available = max(0, self._max_active_open_folders - len(self._scan_threads))
            if not waiting or available <= 0:
                break

            launched_async = False
            for folder_path in waiting[:available]:
                self._store.set_folder_scan_state(folder_path, "SCANNING")  # type: ignore[attr-defined]
                self._start_scan_worker(folder_path)
                launched_async = launched_async or folder_path in self._scan_threads
            if launched_async:
                break

        if self._store.get_waiting_folder_paths() or self._scan_threads:  # type: ignore[attr-defined]
            return

        self._inventory_preparing = False
        self._planned_publish_total = int(self._store.overall_stats()["total"] or 0)
        self._open_next_lazy_folders(self._max_initial_open_folders)
        self._log(
            f"전체 작업 집계 완료 - 총 {self._planned_publish_total}건, "
            f"화면 폴더 순서대로 전송 시작"
        )
        self._log(
            f"대용량 전송 - Chunk {self._config.publish.publish_chunk_size}건, "
            f"queue 상한 {self._config.publish.fallback_max_queued_messages}건"
        )
        self._maybe_dispatch_lazy()

    def _start_scan_worker(self, folder_path: str) -> None:
        if folder_path in self._scan_threads:
            return
        thread = QThread(self)
        worker = ScanWorker(
            scanner=self._scanner,
            folder_path=folder_path,
            insert_batch=self._store.insert_task_batch,  # type: ignore[attr-defined]
            batch_size=min(1000, max(500, int(self._config.publish.publish_chunk_size))),
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.batch_inserted.connect(self._on_scan_batch_inserted)
        worker.scan_completed.connect(self._on_scan_completed)
        worker.scan_stopped.connect(self._on_scan_stopped)
        worker.scan_failed.connect(self._on_scan_failed)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(lambda path=folder_path: self._on_scan_thread_finished(path))
        self._scan_threads[folder_path] = thread
        self._scan_workers[folder_path] = worker
        thread.start()

    @Slot(str, int, int)
    def _on_scan_batch_inserted(self, folder_path: str, inserted: int, total_inserted: int) -> None:
        _ = folder_path, inserted, total_inserted
        self._planned_publish_total = int(self._store.overall_stats()["total"] or 0)
        if not self._inventory_preparing:
            self._maybe_dispatch_lazy()

    @Slot(str, int)
    def _on_scan_completed(self, folder_path: str, total_inserted: int) -> None:
        summary = self._store.get_folder_summary(folder_path)
        self._store.set_folder_scan_state(  # type: ignore[attr-defined]
            folder_path,
            "SCANNED" if total_inserted or (summary is not None and summary.total) else "EMPTY",
        )
        self._log(f"폴더 분할 스캔 완료 - {folder_path}: Task {total_inserted}건")
        if not self._inventory_preparing:
            self._maybe_dispatch_lazy()

    @Slot(str, int)
    def _on_scan_stopped(self, folder_path: str, total_inserted: int) -> None:
        self._store.set_folder_scan_state(folder_path, "WAITING")  # type: ignore[attr-defined]
        self._active_folder_paths.discard(folder_path)
        self._log(f"폴더 스캔 중지 - {folder_path}: 이번 실행에서 Task {total_inserted}건 저장")

    @Slot(str, str)
    def _on_scan_failed(self, folder_path: str, error: str) -> None:
        self._store.set_folder_scan_state(folder_path, "ERROR")  # type: ignore[attr-defined]
        self._active_folder_paths.discard(folder_path)
        self._log(f"폴더 스캔 실패 - {folder_path}: {error}")
        if not self._inventory_preparing:
            self._open_next_lazy_folders(self._available_open_slots())

    def _on_scan_thread_finished(self, folder_path: str) -> None:
        self._scan_threads.pop(folder_path, None)
        self._scan_workers.pop(folder_path, None)
        if self._inventory_preparing:
            self._continue_lazy_inventory()

    def _lazy_queue_high_watermark(self) -> int:
        fallback = max(1, int(self._config.publish.fallback_max_queued_messages))
        chunk_size = max(1, int(self._config.publish.publish_chunk_size))
        if self._last_worker_count is None:
            return fallback
        if self._last_worker_count <= 0:
            return min(fallback, chunk_size)
        return min(fallback, max(chunk_size, self._last_worker_count * chunk_size))

    def _maybe_dispatch_lazy(self) -> None:
        """Publish only one bounded chunk when scan and broker capacity allow it."""

        if (
            not self._lazy_mode
            or not self._active
            or self._inventory_preparing
            or self._is_publish_worker_running()
        ):
            return
        freed_slots = self._refresh_lazy_active_folders()
        if freed_slots:
            self._open_next_lazy_folders(freed_slots)
        self._maybe_expand_lazy_folders()
        high_watermark = self._lazy_queue_high_watermark()
        queued = self._last_queue_message_count
        outstanding = self._store.inflight_count()  # type: ignore[attr-defined]
        pressure = max(queued or 0, outstanding)
        if pressure >= high_watermark:
            return

        capacity = max(1, int(self._config.publish.publish_chunk_size))
        capacity = min(capacity, max(0, high_watermark - pressure))
        if capacity <= 0:
            return

        active_folders = list(self._active_folder_paths)
        messages = self._store.claim_pending_messages(  # type: ignore[attr-defined]
            active_folders,
            self._lazy_action,
            self._active_result_queue or self._ensure_resolved_result_queue(),
            self._lazy_priority,
            capacity,
        )
        if messages:
            self._publish_finished = False
            self._start_publish_worker(
                messages,
                self._publish_exchange,
                self._publish_routing_key,
            )
            return

        if self._scan_threads:
            return
        if self._store.has_pending_tasks() or self._store.has_inflight_tasks():
            return
        if self._store.get_waiting_folder_paths():  # type: ignore[attr-defined]
            return

        self._publish_finished = True
        if self._is_poll_worker_running():
            self._stop_polling_only("모든 대용량 작업이 완료되어 polling을 종료합니다.")
        else:
            self._active = False
            self._view.set_running_state(False)
            self._store.disable_auto_resume()  # type: ignore[attr-defined]
            self._set_runtime_options_locked(False)
            self._log("모든 대용량 작업이 완료되었습니다.")

    def _refresh_lazy_active_folders(self) -> int:
        before = len(self._active_folder_paths)
        for folder_path in list(self._active_folder_paths):
            scan_state = self._store.folder_scan_state(folder_path)  # type: ignore[attr-defined]
            summary = self._store.get_folder_summary(folder_path)
            if scan_state in {"EMPTY", "ERROR"} or (
                scan_state == "SCANNED" and summary is not None and summary.status.is_done
            ):
                self._active_folder_paths.discard(folder_path)
        return min(
            before - len(self._active_folder_paths),
            self._available_open_slots(),
        )

    def _maybe_expand_lazy_folders(self) -> None:
        if len(self._active_folder_paths) >= self._max_active_open_folders:
            return
        unopened = [
            path
            for path in self._store.get_active_candidate_folder_paths()  # type: ignore[attr-defined]
            if path not in self._opened_folder_paths
        ]
        if not unopened:
            return
        if any(
            (summary := self._store.get_folder_summary(folder_path)) is not None
            and summary.progress >= self._next_open_threshold
            for folder_path in self._active_folder_paths
        ):
            self._open_next_lazy_folders(1)

    def _current_recipe_selections(self) -> list[tuple[str, str]]:
        """Return the view's current recipe selection as an immutable snapshot."""

        selection_reader = getattr(self._view, "current_recipe_selections", None)
        if callable(selection_reader):
            selections = selection_reader()
        else:
            _, recipe_path, _, _ = self._view.current_runtime_settings()
            selections = [(recipe_path, recipe_path)] if recipe_path else []

        normalized: list[tuple[str, str]] = []
        seen_paths: set[str] = set()
        for alias, path in selections:
            normalized_alias = str(alias or "").strip()
            normalized_path = str(path or "").strip()
            if not normalized_path or normalized_path in seen_paths:
                continue
            seen_paths.add(normalized_path)
            normalized.append((normalized_alias or normalized_path, normalized_path))
        return normalized

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

        self._inventory_preparing = False
        self._folder_message_batches = []
        self._next_folder_batch_index = 0
        self._opened_folder_paths.clear()
        self._active_folder_paths.clear()
        self._scheduled_request_ids.clear()
        self._planned_publish_total = 0
        self._published_count = 0
        self._last_publish_progress_log_at = 0.0
        self._last_publish_progress_count = 0
        self._result_applied_count = 0
        self._last_result_progress_log_at = 0.0
        self._last_result_progress_count = 0
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
        """Dispatch next folder batches with anti-stall force-open and ramp-up policies."""

        remaining_batches = self._synchronize_dispatch_state()
        publish_running = self._is_publish_worker_running()
        if remaining_batches <= 0:
            self._last_dispatch_skip_reason = None
            return
        if publish_running:
            self._log_dispatch_skip(
                reason="publish worker running",
                remaining_batches=remaining_batches,
                available_slots=self._available_open_slots(),
                publish_running=publish_running,
            )
            return

        available_slots = self._available_open_slots()
        if available_slots <= 0:
            self._log_dispatch_skip(
                reason=f"active cap reached({self._max_active_open_folders})",
                remaining_batches=remaining_batches,
                available_slots=available_slots,
                publish_running=publish_running,
            )
            return

        if len(self._active_folder_paths) == 0:
            self._dispatch_folder_batches(
                folder_count=min(available_slots, remaining_batches),
                trigger="force-open (anti-stall)",
                publish_running=publish_running,
            )
            return

        if self._should_backfill_slots():
            self._dispatch_folder_batches(
                folder_count=available_slots,
                trigger="slot refill",
                publish_running=publish_running,
            )
            return

        if not self._should_expand_by_threshold():
            self._log_dispatch_skip(
                reason="threshold unmet",
                remaining_batches=remaining_batches,
                available_slots=available_slots,
                publish_running=publish_running,
            )
            return

        self._dispatch_folder_batches(
            folder_count=1,
            trigger=f"threshold met({self._next_open_threshold:.0f}%)",
            publish_running=publish_running,
        )

    def _synchronize_dispatch_state(self) -> int:
        """Rebuild open/active tracking sets from scheduler index and store summaries."""

        total_batches = len(self._folder_message_batches)
        if total_batches == 0:
            self._next_folder_batch_index = 0
            self._opened_folder_paths.clear()
            self._active_folder_paths.clear()
            return 0

        self._next_folder_batch_index = min(max(self._next_folder_batch_index, 0), total_batches)
        batch_folder_paths = [folder_path for folder_path, _ in self._folder_message_batches]
        batch_folder_set = set(batch_folder_paths)

        opened_candidates = set(batch_folder_paths[: self._next_folder_batch_index])
        opened_candidates.update(path for path in self._opened_folder_paths if path in batch_folder_set)

        refreshed_opened: set[str] = set()
        refreshed_active: set[str] = set()
        for folder_path in opened_candidates:
            summary = self._store.get_folder_summary(folder_path)
            if summary is None:
                continue
            refreshed_opened.add(folder_path)
            if not summary.status.is_done:
                refreshed_active.add(folder_path)

        # Recover from transient set loss by treating currently-running folders as opened+active.
        for folder_path in batch_folder_set:
            if folder_path in refreshed_active:
                continue
            summary = self._store.get_folder_summary(folder_path)
            if summary is None or summary.status != TaskStatus.RUNNING:
                continue
            refreshed_opened.add(folder_path)
            refreshed_active.add(folder_path)

        self._opened_folder_paths = refreshed_opened
        self._active_folder_paths = refreshed_active
        return max(0, total_batches - self._next_folder_batch_index)

    def _is_publish_worker_running(self) -> bool:
        """Return whether one publish worker thread is still active."""

        if self._publish_thread is None:
            return False
        try:
            running = self._publish_thread.isRunning()
        except RuntimeError:
            self._publish_thread = None
            self._publish_worker = None
            return False
        if not running and self._publish_finished:
            self._publish_thread = None
            self._publish_worker = None
        return running

    def _is_poll_worker_running(self) -> bool:
        """Return whether one polling worker thread is still active."""

        if self._poll_thread is None:
            return False
        try:
            running = self._poll_thread.isRunning()
        except RuntimeError:
            self._poll_thread = None
            self._poll_worker = None
            return False
        if not running:
            self._poll_thread = None
            self._poll_worker = None
        return running

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

    def _dispatch_folder_batches(self, folder_count: int, trigger: str, publish_running: bool) -> None:
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
            f"available_slots={self._available_open_slots()}, "
            f"next_index={self._next_folder_batch_index}, "
            f"publish_running={publish_running})"
        )

    def _log_dispatch_skip(
        self,
        reason: str,
        remaining_batches: int,
        available_slots: int,
        publish_running: bool,
    ) -> None:
        """Log dispatch skip reason with deduplication to avoid noisy logs."""

        dedupe_key = (
            f"{reason}|active={len(self._active_folder_paths)}|opened={len(self._opened_folder_paths)}"
            f"|remaining={remaining_batches}|slots={available_slots}|idx={self._next_folder_batch_index}"
            f"|running={publish_running}"
        )
        if dedupe_key == self._last_dispatch_skip_reason:
            return
        self._last_dispatch_skip_reason = dedupe_key
        self._log(
            "밸런싱 대기 - "
            f"{reason} (active_count={len(self._active_folder_paths)}, "
            f"opened_count={len(self._opened_folder_paths)}, "
            f"remaining_batches={remaining_batches}, "
            f"available_slots={available_slots}, "
            f"next_index={self._next_folder_batch_index}, "
            f"publish_running={publish_running})"
        )

    def _warn_if_recipe_file_missing(self, recipe_path: str) -> None:
        """Warn when the selected recipe file is not visible on the local machine.

        The MQ payload still uses the configured recipe path as-is because some
        deployments intentionally reference recipe files on the worker side.
        """

        normalized_recipe_path = str(recipe_path or "").strip()
        if not normalized_recipe_path:
            return

        candidate = Path(normalized_recipe_path)
        if not candidate.is_absolute():
            recipe_config_file = str(self._config.recipe_config_path or "").strip()
            if recipe_config_file:
                candidate = Path(recipe_config_file).expanduser().resolve().parent / candidate

        if candidate.exists():
            return

        self._log(
            "선택한 recipe 파일이 로컬에서 보이지 않습니다: "
            f"{candidate} (전송 payload 에는 configured path '{normalized_recipe_path}' 그대로 사용)"
        )
