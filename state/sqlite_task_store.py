"""Qt-facing disk-backed task store used by the production runtime."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import csv
import json
import os
from pathlib import Path
from typing import Any

from config.models import AppConfig
from models.task_models import (
    FolderSummary,
    ImageTask,
    RunHistorySummary,
    TaskMessage,
    TaskResult,
    TaskStatus,
)
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
    duplicate_folders_detected = Signal(list)
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
        created_new_session = self.repository.prepare_session_for_registration()
        if created_new_session:
            self._first_sent_at = None
            self.store_reset.emit()
        normalized_paths = list(
            dict.fromkeys(str(path).strip() for path in folder_paths if str(path).strip())
        )
        normalized_recipes = list(
            dict.fromkeys(
                (str(alias).strip() or str(path).strip(), str(path).strip())
                for alias, path in recipe_selections
                if str(path).strip()
            )
        )
        before_rows = self.repository.list_folder_descriptors()
        before = {str(row["folder_path"]) for row in before_rows}
        existing_by_identity = {
            (str(row["source_path"]), str(row["recipe_path"])): str(row["folder_path"])
            for row in before_rows
        }
        duplicates = [
            existing_by_identity[(source_path, recipe_path)]
            for source_path in normalized_paths
            for _alias, recipe_path in normalized_recipes
            if (source_path, recipe_path) in existing_by_identity
        ]
        added = self.repository.register_folder_descriptors(folder_paths, recipe_selections)
        for folder_path in self.get_folder_paths():
            if folder_path not in before:
                self.folder_group_added.emit(folder_path)
                self.folder_group_updated.emit(folder_path)
        if added:
            self._emit_overall()
        if duplicates:
            self.duplicate_folders_detected.emit(duplicates)
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

    def set_folder_inaccessible_count(self, folder_path: str, count: int) -> None:
        self.repository.set_folder_inaccessible_count(folder_path, count)

    def folder_scan_state(self, folder_path: str) -> str | None:
        for row in self.repository.list_folder_descriptors():
            if str(row["folder_path"]) == folder_path:
                return str(row["scan_state"])
        return None

    def get_folder_paths(self) -> list[str]:
        return [str(row["folder_path"]) for row in self.repository.list_folder_descriptors()]

    def get_folder_source_path(self, folder_path: str) -> str | None:
        return self.repository.folder_source_path(folder_path)

    def populate_folder_from_scanned_sibling(self, folder_path: str) -> bool:
        populated = self.repository.populate_folder_from_scanned_sibling(folder_path)
        if populated:
            self.folder_group_updated.emit(folder_path)
            self._emit_overall()
        return populated

    def get_waiting_folder_paths(self) -> list[str]:
        return [
            str(row["folder_path"])
            for row in self.repository.list_folder_descriptors(states=["WAITING"])
        ]

    def get_active_candidate_folder_paths(self) -> list[str]:
        return [
            str(row["folder_path"])
            for row in self.repository.list_folder_descriptors(
                states=["SCANNING", "SCANNED"], include_held=False
            )
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

    def should_auto_resume(self) -> bool:
        """Return whether persisted work was explicitly started by the user."""

        return (
            self.repository.session_state() == "ACTIVE"
            and self.repository.is_resume_enabled()
            and self.has_resumable_work()
        )

    def is_paused_by_user(self) -> bool:
        return self.repository.session_state() == "PAUSED_BY_USER"

    def paused_work_count(self) -> int:
        return self.repository.paused_work_count()

    def pause_by_user(self) -> None:
        self.release_claimed()
        self.repository.pause_by_user()

    def resume_by_user(self) -> None:
        self.repository.resume_by_user()

    def complete_session(self) -> None:
        self.repository.mark_completed()

    def prune_history(self, max_sessions: int) -> int:
        return self.repository.prune_history(max_sessions)

    def disable_auto_resume(self) -> None:
        """Prevent completed work from authorizing later folders to auto-start."""

        self.repository.disable_auto_resume()

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

    def reorder_folders(
        self,
        folder_paths: list[str],
        operation: str,
    ) -> tuple[list[str], list[str]]:
        moved, blocked = self.repository.reorder_folders(folder_paths, operation)
        if moved:
            self._emit_all_folders()
        return moved, blocked

    def set_folders_held(
        self,
        folder_paths: list[str],
        held: bool,
    ) -> tuple[list[str], list[str]]:
        changed, blocked = self.repository.set_folders_held(folder_paths, held)
        for folder_path in changed:
            self.folder_group_updated.emit(folder_path)
        return changed, blocked

    def has_dispatchable_pending_tasks(self) -> bool:
        return any(
            summary.total > summary.completed and not summary.held
            for summary in self.get_folder_summaries()
        )

    def held_pending_count(self) -> int:
        return sum(
            max(0, summary.total - summary.completed)
            for summary in self.get_folder_summaries()
            if summary.held
        )

    def build_preflight_report(
        self,
        *,
        broker_connected: bool,
        request_queue: str,
        priority: int,
        initial_open_folders: int,
        max_active_open_folders: int,
        warning_threshold: int,
        worker_count: int | None = None,
        queued_message_count: int | None = None,
    ) -> dict[str, Any]:
        snapshot = self.repository.preflight_snapshot()
        folder_paths = list(snapshot["folder_paths"])
        recipe_paths = list(snapshot["recipe_paths"])
        scan_error_folders = set(snapshot["scan_error_folders"])
        inaccessible_folders = list(
            dict.fromkeys(
                [
                    folder_path
                    for folder_path in folder_paths
                    if folder_path in scan_error_folders
                    or not Path(folder_path).is_dir()
                    or not os.access(folder_path, os.R_OK)
                ]
            )
        )
        missing_recipes = [
            recipe_path
            for recipe_path in recipe_paths
            if not Path(recipe_path).expanduser().is_file()
        ]
        total = int(snapshot["total"])
        held_task_count = int(snapshot["held_task_count"])
        dispatchable_total = max(0, total - held_task_count)
        held_count = int(snapshot["held_folder_count"])
        inaccessible_images = int(snapshot["inaccessible_image_count"])
        issues: list[str] = []
        if missing_recipes:
            issues.append(f"존재하지 않는 Recipe {len(missing_recipes)}개")
        if inaccessible_folders:
            issues.append(f"접근할 수 없는 폴더 {len(inaccessible_folders)}개")
        if inaccessible_images:
            issues.append(f"접근할 수 없는 이미지 {inaccessible_images}개")
        if not broker_connected:
            issues.append("RabbitMQ 연결 실패")
        return {
            "folder_count": len(folder_paths),
            "held_folder_count": held_count,
            "recipe_count": len(recipe_paths),
            "image_count": int(snapshot["image_count"]),
            "message_count": total,
            "dispatchable_message_count": dispatchable_total,
            "missing_recipes": missing_recipes,
            "inaccessible_folders": inaccessible_folders,
            "inaccessible_image_count": inaccessible_images,
            "broker_connected": bool(broker_connected),
            "worker_count": worker_count,
            "queued_message_count": queued_message_count,
            "request_queue": request_queue,
            "priority": max(0, int(priority)),
            "initial_open_folders": max(1, int(initial_open_folders)),
            "max_active_open_folders": max(1, int(max_active_open_folders)),
            "warning_threshold": max(1, int(warning_threshold)),
            "threshold_exceeded": total >= max(1, int(warning_threshold)),
            "issues": issues,
        }

    def list_run_history(self) -> list[RunHistorySummary]:
        return self.repository.list_run_history()

    def export_run_history_csv(self, session_id: str, destination: str | Path) -> int:
        rows = self.repository.get_history_task_rows(session_id)
        destination_path = Path(destination)
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        with destination_path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(
                [
                    "request_id",
                    "folder_path",
                    "image_path",
                    "recipe_alias",
                    "recipe_path",
                    "status",
                    "created_at",
                    "sent_at",
                    "completed_at",
                    "result",
                    "error_message",
                ]
            )
            for row in rows:
                writer.writerow(
                    [
                        row["request_id"],
                        row["folder_path"],
                        row["image_path"],
                        row["recipe_alias"],
                        row["recipe_path"],
                        row["status"],
                        row["created_at"],
                        row["sent_at"],
                        row["completed_at"],
                        row["result_json"],
                        row["error_message"],
                    ]
                )
        return len(rows)

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
