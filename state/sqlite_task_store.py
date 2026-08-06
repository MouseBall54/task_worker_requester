"""Qt-facing disk-backed task store used by the production runtime."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any

from config.models import AppConfig
from models.task_models import FolderSummary, ImageTask, TaskMessage, TaskResult, TaskStatus
from services.broker.result_queue import resolve_result_queue_name
from services.broker.routing import resolve_publish_route
from state.task_repository import TaskRepository
from utils.qt_compat import QObject, Signal


class SqliteTaskStore(QObject):
    """Expose task state through bounded SQLite queries and aggregate signals."""

    supports_lazy_loading = True

    folder_group_added = Signal(str)
    folder_group_updated = Signal(str)
    folder_group_removed = Signal(str)
    task_updated = Signal(str)
    store_reset = Signal()
    overall_updated = Signal(dict)

    def __init__(self, database_path: str | Path) -> None:
        super().__init__()
        self.repository = TaskRepository(database_path)
        self._first_sent_at: datetime | None = None

    def close(self) -> None:
        self.repository.close()

    def save_runtime_settings(
        self,
        action: str,
        result_queue: str,
        priority: int,
        polling_interval: int,
    ) -> None:
        self.repository.save_runtime_settings(
            action,
            result_queue,
            priority,
            polling_interval,
        )

    def get_runtime_settings(self) -> dict[str, Any] | None:
        return self.repository.get_runtime_settings()

    def reset(self) -> None:
        self.repository.clear_session()
        self._first_sent_at = None
        self.store_reset.emit()
        self._emit_overall()

    def register_folder_descriptors(
        self,
        folder_paths: list[str],
        recipe_selections: list[tuple[str, str]],
    ) -> int:
        before = set(self.get_folder_paths())
        added = self.repository.register_folder_descriptors(folder_paths, recipe_selections)
        for folder_path in self.get_folder_paths():
            if folder_path not in before:
                self.folder_group_added.emit(folder_path)
                self.folder_group_updated.emit(folder_path)
        if added:
            self._emit_overall()
        return added

    def insert_task_batch(self, folder_path: str, image_paths: list[str]) -> int:
        added = self.repository.insert_task_batch(folder_path, image_paths)
        if added:
            self.folder_group_updated.emit(folder_path)
            self._emit_overall()
        return added

    def set_folder_scan_state(self, folder_path: str, state: str) -> None:
        self.repository.set_folder_scan_state(folder_path, state)
        self.folder_group_updated.emit(folder_path)
        self._emit_overall()

    def folder_scan_state(self, folder_path: str) -> str | None:
        for row in self.repository.list_folder_descriptors():
            if str(row["folder_path"]) == folder_path:
                return str(row["scan_state"])
        return None

    def get_folder_paths(self) -> list[str]:
        return [str(row["folder_path"]) for row in self.repository.list_folder_descriptors()]

    def get_waiting_folder_paths(self) -> list[str]:
        return [
            str(row["folder_path"])
            for row in self.repository.list_folder_descriptors(states=["WAITING"])
        ]

    def get_active_candidate_folder_paths(self) -> list[str]:
        return [
            str(row["folder_path"])
            for row in self.repository.list_folder_descriptors(states=["SCANNING", "SCANNED"])
        ]

    def claim_pending_messages(
        self,
        folder_paths: list[str],
        action: str,
        result_queue_name: str,
        priority: int,
        limit: int,
    ) -> list[TaskMessage]:
        normalized_priority = max(0, int(priority))
        tasks = self.repository.claim_pending(
            folder_paths,
            limit,
            action=action,
            result_queue=result_queue_name,
            priority=normalized_priority,
        )
        messages: list[TaskMessage] = []
        for task in tasks:
            messages.append(
                TaskMessage(
                    request_id=task.request_id,
                    action=action,
                    QUEUE_NAME=result_queue_name,
                    RECIPE_PATH=task.recipe_path,
                    IMG_LIST=[task.image_path],
                    priority=normalized_priority,
                )
            )
        for folder_path in {task.folder_path for task in tasks}:
            self.folder_group_updated.emit(folder_path)
        if tasks:
            self._emit_overall()
        return messages

    def release_claimed(self) -> int:
        changed = self.repository.transition_all(TaskStatus.CLAIMED, TaskStatus.PENDING)
        if changed:
            self._emit_all_folders()
            self._emit_overall()
        return changed

    def mark_task_sent(self, request_id: str) -> None:
        sent_at = datetime.now(timezone.utc)
        changed = self.repository.transition_task(
            request_id,
            TaskStatus.SENT,
            {TaskStatus.CLAIMED, TaskStatus.PENDING},
            sent_at=sent_at.isoformat(),
        )
        if not changed:
            return
        if self._first_sent_at is None:
            self._first_sent_at = sent_at
        self._emit_task_and_folder(request_id)

    def mark_inflight_running(self) -> int:
        changed = self.repository.transition_all(TaskStatus.SENT, TaskStatus.RUNNING)
        if changed:
            self._emit_all_folders()
            self._emit_overall()
        return changed

    def set_task_expected_message(
        self,
        request_id: str,
        payload: dict[str, Any],
        meta: dict[str, Any] | None = None,
    ) -> None:
        _ = request_id, payload, meta

    def set_task_published_message(
        self,
        request_id: str,
        payload: dict[str, Any],
        meta: dict[str, Any] | None = None,
    ) -> None:
        _ = request_id, payload, meta

    def set_task_received_message(
        self,
        request_id: str,
        payload: dict[str, Any],
        meta: dict[str, Any] | None = None,
    ) -> None:
        row = self.repository.get_task_record(request_id)
        if row is None or row["received_json"]:
            return
        self.repository.update_task_fields(
            request_id,
            received_json=json.dumps(payload, ensure_ascii=False),
            received_meta_json=json.dumps(meta or {}, ensure_ascii=False),
        )
        self.task_updated.emit(request_id)

    def mark_task_error(self, request_id: str, message: str) -> None:
        changed = self.repository.transition_task(
            request_id,
            TaskStatus.ERROR,
            {TaskStatus.PENDING, TaskStatus.CLAIMED, TaskStatus.SENT, TaskStatus.RUNNING},
            completed_at=datetime.now(timezone.utc).isoformat(),
            error_message=message,
        )
        if changed:
            self._emit_task_and_folder(request_id)

    def apply_result(self, task_result: TaskResult) -> bool:
        target = TaskStatus.SUCCESS if task_result.is_success else TaskStatus.FAIL
        completed_at = task_result.completed_at or datetime.now(timezone.utc).isoformat()
        changed = self.repository.transition_task(
            task_result.request_id,
            target,
            {TaskStatus.SENT, TaskStatus.RUNNING},
            completed_at=completed_at,
            result_json=json.dumps(task_result.result, ensure_ascii=False),
            error_message=task_result.error,
        )
        if changed:
            self._emit_task_and_folder(task_result.request_id)
        return changed

    def mark_timeouts(self, timeout_seconds: int) -> list[str]:
        now = datetime.now(timezone.utc)
        request_ids = self.repository.timeout_before(
            (now - timedelta(seconds=max(1, int(timeout_seconds)))).isoformat(),
            now.isoformat(),
        )
        if request_ids:
            for request_id in request_ids:
                self.task_updated.emit(request_id)
            self._emit_all_folders()
            self._emit_overall()
        return request_ids

    def get_folder_summaries(self) -> list[FolderSummary]:
        return self.repository.get_folder_summaries()

    def get_folder_summary(self, folder_path: str) -> FolderSummary | None:
        return self.repository.get_folder_summary(folder_path)

    def get_image_tasks(self, folder_path: str, offset: int = 0, limit: int = 500) -> list[ImageTask]:
        return self.repository.get_tasks_page(folder_path, offset=offset, limit=limit)

    def get_task(self, request_id: str) -> ImageTask | None:
        return self.repository.get_task(request_id)

    def get_known_request_ids(self) -> set[str]:
        return self.repository.known_request_ids()

    def has_pending_tasks(self) -> bool:
        return self.repository.any_tasks([TaskStatus.PENDING, TaskStatus.CLAIMED])

    def has_inflight_tasks(self) -> bool:
        return self.repository.any_tasks([TaskStatus.SENT, TaskStatus.RUNNING])

    def inflight_count(self) -> int:
        """Return claimed or published tasks that still consume flow-control capacity."""

        return self.repository.count_tasks(
            [TaskStatus.CLAIMED, TaskStatus.SENT, TaskStatus.RUNNING]
        )

    def all_tasks_terminal(self) -> bool:
        counts = self.repository.overall_counts()
        return counts["total"] > 0 and not any(
            counts[key] for key in ("pending", "claimed", "sent", "running")
        )

    def has_resumable_work(self) -> bool:
        """Return whether the persisted session needs automatic processing or polling."""

        if self.repository.any_tasks(
            [TaskStatus.PENDING, TaskStatus.CLAIMED, TaskStatus.SENT, TaskStatus.RUNNING]
        ):
            return True
        return bool(self.repository.list_folder_descriptors(states=["WAITING", "SCANNING"]))

    def remove_pending_only_folders(
        self,
        folder_paths: list[str],
    ) -> tuple[list[str], list[str], list[str], int]:
        task_counts = {
            folder_path: summary.total
            for folder_path in folder_paths
            if (summary := self.get_folder_summary(folder_path)) is not None
        }
        removed, blocked, request_ids = self.repository.delete_pending_folders(
            folder_paths,
            include_request_ids=False,
        )
        for folder_path in removed:
            self.folder_group_removed.emit(folder_path)
        if removed:
            self._emit_overall()
        return removed, blocked, request_ids, sum(task_counts.get(path, 0) for path in removed)

    def overall_stats(self) -> dict[str, float | int | None]:
        counts = self.repository.overall_counts()
        completed = sum(counts[key] for key in ("success", "fail", "timeout", "error", "cancelled"))
        total = counts["total"]
        remaining = max(0, total - completed)
        avg_seconds: float | None = None
        eta_seconds: float | None = 0.0 if remaining == 0 else None
        if self._first_sent_at is not None and completed > 0:
            elapsed = (datetime.now(timezone.utc) - self._first_sent_at).total_seconds()
            if elapsed > 0:
                avg_seconds = elapsed / completed
                eta_seconds = avg_seconds * remaining
        return {
            "total": total,
            "total_final": not self.repository.any_folder_descriptors(["WAITING", "SCANNING"]),
            "completed": completed,
            "success": counts["success"],
            "fail": counts["fail"],
            "timeout": counts["timeout"],
            "error": counts["error"],
            "progress": (completed / total * 100.0) if total else 0.0,
            "avg_processing_seconds": avg_seconds,
            "eta_seconds": eta_seconds,
        }

    def build_mq_preview(
        self,
        request_id: str,
        app_config: AppConfig,
        active_result_queue: str | None,
        runtime_action: str | None = None,
        runtime_recipe_path: str | None = None,
        runtime_priority: int | None = None,
        resolved_local_ipv4: str | None = None,
    ) -> dict[str, Any] | None:
        row = self.repository.get_task_record(request_id)
        if row is None:
            return None
        rabbitmq = app_config.rabbitmq
        exchange, routing_key = resolve_publish_route(rabbitmq)
        queue_name = str(row["result_queue"] or active_result_queue or "").strip()
        if not queue_name:
            queue_name = resolve_result_queue_name(
                rabbitmq.result_queue_base,
                resolved_local_ipv4 or "127.0.0.1",
            )
        action = str(row["action"] or runtime_action or app_config.publish.default_action)
        recipe_path = str(row["recipe_path"] or runtime_recipe_path or app_config.recipe_config.default_path)
        priority = int(row["priority"] if row["priority"] is not None else runtime_priority or 0)
        payload = {
            "request_id": request_id,
            "action": action,
            "QUEUE_NAME": queue_name,
            "RECIPE_PATH": recipe_path,
            "IMG_LIST": [str(row["image_path"])],
        }
        published = payload if TaskStatus(str(row["status"])) not in {
            TaskStatus.PENDING,
            TaskStatus.CLAIMED,
        } else {}
        received = json.loads(str(row["received_json"])) if row["received_json"] else {}
        received_meta = json.loads(str(row["received_meta_json"] or "{}"))
        return {
            "connection": {
                "host": rabbitmq.host,
                "port": rabbitmq.port,
                "virtual_host": rabbitmq.virtual_host,
                "request_exchange": rabbitmq.request_exchange,
                "request_routing_key": rabbitmq.request_routing_key,
                "request_queue": rabbitmq.request_queue,
                "result_queue_base": rabbitmq.result_queue_base,
                "resolved_result_queue": queue_name,
                "active_result_queue": active_result_queue or "",
            },
            "message": {
                "request_id": request_id,
                "status": str(row["status"]),
                "sent_at": row["sent_at"],
                "completed_at": row["completed_at"],
                "image_path": str(row["image_path"]),
                "selected_priority": priority,
                "publish_meta": {
                    "exchange": exchange,
                    "routing_key": routing_key,
                    "reply_to": queue_name,
                    "message_id": request_id,
                    "correlation_id": request_id,
                    "content_type": "application/json",
                    "priority": priority,
                },
                "received_meta": received_meta,
            },
            "payload": {"expected": payload, "published": published, "received": received},
        }

    def _emit_task_and_folder(self, request_id: str) -> None:
        task = self.get_task(request_id)
        if task is None:
            return
        self.task_updated.emit(request_id)
        self.folder_group_updated.emit(task.folder_path)
        self._emit_overall()

    def _emit_all_folders(self) -> None:
        for folder_path in self.get_folder_paths():
            self.folder_group_updated.emit(folder_path)

    def _emit_overall(self) -> None:
        self.overall_updated.emit(self.overall_stats())
