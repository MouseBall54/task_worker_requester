"""Centralized in-memory task state store."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from config.models import AppConfig
from models.task_models import (
    FolderSummary,
    FolderTaskGroup,
    ImageTask,
    TaskMessage,
    TaskResult,
    TaskStatus,
)
from services.broker.routing import resolve_publish_route
from utils.qt_compat import QObject, Signal


class TaskStore(QObject):
    """Owns all runtime task/group states and emits update signals."""

    folder_group_added = Signal(str)
    folder_group_updated = Signal(str)
    task_updated = Signal(str)
    store_reset = Signal()
    overall_updated = Signal(dict)

    def __init__(self) -> None:
        super().__init__()
        self._tasks: dict[str, ImageTask] = {}
        self._groups: dict[str, FolderTaskGroup] = {}
        self._folder_order: list[str] = []
        self._processed_request_ids: set[str] = set()
        self._folder_image_index: dict[str, set[str]] = {}

    def reset(self) -> None:
        """Clear every runtime state item."""

        self._tasks.clear()
        self._groups.clear()
        self._folder_order.clear()
        self._processed_request_ids.clear()
        self._folder_image_index.clear()
        self.store_reset.emit()
        self._emit_overall()

    def register_folder_map(self, folder_map: dict[str, list[str]]) -> tuple[int, int]:
        """Register image tasks grouped by folder.

        Returns
        -------
        tuple[int, int]
            Added folder count, added image count.
        """

        added_folders = 0
        added_images = 0

        for folder_path, image_paths in folder_map.items():
            if not image_paths:
                continue

            if folder_path not in self._groups:
                self._groups[folder_path] = FolderTaskGroup(folder_path=folder_path)
                self._folder_order.append(folder_path)
                self._folder_image_index[folder_path] = set()
                added_folders += 1
                self.folder_group_added.emit(folder_path)

            group = self._groups[folder_path]
            index = self._folder_image_index[folder_path]

            for image_path in image_paths:
                if image_path in index:
                    continue

                request_id = str(uuid4())
                task = ImageTask(
                    request_id=request_id,
                    image_path=image_path,
                    folder_path=folder_path,
                )
                self._tasks[request_id] = task
                group.task_ids.append(request_id)
                index.add(image_path)
                added_images += 1
                self.task_updated.emit(request_id)

            self.folder_group_updated.emit(folder_path)

        if added_folders or added_images:
            self._emit_overall()

        return added_folders, added_images

    def build_pending_messages(
        self,
        action: str,
        result_queue_name: str,
        recipe_path: str,
    ) -> list[TaskMessage]:
        """Create outbound task messages for not-yet-sent requests."""

        messages: list[TaskMessage] = []
        for task in self._tasks.values():
            if task.status != TaskStatus.PENDING:
                continue
            messages.append(
                TaskMessage(
                    request_id=task.request_id,
                    action=action,
                    QUEUE_NAME=result_queue_name,
                    RECIPE_PATH=recipe_path,
                    IMG_LIST=[task.image_path],
                )
            )
        return messages

    def build_pending_messages_by_folder(
        self,
        action: str,
        result_queue_name: str,
        recipe_path: str,
    ) -> list[tuple[str, list[TaskMessage]]]:
        """Create outbound messages grouped by folder in stable folder order."""

        grouped: list[tuple[str, list[TaskMessage]]] = []
        for folder_path in self._folder_order:
            group = self._groups.get(folder_path)
            if group is None:
                continue

            messages: list[TaskMessage] = []
            for task_id in group.task_ids:
                task = self._tasks.get(task_id)
                if task is None or task.status != TaskStatus.PENDING:
                    continue
                messages.append(
                    TaskMessage(
                        request_id=task.request_id,
                        action=action,
                        QUEUE_NAME=result_queue_name,
                        RECIPE_PATH=recipe_path,
                        IMG_LIST=[task.image_path],
                    )
                )

            if messages:
                grouped.append((folder_path, messages))

        return grouped

    def mark_task_sent(self, request_id: str) -> None:
        """Update state when one request publish succeeds."""

        task = self._tasks.get(request_id)
        if not task or task.status != TaskStatus.PENDING:
            return
        task.status = TaskStatus.SENT
        task.sent_at = datetime.now(timezone.utc)
        self.task_updated.emit(request_id)
        self.folder_group_updated.emit(task.folder_path)
        self._emit_overall()

    def mark_inflight_running(self) -> int:
        """Promote SENT tasks to RUNNING while waiting for results."""

        changed_count = 0
        changed_folders: set[str] = set()

        for task in self._tasks.values():
            if task.status != TaskStatus.SENT:
                continue
            if task.status.is_done:
                continue

            task.status = TaskStatus.RUNNING
            changed_count += 1
            changed_folders.add(task.folder_path)
            self.task_updated.emit(task.request_id)

        for folder_path in changed_folders:
            self.folder_group_updated.emit(folder_path)

        if changed_count:
            self._emit_overall()

        return changed_count

    def set_task_expected_message(
        self,
        request_id: str,
        payload: dict[str, Any],
        meta: dict[str, str] | None = None,
    ) -> None:
        """Store expected outbound message snapshot for preview UI."""

        task = self._tasks.get(request_id)
        if not task:
            return
        task.expected_message = dict(payload)
        if meta:
            task.publish_meta.update(meta)
        self.task_updated.emit(request_id)

    def set_task_published_message(
        self,
        request_id: str,
        payload: dict[str, Any],
        meta: dict[str, str] | None = None,
    ) -> None:
        """Store published message snapshot/metadata for preview UI."""

        task = self._tasks.get(request_id)
        if not task:
            return
        task.published_message = dict(payload)
        if meta:
            task.publish_meta.update(meta)
        self.task_updated.emit(request_id)

    def set_task_received_message(
        self,
        request_id: str,
        payload: dict[str, Any],
        meta: dict[str, str] | None = None,
    ) -> None:
        """Store first-matched inbound raw message snapshot for preview UI."""

        task = self._tasks.get(request_id)
        if not task:
            return

        # Keep first matched raw message for stable forensic preview.
        if task.received_message is not None:
            return

        task.received_message = dict(payload)
        if meta:
            task.received_meta.update(meta)
        self.task_updated.emit(request_id)

    def mark_task_error(self, request_id: str, message: str) -> None:
        """Mark task as failed during publish/processing errors."""

        task = self._tasks.get(request_id)
        if not task:
            return
        if task.status.is_done:
            return

        task.status = TaskStatus.ERROR
        task.error_message = message
        task.completed_at = datetime.now(timezone.utc)
        self.task_updated.emit(request_id)
        self.folder_group_updated.emit(task.folder_path)
        self._emit_overall()

    def apply_result(self, task_result: TaskResult) -> bool:
        """Merge one inbound result into task state.

        Returns
        -------
        bool
            True when state changed, False for duplicate/unknown.
        """

        request_id = task_result.request_id
        if request_id in self._processed_request_ids:
            return False

        task = self._tasks.get(request_id)
        if not task:
            return False

        if task.status.is_done:
            self._processed_request_ids.add(request_id)
            return False

        task.result = task_result.result
        task.error_message = task_result.error
        task.completed_at = self._parse_completed_at(task_result.completed_at)

        if task_result.is_success:
            task.status = TaskStatus.SUCCESS
        else:
            task.status = TaskStatus.FAIL

        self._processed_request_ids.add(request_id)
        self.task_updated.emit(request_id)
        self.folder_group_updated.emit(task.folder_path)
        self._emit_overall()
        return True

    def mark_timeouts(self, timeout_seconds: int) -> list[str]:
        """Mark non-terminal sent tasks as timeout after threshold."""

        now = datetime.now(timezone.utc)
        timed_out_request_ids: list[str] = []

        for task in self._tasks.values():
            if task.status not in {TaskStatus.SENT, TaskStatus.RUNNING}:
                continue
            if task.sent_at is None:
                continue
            elapsed = (now - task.sent_at).total_seconds()
            if elapsed < timeout_seconds:
                continue

            task.status = TaskStatus.TIMEOUT
            task.completed_at = now
            task.error_message = "결과 수신 시간 초과"
            self._processed_request_ids.add(task.request_id)
            timed_out_request_ids.append(task.request_id)
            self.task_updated.emit(task.request_id)
            self.folder_group_updated.emit(task.folder_path)

        if timed_out_request_ids:
            self._emit_overall()

        return timed_out_request_ids

    def get_folder_summaries(self) -> list[FolderSummary]:
        """Return summaries in stable insertion order."""

        return [self._groups[path].to_summary(self._tasks) for path in self._folder_order]

    def get_folder_summary(self, folder_path: str) -> FolderSummary | None:
        """Return one folder summary by path."""

        group = self._groups.get(folder_path)
        if not group:
            return None
        return group.to_summary(self._tasks)

    def get_image_tasks(self, folder_path: str) -> list[ImageTask]:
        """Return image tasks for selected folder."""

        group = self._groups.get(folder_path)
        if not group:
            return []

        tasks = [self._tasks[task_id] for task_id in group.task_ids if task_id in self._tasks]
        return sorted(tasks, key=lambda task: task.image_path.lower())

    def get_task(self, request_id: str) -> ImageTask | None:
        """Return task by request ID."""

        return self._tasks.get(request_id)

    def build_mq_preview(
        self,
        request_id: str,
        app_config: AppConfig,
        active_result_queue: str | None,
        runtime_action: str | None = None,
        runtime_recipe_path: str | None = None,
    ) -> dict[str, Any] | None:
        """Build MQ preview payload for one request row."""

        task = self._tasks.get(request_id)
        if task is None:
            return None

        rabbitmq = app_config.rabbitmq
        publish_exchange, publish_routing_key = resolve_publish_route(rabbitmq)

        active_queue = (active_result_queue or "").strip()
        predicted_queue = active_queue or rabbitmq.result_queue_base
        resolved_action = (runtime_action or "").strip() or app_config.publish.default_action
        resolved_recipe_path = (runtime_recipe_path or "").strip() or app_config.recipe_config.default_path

        dynamic_expected_payload = {
            "request_id": task.request_id,
            "action": resolved_action,
            "QUEUE_NAME": predicted_queue,
            "RECIPE_PATH": resolved_recipe_path,
            "IMG_LIST": [task.image_path],
        }

        expected_payload = dict(task.expected_message) if task.expected_message else dynamic_expected_payload
        published_payload = dict(task.published_message or {})
        received_payload = dict(task.received_message or {})

        dynamic_publish_meta = {
            "exchange": publish_exchange,
            "routing_key": publish_routing_key,
            "reply_to": str(expected_payload.get("QUEUE_NAME", predicted_queue)),
            "message_id": task.request_id,
            "correlation_id": task.request_id,
            "content_type": "application/json",
        }
        publish_meta = {**dynamic_publish_meta, **dict(task.publish_meta)}

        return {
            "connection": {
                "host": rabbitmq.host,
                "port": rabbitmq.port,
                "virtual_host": rabbitmq.virtual_host,
                "request_exchange": rabbitmq.request_exchange,
                "request_routing_key": rabbitmq.request_routing_key,
                "request_queue": rabbitmq.request_queue,
                "result_queue_base": rabbitmq.result_queue_base,
                "request_queue_declare": asdict(rabbitmq.request_queue_declare),
                "result_queue_declare": asdict(rabbitmq.result_queue_declare),
                "active_result_queue": active_queue,
                "predicted_result_queue": predicted_queue,
            },
            "message": {
                "request_id": task.request_id,
                "status": task.status.value,
                "sent_at": self._datetime_to_str(task.sent_at),
                "completed_at": self._datetime_to_str(task.completed_at),
                "image_path": task.image_path,
                "publish_meta": publish_meta,
                "received_meta": dict(task.received_meta),
            },
            "payload": {
                "expected": expected_payload,
                "published": published_payload,
                "received": received_payload,
            },
        }

    def get_folder_paths(self) -> list[str]:
        """Return all tracked folder paths."""

        return list(self._folder_order)

    def has_pending_tasks(self) -> bool:
        """Return whether there are tasks not yet published."""

        return any(task.status == TaskStatus.PENDING for task in self._tasks.values())

    def all_tasks_terminal(self) -> bool:
        """Return True when every tracked task is in terminal state."""

        if not self._tasks:
            return False
        return all(task.status.is_done for task in self._tasks.values())

    def overall_stats(self) -> dict[str, float | int | None]:
        """Calculate total progress across all folders."""

        total = len(self._tasks)
        completed = sum(task.status.is_done for task in self._tasks.values())
        success = sum(task.status == TaskStatus.SUCCESS for task in self._tasks.values())
        fail = sum(task.status == TaskStatus.FAIL for task in self._tasks.values())
        timeout = sum(task.status == TaskStatus.TIMEOUT for task in self._tasks.values())
        error = sum(task.status == TaskStatus.ERROR for task in self._tasks.values())
        progress = (completed / total * 100.0) if total else 0.0

        first_sent_at: datetime | None = None
        now_utc = datetime.now(timezone.utc)

        for task in self._tasks.values():
            sent_at = self._to_utc_datetime(task.sent_at)
            if sent_at is None:
                continue

            if first_sent_at is None or sent_at < first_sent_at:
                first_sent_at = sent_at

        remaining = max(total - completed, 0)
        avg_processing_seconds: float | None = None
        eta_seconds: float | None = None

        if remaining == 0:
            eta_seconds = 0.0
        if first_sent_at is not None and completed > 0:
            elapsed_since_first_sent = (now_utc - first_sent_at).total_seconds()
            if elapsed_since_first_sent > 0:
                throughput = completed / elapsed_since_first_sent
                if throughput > 0:
                    avg_processing_seconds = elapsed_since_first_sent / completed
                    eta_seconds = remaining / throughput if remaining > 0 else 0.0

        return {
            "total": total,
            "completed": completed,
            "success": success,
            "fail": fail,
            "timeout": timeout,
            "error": error,
            "progress": progress,
            "avg_processing_seconds": avg_processing_seconds,
            "eta_seconds": eta_seconds,
        }

    def _emit_overall(self) -> None:
        """Emit aggregate progress to update global UI widgets."""

        self.overall_updated.emit(self.overall_stats())

    @staticmethod
    def _parse_completed_at(raw_completed_at: str | None) -> datetime:
        """Parse completed timestamp from message or fallback to now."""

        if not raw_completed_at:
            return datetime.now(timezone.utc)
        try:
            parsed = datetime.fromisoformat(raw_completed_at)
            normalized = TaskStore._to_utc_datetime(parsed)
            if normalized is not None:
                return normalized
            return datetime.now(timezone.utc)
        except ValueError:
            return datetime.now(timezone.utc)

    @staticmethod
    def _datetime_to_str(value: datetime | None) -> str:
        """Format datetime for preview output."""

        normalized = TaskStore._to_utc_datetime(value)
        if normalized is None:
            return ""
        return normalized.astimezone().isoformat()

    @staticmethod
    def _to_utc_datetime(value: datetime | None) -> datetime | None:
        """Normalize datetime to timezone-aware UTC for safe arithmetic."""

        if value is None:
            return None
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
