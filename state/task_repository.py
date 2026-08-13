"""SQLite-backed task persistence for bounded-memory large job runs."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from models.task_models import FolderSummary, ImageTask, RunHistorySummary, TaskStatus
from utils.image_sort import compare_image_paths


_COUNTER_COLUMN = {
    TaskStatus.PENDING: "pending_count",
    TaskStatus.CLAIMED: "claimed_count",
    TaskStatus.SENT: "sent_count",
    TaskStatus.RUNNING: "running_count",
    TaskStatus.SUCCESS: "success_count",
    TaskStatus.FAIL: "fail_count",
    TaskStatus.TIMEOUT: "timeout_count",
    TaskStatus.ERROR: "error_count",
    TaskStatus.CANCELLED: "cancelled_count",
}


class TaskRepository:
    """Persist task state and aggregate counters without hydrating every task."""

    def __init__(self, database_path: str | Path = ":memory:", session_id: str | None = None) -> None:
        self.database_path = str(database_path)
        if self.database_path != ":memory:":
            Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self.database_path,
            timeout=30.0,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._configure_connection()
        self._create_schema()
        self.session_id = session_id or self._resume_or_create_session()
        self.recover_claimed_tasks()
        self.recover_interrupted_scans()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def clear_session(self) -> None:
        """Archive the current workload and switch to a fresh active session."""

        if self.session_state() in {"COMPLETED", "RESET"}:
            self.session_id = self._create_session()
            return
        with self._lock:
            has_content = self._connection.execute(
                "SELECT 1 FROM folders WHERE session_id = ? LIMIT 1",
                (self.session_id,),
            ).fetchone()
        if has_content is None:
            with self._transaction() as cursor:
                cursor.execute(
                    """
                    UPDATE sessions SET state = 'ACTIVE', updated_at = ?, action = NULL,
                        result_queue = NULL, priority = NULL, polling_interval = NULL,
                        resume_enabled = 0, ended_at = NULL
                    WHERE session_id = ?
                    """,
                    (_now_text(), self.session_id),
                )
            return
        self._archive_and_create_session("RESET")

    def save_runtime_settings(
        self,
        action: str,
        result_queue: str,
        priority: int,
        polling_interval: int,
    ) -> None:
        """Persist session-level settings needed for deterministic restart recovery."""

        now = _now_text()
        with self._transaction() as cursor:
            cursor.execute(
                """
                UPDATE sessions SET action = ?, result_queue = ?, priority = ?,
                    polling_interval = ?, resume_enabled = 1, state = 'ACTIVE',
                    started_at = COALESCE(started_at, ?), updated_at = ?
                WHERE session_id = ?
                """,
                (
                    action,
                    result_queue,
                    max(0, int(priority)),
                    max(1, int(polling_interval)),
                    now,
                    now,
                    self.session_id,
                ),
            )

    def get_runtime_settings(self) -> dict[str, Any] | None:
        """Load saved session settings, if the session has started before."""

        with self._lock:
            row = self._connection.execute(
                """
                SELECT action, result_queue, priority, polling_interval
                FROM sessions WHERE session_id = ?
                """,
                (self.session_id,),
            ).fetchone()
        if row is None or not row["action"] or not row["result_queue"]:
            return None
        return {
            "action": str(row["action"]),
            "result_queue": str(row["result_queue"]),
            "priority": max(0, int(row["priority"] or 0)),
            "polling_interval": max(1, int(row["polling_interval"] or 1)),
        }

    def is_resume_enabled(self) -> bool:
        """Return whether the user previously started the current workload."""

        with self._lock:
            row = self._connection.execute(
                "SELECT resume_enabled FROM sessions WHERE session_id = ?",
                (self.session_id,),
            ).fetchone()
        return bool(row and row["resume_enabled"])

    def session_state(self) -> str:
        with self._lock:
            row = self._connection.execute(
                "SELECT state FROM sessions WHERE session_id = ?",
                (self.session_id,),
            ).fetchone()
        return str(row[0]) if row is not None else "ACTIVE"

    def pause_by_user(self) -> None:
        """Persist a user-requested pause without losing restart metadata."""

        with self._transaction() as cursor:
            cursor.execute(
                """
                UPDATE sessions SET state = 'PAUSED_BY_USER', updated_at = ?, resume_enabled = 1
                WHERE session_id = ?
                """,
                (_now_text(), self.session_id),
            )

    def resume_by_user(self) -> None:
        """Return a user-paused session to automatic recovery state."""

        with self._transaction() as cursor:
            cursor.execute(
                """
                UPDATE sessions SET state = 'ACTIVE', updated_at = ?, resume_enabled = 1,
                    ended_at = NULL
                WHERE session_id = ?
                """,
                (_now_text(), self.session_id),
            )

    def paused_work_count(self) -> int:
        if self.session_state() != "PAUSED_BY_USER":
            return 0
        task_count = self.count_tasks(
            [TaskStatus.PENDING, TaskStatus.CLAIMED, TaskStatus.SENT, TaskStatus.RUNNING]
        )
        if task_count:
            return task_count
        return len(self.list_folder_descriptors(states=["WAITING", "SCANNING"]))

    def mark_completed(self) -> None:
        """Close the current session while keeping its rows available to the UI."""

        now = _now_text()
        with self._transaction() as cursor:
            cursor.execute(
                """
                UPDATE sessions SET state = 'COMPLETED', updated_at = ?, ended_at = ?,
                    resume_enabled = 0
                WHERE session_id = ?
                """,
                (now, now, self.session_id),
            )

    def disable_auto_resume(self) -> None:
        """Mark the current workload as completed and not eligible for auto-resume."""

        with self._transaction() as cursor:
            cursor.execute(
                "UPDATE sessions SET resume_enabled = 0 WHERE session_id = ?",
                (self.session_id,),
            )

    def prepare_session_for_registration(self) -> bool:
        """Create a fresh session when the displayed workload is already archived."""

        if self.session_state() in {"ACTIVE", "PAUSED_BY_USER"}:
            return False
        self.session_id = self._create_session()
        return True

    def register_folder_descriptors(
        self,
        folder_paths: Sequence[str],
        recipe_selections: Sequence[tuple[str, str]],
    ) -> int:
        """Register lightweight folder descriptors without enumerating images."""

        normalized_recipes = _normalize_recipes(recipe_selections)
        if not normalized_recipes:
            return 0
        added = 0
        with self._transaction() as cursor:
            next_position = int(
                cursor.execute(
                    "SELECT COALESCE(MAX(position), -1) + 1 FROM folders WHERE session_id = ?",
                    (self.session_id,),
                ).fetchone()[0]
            )
            for source_path in dict.fromkeys(
                str(path).strip() for path in folder_paths if str(path).strip()
            ):
                for recipe_alias, recipe_path in normalized_recipes:
                    existing = cursor.execute(
                        """
                        SELECT 1 FROM folders
                        WHERE session_id = ? AND source_path = ? AND recipe_path = ?
                        """,
                        (self.session_id, source_path, recipe_path),
                    ).fetchone()
                    if existing is not None:
                        continue
                    queue_key = self._available_folder_queue_key(cursor, source_path, recipe_path)
                    cursor.execute(
                        """
                        INSERT INTO folders(
                            session_id, folder_path, source_path, position, recipes_json,
                            recipe_alias, recipe_path, scan_state
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'WAITING')
                        """,
                        (
                            self.session_id,
                            queue_key,
                            source_path,
                            next_position,
                            json.dumps([(recipe_alias, recipe_path)], ensure_ascii=False),
                            recipe_alias,
                            recipe_path,
                        ),
                    )
                    added += 1
                    next_position += 1
        return added

    def _available_folder_queue_key(
        self,
        cursor: sqlite3.Cursor,
        source_path: str,
        recipe_path: str,
    ) -> str:
        """Keep the first queue backward-compatible and key later recipes deterministically."""

        source_key_used = cursor.execute(
            "SELECT 1 FROM folders WHERE session_id = ? AND folder_path = ?",
            (self.session_id, source_path),
        ).fetchone()
        if source_key_used is None:
            return source_path
        return f"recipe-queue:{uuid5(NAMESPACE_URL, source_path + chr(31) + recipe_path)}"

    def list_folder_descriptors(
        self,
        states: Sequence[str] | None = None,
        *,
        include_held: bool = True,
    ) -> list[sqlite3.Row]:
        query = "SELECT * FROM folders WHERE session_id = ?"
        params: list[Any] = [self.session_id]
        if states:
            placeholders = ",".join("?" for _ in states)
            query += f" AND scan_state IN ({placeholders})"
            params.extend(states)
        if not include_held:
            query += " AND held = 0"
        query += " ORDER BY position"
        with self._lock:
            return list(self._connection.execute(query, params).fetchall())

    def folder_source_path(self, folder_path: str) -> str | None:
        """Resolve an opaque folder queue key to its physical source path."""

        with self._lock:
            row = self._connection.execute(
                "SELECT source_path FROM folders WHERE session_id = ? AND folder_path = ?",
                (self.session_id, folder_path),
            ).fetchone()
        return str(row[0]) if row is not None else None

    def populate_folder_from_scanned_sibling(self, folder_path: str) -> bool:
        """Reuse an already inventoried physical folder for another recipe queue."""

        with self._transaction(immediate=True) as cursor:
            target = cursor.execute(
                """
                SELECT source_path, recipe_alias, recipe_path FROM folders
                WHERE session_id = ? AND folder_path = ? AND scan_state = 'WAITING'
                """,
                (self.session_id, folder_path),
            ).fetchone()
            if target is None:
                return False
            sibling = cursor.execute(
                """
                SELECT folder_path, scan_state FROM folders
                WHERE session_id = ? AND source_path = ? AND folder_path != ?
                  AND scan_state IN ('SCANNED', 'EMPTY')
                ORDER BY position LIMIT 1
                """,
                (self.session_id, target["source_path"], folder_path),
            ).fetchone()
            if sibling is None:
                return False
            if str(sibling["scan_state"]) == "EMPTY":
                cursor.execute(
                    """
                    UPDATE folders SET scan_state = 'EMPTY'
                    WHERE session_id = ? AND folder_path = ?
                    """,
                    (self.session_id, folder_path),
                )
                return True

            before = self._connection.total_changes
            cursor.execute(
                """
                INSERT OR IGNORE INTO tasks(
                    session_id, request_id, folder_path, image_path, recipe_alias,
                    recipe_path, status, created_at
                )
                SELECT session_id, lower(hex(randomblob(16))), ?, image_path, ?, ?, ?, ?
                FROM tasks
                WHERE session_id = ? AND folder_path = ?
                GROUP BY image_path
                """,
                (
                    folder_path,
                    target["recipe_alias"],
                    target["recipe_path"],
                    TaskStatus.PENDING.value,
                    _now_text(),
                    self.session_id,
                    sibling["folder_path"],
                ),
            )
            inserted = self._connection.total_changes - before
            cursor.execute(
                """
                UPDATE folders SET scan_state = 'SCANNED', total_count = ?, pending_count = ?
                WHERE session_id = ? AND folder_path = ?
                """,
                (inserted, inserted, self.session_id, folder_path),
            )
        return True

    def any_folder_descriptors(self, states: Sequence[str]) -> bool:
        """Check folder scan-state existence without materializing descriptor rows."""

        if not states:
            return False
        placeholders = ",".join("?" for _ in states)
        with self._lock:
            row = self._connection.execute(
                f"""
                SELECT 1 FROM folders
                WHERE session_id = ? AND scan_state IN ({placeholders}) LIMIT 1
                """,
                [self.session_id, *states],
            ).fetchone()
        return row is not None

    def set_folder_scan_state(self, folder_path: str, state: str) -> None:
        with self._transaction() as cursor:
            cursor.execute(
                "UPDATE folders SET scan_state = ? WHERE session_id = ? AND folder_path = ?",
                (state, self.session_id, folder_path),
            )

    def set_folder_inaccessible_count(self, folder_path: str, count: int) -> None:
        with self._transaction() as cursor:
            cursor.execute(
                """
                UPDATE folders SET inaccessible_count = ?
                WHERE session_id = ? AND folder_path = ?
                """,
                (max(0, int(count)), self.session_id, folder_path),
            )

    def set_folders_held(self, folder_paths: Sequence[str], held: bool) -> tuple[list[str], list[str]]:
        """Hold or release folders only while every task remains unclaimed."""

        changed: list[str] = []
        blocked: list[str] = []
        with self._transaction(immediate=True) as cursor:
            for folder_path in dict.fromkeys(folder_paths):
                row = cursor.execute(
                    """
                    SELECT held, total_count, pending_count, claimed_count, sent_count, running_count,
                           success_count, fail_count, timeout_count, error_count, cancelled_count
                    FROM folders WHERE session_id = ? AND folder_path = ?
                    """,
                    (self.session_id, folder_path),
                ).fetchone()
                if row is None:
                    continue
                if not _folder_is_controllable(row):
                    blocked.append(folder_path)
                    continue
                if bool(row["held"]) == bool(held):
                    continue
                cursor.execute(
                    "UPDATE folders SET held = ? WHERE session_id = ? AND folder_path = ?",
                    (1 if held else 0, self.session_id, folder_path),
                )
                if cursor.rowcount:
                    changed.append(folder_path)
        return changed, blocked

    def reorder_folders(
        self,
        folder_paths: Sequence[str],
        operation: str,
    ) -> tuple[list[str], list[str]]:
        """Move controllable folders while preserving relative order."""

        selected = list(dict.fromkeys(str(path) for path in folder_paths if str(path)))
        if not selected:
            return [], []
        with self._transaction(immediate=True) as cursor:
            rows = cursor.execute(
                "SELECT * FROM folders WHERE session_id = ? ORDER BY position",
                (self.session_id,),
            ).fetchall()
            row_by_path = {str(row["folder_path"]): row for row in rows}
            blocked = [path for path in selected if path in row_by_path and not _folder_is_controllable(row_by_path[path])]
            movable = [path for path in selected if path in row_by_path and path not in blocked]
            if not movable:
                return [], blocked
            order = [str(row["folder_path"]) for row in rows]
            original_order = list(order)
            controllable_paths = [
                str(row["folder_path"])
                for row in rows
                if _folder_is_controllable(row)
            ]
            controllable_slots = [
                index
                for index, row in enumerate(rows)
                if _folder_is_controllable(row)
            ]
            selected_set = set(movable)
            if operation == "top":
                prefix = [path for path in controllable_paths if path in selected_set]
                controllable_paths = [
                    *prefix,
                    *(path for path in controllable_paths if path not in selected_set),
                ]
            elif operation == "up":
                for index in range(1, len(controllable_paths)):
                    if (
                        controllable_paths[index] in selected_set
                        and controllable_paths[index - 1] not in selected_set
                    ):
                        controllable_paths[index - 1], controllable_paths[index] = (
                            controllable_paths[index],
                            controllable_paths[index - 1],
                        )
            elif operation == "down":
                for index in range(len(controllable_paths) - 2, -1, -1):
                    if (
                        controllable_paths[index] in selected_set
                        and controllable_paths[index + 1] not in selected_set
                    ):
                        controllable_paths[index], controllable_paths[index + 1] = (
                            controllable_paths[index + 1],
                            controllable_paths[index],
                        )
            elif operation == "bottom":
                suffix = [path for path in controllable_paths if path in selected_set]
                controllable_paths = [
                    *(path for path in controllable_paths if path not in selected_set),
                    *suffix,
                ]
            else:
                raise ValueError(f"지원하지 않는 폴더 이동 작업입니다: {operation}")
            for slot, folder_path in zip(controllable_slots, controllable_paths, strict=True):
                order[slot] = folder_path
            if order == original_order:
                return [], blocked
            for position, folder_path in enumerate(order):
                cursor.execute(
                    "UPDATE folders SET position = ? WHERE session_id = ? AND folder_path = ?",
                    (position, self.session_id, folder_path),
                )
        return movable, blocked

    def folder_recipes(self, folder_path: str) -> list[tuple[str, str]]:
        with self._lock:
            row = self._connection.execute(
                "SELECT recipes_json FROM folders WHERE session_id = ? AND folder_path = ?",
                (self.session_id, folder_path),
            ).fetchone()
        if row is None:
            return []
        loaded = json.loads(str(row[0] or "[]"))
        return _normalize_recipes((str(item[0]), str(item[1])) for item in loaded)

    def insert_task_batch(
        self,
        folder_path: str,
        image_paths: Iterable[str],
        recipe_selections: Sequence[tuple[str, str]] | None = None,
    ) -> int:
        """Insert one bounded image batch and update folder counters transactionally."""

        recipes = _normalize_recipes(recipe_selections or self.folder_recipes(folder_path))
        if not recipes:
            return 0
        normalized_images = list(dict.fromkeys(str(path).strip() for path in image_paths if str(path).strip()))
        if not normalized_images:
            return 0

        now = _now_text()
        rows = [
            (
                self.session_id,
                str(uuid4()),
                folder_path,
                image_path,
                alias,
                recipe_path,
                TaskStatus.PENDING.value,
                now,
            )
            for image_path in normalized_images
            for alias, recipe_path in recipes
        ]
        with self._transaction() as cursor:
            descriptor_exists = cursor.execute(
                "SELECT 1 FROM folders WHERE session_id = ? AND folder_path = ?",
                (self.session_id, folder_path),
            ).fetchone()
            if descriptor_exists is None:
                return 0
            before = self._connection.total_changes
            cursor.executemany(
                """
                INSERT OR IGNORE INTO tasks(
                    session_id, request_id, folder_path, image_path, recipe_alias,
                    recipe_path, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            inserted = self._connection.total_changes - before
            if inserted:
                cursor.execute(
                    """
                    UPDATE folders
                    SET total_count = total_count + ?, pending_count = pending_count + ?
                    WHERE session_id = ? AND folder_path = ?
                    """,
                    (inserted, inserted, self.session_id, folder_path),
                )
        return int(inserted)

    def claim_pending(
        self,
        folder_paths: Sequence[str],
        limit: int,
        *,
        action: str | None = None,
        result_queue: str | None = None,
        priority: int | None = None,
    ) -> list[ImageTask]:
        """Atomically claim the next pending tasks for a bounded publish chunk."""

        normalized_folders = list(dict.fromkeys(str(path) for path in folder_paths if str(path)))
        if not normalized_folders or limit <= 0:
            return []
        placeholders = ",".join("?" for _ in normalized_folders)
        with self._transaction(immediate=True) as cursor:
            rows = cursor.execute(
                f"""
                SELECT t.* FROM tasks t
                JOIN folders f
                  ON f.session_id = t.session_id AND f.folder_path = t.folder_path
                WHERE t.session_id = ? AND t.status = ?
                  AND t.folder_path IN ({placeholders})
                  AND f.held = 0
                ORDER BY f.position, t.rowid
                LIMIT ?
                """,
                [self.session_id, TaskStatus.PENDING.value, *normalized_folders, int(limit)],
            ).fetchall()
            if not rows:
                return []
            request_ids = [str(row["request_id"]) for row in rows]
            request_placeholders = ",".join("?" for _ in request_ids)
            assignments = ["status = ?"]
            update_params: list[Any] = [TaskStatus.CLAIMED.value]
            if action is not None:
                assignments.append("action = ?")
                update_params.append(action)
            if result_queue is not None:
                assignments.append("result_queue = ?")
                update_params.append(result_queue)
            if priority is not None:
                assignments.append("priority = ?")
                update_params.append(max(0, int(priority)))
            cursor.execute(
                f"UPDATE tasks SET {', '.join(assignments)} "
                f"WHERE request_id IN ({request_placeholders})",
                [*update_params, *request_ids],
            )
            counts = _count_by_folder(rows)
            for folder_path, count in counts.items():
                cursor.execute(
                    """
                    UPDATE folders SET
                        pending_count = pending_count - ?,
                        claimed_count = claimed_count + ?
                    WHERE session_id = ? AND folder_path = ?
                    """,
                    (count, count, self.session_id, folder_path),
                )
        return [_row_to_task(row, status=TaskStatus.CLAIMED) for row in rows]

    def recover_claimed_tasks(self) -> int:
        """Return interrupted, not-yet-published claims to PENDING."""

        with self._transaction(immediate=True) as cursor:
            rows = cursor.execute(
                """
                SELECT folder_path, COUNT(*) AS count FROM tasks
                WHERE session_id = ? AND status = ? GROUP BY folder_path
                """,
                (self.session_id, TaskStatus.CLAIMED.value),
            ).fetchall()
            recovered = sum(int(row["count"]) for row in rows)
            if not recovered:
                return 0
            cursor.execute(
                "UPDATE tasks SET status = ? WHERE session_id = ? AND status = ?",
                (TaskStatus.PENDING.value, self.session_id, TaskStatus.CLAIMED.value),
            )
            for row in rows:
                cursor.execute(
                    """
                    UPDATE folders SET
                        claimed_count = claimed_count - ?,
                        pending_count = pending_count + ?
                    WHERE session_id = ? AND folder_path = ?
                    """,
                    (row["count"], row["count"], self.session_id, row["folder_path"]),
                )
        return recovered

    def recover_interrupted_scans(self) -> int:
        """Make folders interrupted during streaming scan eligible to resume."""

        with self._transaction() as cursor:
            cursor.execute(
                """
                UPDATE folders SET scan_state = 'WAITING'
                WHERE session_id = ? AND scan_state = 'SCANNING'
                """,
                (self.session_id,),
            )
            return int(cursor.rowcount)

    def transition_task(
        self,
        request_id: str,
        target_status: TaskStatus,
        allowed_from: set[TaskStatus],
        **fields: Any,
    ) -> bool:
        """Transition one task and adjust aggregate counters in constant time."""

        with self._transaction(immediate=True) as cursor:
            row = cursor.execute(
                "SELECT folder_path, status FROM tasks WHERE session_id = ? AND request_id = ?",
                (self.session_id, request_id),
            ).fetchone()
            if row is None:
                return False
            current_status = TaskStatus(str(row["status"]))
            if current_status not in allowed_from:
                return False

            assignments = ["status = ?"]
            params: list[Any] = [target_status.value]
            for column, value in fields.items():
                if column not in {
                    "sent_at", "completed_at", "result_json", "error_message",
                    "received_json", "received_meta_json", "action", "result_queue", "priority",
                }:
                    continue
                assignments.append(f"{column} = ?")
                params.append(value)
            params.extend([self.session_id, request_id])
            cursor.execute(
                f"UPDATE tasks SET {', '.join(assignments)} WHERE session_id = ? AND request_id = ?",
                params,
            )
            old_column = _COUNTER_COLUMN[current_status]
            new_column = _COUNTER_COLUMN[target_status]
            cursor.execute(
                f"""
                UPDATE folders SET {old_column} = {old_column} - 1,
                                   {new_column} = {new_column} + 1
                WHERE session_id = ? AND folder_path = ?
                """,
                (self.session_id, row["folder_path"]),
            )
        return True

    def update_task_fields(self, request_id: str, **fields: Any) -> bool:
        """Update compact payload metadata without changing task status counters."""

        allowed_columns = {
            "sent_at", "completed_at", "result_json", "error_message",
            "received_json", "received_meta_json", "action", "result_queue", "priority",
        }
        assignments: list[str] = []
        params: list[Any] = []
        for column, value in fields.items():
            if column not in allowed_columns:
                continue
            assignments.append(f"{column} = ?")
            params.append(value)
        if not assignments:
            return False
        params.extend([self.session_id, request_id])
        with self._transaction() as cursor:
            cursor.execute(
                f"UPDATE tasks SET {', '.join(assignments)} WHERE session_id = ? AND request_id = ?",
                params,
            )
            return bool(cursor.rowcount)

    def transition_all(self, source: TaskStatus, target: TaskStatus) -> int:
        """Transition all tasks in one status using set-based SQL and folder counters."""

        with self._transaction(immediate=True) as cursor:
            rows = cursor.execute(
                """
                SELECT folder_path, COUNT(*) AS count FROM tasks
                WHERE session_id = ? AND status = ? GROUP BY folder_path
                """,
                (self.session_id, source.value),
            ).fetchall()
            changed = sum(int(row["count"]) for row in rows)
            if not changed:
                return 0
            cursor.execute(
                "UPDATE tasks SET status = ? WHERE session_id = ? AND status = ?",
                (target.value, self.session_id, source.value),
            )
            source_column = _COUNTER_COLUMN[source]
            target_column = _COUNTER_COLUMN[target]
            for row in rows:
                cursor.execute(
                    f"""
                    UPDATE folders SET {source_column} = {source_column} - ?,
                                       {target_column} = {target_column} + ?
                    WHERE session_id = ? AND folder_path = ?
                    """,
                    (row["count"], row["count"], self.session_id, row["folder_path"]),
                )
        return changed

    def timeout_before(self, cutoff_iso: str, completed_at: str) -> list[str]:
        """Mark all overdue SENT/RUNNING tasks in one transaction."""

        sources = (TaskStatus.SENT.value, TaskStatus.RUNNING.value)
        with self._transaction(immediate=True) as cursor:
            rows = cursor.execute(
                """
                SELECT request_id, folder_path, status FROM tasks
                WHERE session_id = ? AND status IN (?, ?)
                  AND sent_at IS NOT NULL AND sent_at <= ?
                """,
                (self.session_id, *sources, cutoff_iso),
            ).fetchall()
            if not rows:
                return []
            request_ids = [str(row["request_id"]) for row in rows]
            placeholders = ",".join("?" for _ in request_ids)
            cursor.execute(
                f"""
                UPDATE tasks SET status = ?, completed_at = ?, error_message = ?
                WHERE request_id IN ({placeholders})
                """,
                [
                    TaskStatus.TIMEOUT.value,
                    completed_at,
                    "결과 수신 시간 초과",
                    *request_ids,
                ],
            )
            grouped: dict[tuple[str, str], int] = {}
            for row in rows:
                key = (str(row["folder_path"]), str(row["status"]))
                grouped[key] = grouped.get(key, 0) + 1
            for (folder_path, source_status), count in grouped.items():
                source_column = _COUNTER_COLUMN[TaskStatus(source_status)]
                cursor.execute(
                    f"""
                    UPDATE folders SET {source_column} = {source_column} - ?,
                                       timeout_count = timeout_count + ?
                    WHERE session_id = ? AND folder_path = ?
                    """,
                    (count, count, self.session_id, folder_path),
                )
        return request_ids

    def get_task_record(self, request_id: str) -> sqlite3.Row | None:
        with self._lock:
            return self._connection.execute(
                "SELECT * FROM tasks WHERE session_id = ? AND request_id = ?",
                (self.session_id, request_id),
            ).fetchone()

    def delete_pending_folders(
        self,
        folder_paths: Sequence[str],
        *,
        include_request_ids: bool = True,
    ) -> tuple[list[str], list[str], list[str]]:
        """Delete folders only when every persisted task is still PENDING."""

        removed: list[str] = []
        blocked: list[str] = []
        request_ids: list[str] = []
        with self._transaction(immediate=True) as cursor:
            for folder_path in dict.fromkeys(folder_paths):
                row = cursor.execute(
                    """
                    SELECT total_count, pending_count, claimed_count, sent_count, running_count,
                           success_count, fail_count, timeout_count, error_count, cancelled_count
                    FROM folders WHERE session_id = ? AND folder_path = ?
                    """,
                    (self.session_id, folder_path),
                ).fetchone()
                if row is None:
                    continue
                non_pending = sum(int(row[key]) for key in row.keys() if key != "pending_count") - int(
                    row["total_count"]
                )
                if non_pending or int(row["total_count"]) != int(row["pending_count"]):
                    blocked.append(folder_path)
                    continue
                if include_request_ids:
                    request_ids.extend(
                        str(item[0])
                        for item in cursor.execute(
                            "SELECT request_id FROM tasks WHERE session_id = ? AND folder_path = ?",
                            (self.session_id, folder_path),
                        ).fetchall()
                    )
                cursor.execute(
                    "DELETE FROM folders WHERE session_id = ? AND folder_path = ?",
                    (self.session_id, folder_path),
                )
                removed.append(folder_path)
        return removed, blocked, request_ids

    def get_task(self, request_id: str) -> ImageTask | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM tasks WHERE session_id = ? AND request_id = ?",
                (self.session_id, request_id),
            ).fetchone()
        return _row_to_task(row) if row is not None else None

    def get_tasks_page(self, folder_path: str, offset: int = 0, limit: int = 500) -> list[ImageTask]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM tasks WHERE session_id = ? AND folder_path = ?
                ORDER BY image_path COLLATE IMAGE_FILENAME_ASC, recipe_alias COLLATE NOCASE
                LIMIT ? OFFSET ?
                """,
                (self.session_id, folder_path, max(1, int(limit)), max(0, int(offset))),
            ).fetchall()
        return [_row_to_task(row) for row in rows]

    def get_folder_summaries(self) -> list[FolderSummary]:
        return [_folder_row_to_summary(row) for row in self.list_folder_descriptors()]

    def get_folder_summary(self, folder_path: str) -> FolderSummary | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM folders WHERE session_id = ? AND folder_path = ?",
                (self.session_id, folder_path),
            ).fetchone()
        return _folder_row_to_summary(row) if row is not None else None

    def preflight_snapshot(self) -> dict[str, Any]:
        """Return bounded aggregate and path data for the preflight dialog."""

        with self._lock:
            folder_rows = self._connection.execute(
                """
                SELECT folder_path, source_path, recipes_json, held, scan_state FROM folders
                WHERE session_id = ? ORDER BY position
                """,
                (self.session_id,),
            ).fetchall()
            counts = self.overall_counts()
            image_count = int(
                self._connection.execute(
                    "SELECT COUNT(DISTINCT image_path) FROM tasks WHERE session_id = ?",
                    (self.session_id,),
                ).fetchone()[0]
            )
            inaccessible_count = int(
                self._connection.execute(
                    """
                    SELECT COALESCE(SUM(inaccessible_count), 0) FROM folders
                    WHERE session_id = ?
                    """,
                    (self.session_id,),
                ).fetchone()[0]
            )
            held_task_count = int(
                self._connection.execute(
                    """
                    SELECT COALESCE(SUM(total_count), 0) FROM folders
                    WHERE session_id = ? AND held = 1
                    """,
                    (self.session_id,),
                ).fetchone()[0]
            )
        recipe_paths: list[str] = []
        for row in folder_rows:
            for _alias, recipe_path in json.loads(str(row["recipes_json"] or "[]")):
                if recipe_path not in recipe_paths:
                    recipe_paths.append(str(recipe_path))
        return {
            "folder_paths": [str(row["source_path"]) for row in folder_rows],
            "scan_error_folders": [
                str(row["source_path"])
                for row in folder_rows
                if str(row["scan_state"]) == "ERROR"
            ],
            "held_folder_count": sum(bool(row["held"]) for row in folder_rows),
            "recipe_paths": recipe_paths,
            "image_count": image_count,
            "inaccessible_image_count": inaccessible_count,
            "held_task_count": held_task_count,
            "total": counts["total"],
        }

    def list_run_history(self) -> list[RunHistorySummary]:
        """List non-empty current and archived sessions newest first."""

        with self._lock:
            sessions = self._connection.execute(
                """
                SELECT s.session_id, s.state, COALESCE(s.started_at, s.created_at) AS created_at,
                       s.ended_at,
                       COUNT(DISTINCT f.folder_path) AS folder_count,
                       COALESCE(SUM(f.total_count), 0) AS total,
                       COALESCE(SUM(f.success_count), 0) AS success,
                       COALESCE(SUM(f.fail_count), 0) AS fail,
                       COALESCE(SUM(f.timeout_count), 0) AS timeout,
                       COALESCE(SUM(f.error_count), 0) AS error,
                       COALESCE(SUM(f.cancelled_count), 0) AS cancelled,
                       (
                           SELECT AVG((julianday(t.completed_at) - julianday(t.sent_at)) * 86400.0)
                           FROM tasks t
                           WHERE t.session_id = s.session_id
                             AND t.sent_at IS NOT NULL AND t.completed_at IS NOT NULL
                       ) AS avg_processing_seconds
                FROM sessions s
                JOIN folders f ON f.session_id = s.session_id
                GROUP BY s.session_id
                ORDER BY s.created_at DESC
                """
            ).fetchall()
            recipe_rows = self._connection.execute(
                "SELECT session_id, recipes_json FROM folders ORDER BY session_id, position"
            ).fetchall()
            error_rows = self._connection.execute(
                """
                SELECT session_id, error_message, COUNT(*) AS count
                FROM tasks
                WHERE error_message IS NOT NULL AND TRIM(error_message) != ''
                GROUP BY session_id, error_message
                ORDER BY session_id, count DESC, error_message
                """
            ).fetchall()
        recipe_paths_by_session: dict[str, set[str]] = {}
        for row in recipe_rows:
            paths = recipe_paths_by_session.setdefault(str(row["session_id"]), set())
            for _alias, recipe_path in json.loads(str(row["recipes_json"] or "[]")):
                paths.add(str(recipe_path))
        errors_by_session: dict[str, list[str]] = {}
        for row in error_rows:
            errors = errors_by_session.setdefault(str(row["session_id"]), [])
            errors.append(f"{row['error_message']} ({int(row['count'])})")
        return [
            RunHistorySummary(
                session_id=str(row["session_id"]),
                state=str(row["state"]),
                created_at=str(row["created_at"]),
                ended_at=str(row["ended_at"]) if row["ended_at"] else None,
                folder_count=int(row["folder_count"]),
                recipe_count=len(recipe_paths_by_session.get(str(row["session_id"]), set())),
                total=int(row["total"]),
                success=int(row["success"]),
                fail=int(row["fail"]),
                timeout=int(row["timeout"]),
                error=int(row["error"]),
                cancelled=int(row["cancelled"]),
                avg_processing_seconds=(
                    float(row["avg_processing_seconds"])
                    if row["avg_processing_seconds"] is not None
                    else None
                ),
                error_types=tuple(errors_by_session.get(str(row["session_id"]), [])),
            )
            for row in sessions
        ]

    def get_history_task_rows(self, session_id: str) -> list[sqlite3.Row]:
        """Return one archived session's task records for explicit CSV export."""

        with self._lock:
            return list(
                self._connection.execute(
                    """
                    SELECT t.request_id, f.source_path AS folder_path, t.image_path,
                           t.recipe_alias, t.recipe_path,
                           status, created_at, sent_at, completed_at, result_json, error_message
                    FROM tasks t
                    JOIN folders f
                      ON f.session_id = t.session_id AND f.folder_path = t.folder_path
                    WHERE t.session_id = ?
                    ORDER BY f.source_path COLLATE NOCASE,
                             t.image_path COLLATE IMAGE_FILENAME_ASC,
                             t.recipe_alias COLLATE NOCASE
                    """,
                    (session_id,),
                ).fetchall()
            )

    def prune_history(self, max_sessions: int) -> int:
        """Delete the oldest archived sessions beyond the configured retention count."""

        keep = max(1, int(max_sessions))
        with self._transaction(immediate=True) as cursor:
            rows = cursor.execute(
                """
                SELECT session_id FROM sessions
                WHERE state IN ('COMPLETED', 'RESET')
                ORDER BY COALESCE(ended_at, updated_at) DESC
                LIMIT -1 OFFSET ?
                """,
                (keep,),
            ).fetchall()
            session_ids = [str(row[0]) for row in rows]
            if session_ids:
                placeholders = ",".join("?" for _ in session_ids)
                cursor.execute(
                    f"DELETE FROM sessions WHERE session_id IN ({placeholders})",
                    session_ids,
                )
        return len(session_ids)

    def overall_counts(self) -> dict[str, int]:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT COALESCE(SUM(total_count), 0) AS total,
                       COALESCE(SUM(success_count), 0) AS success,
                       COALESCE(SUM(fail_count), 0) AS fail,
                       COALESCE(SUM(timeout_count), 0) AS timeout,
                       COALESCE(SUM(error_count), 0) AS error,
                       COALESCE(SUM(cancelled_count), 0) AS cancelled,
                       COALESCE(SUM(pending_count), 0) AS pending,
                       COALESCE(SUM(claimed_count), 0) AS claimed,
                       COALESCE(SUM(sent_count), 0) AS sent,
                       COALESCE(SUM(running_count), 0) AS running
                FROM folders WHERE session_id = ?
                """,
                (self.session_id,),
            ).fetchone()
        return {key: int(row[key]) for key in row.keys()}

    def count_tasks(self, statuses: Sequence[TaskStatus] | None = None) -> int:
        query = "SELECT COUNT(*) FROM tasks WHERE session_id = ?"
        params: list[Any] = [self.session_id]
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            query += f" AND status IN ({placeholders})"
            params.extend(status.value for status in statuses)
        with self._lock:
            return int(self._connection.execute(query, params).fetchone()[0])

    def any_tasks(self, statuses: Sequence[TaskStatus]) -> bool:
        """Check indexed task existence without counting the entire matching range."""

        if not statuses:
            return False
        placeholders = ",".join("?" for _ in statuses)
        with self._lock:
            row = self._connection.execute(
                f"""
                SELECT 1 FROM tasks
                WHERE session_id = ? AND status IN ({placeholders}) LIMIT 1
                """,
                [self.session_id, *(status.value for status in statuses)],
            ).fetchone()
        return row is not None

    def known_request_ids(self, statuses: Sequence[TaskStatus] | None = None) -> set[str]:
        query = "SELECT request_id FROM tasks WHERE session_id = ?"
        params: list[Any] = [self.session_id]
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            query += f" AND status IN ({placeholders})"
            params.extend(status.value for status in statuses)
        with self._lock:
            return {str(row[0]) for row in self._connection.execute(query, params)}

    def _configure_connection(self) -> None:
        self._connection.create_collation("IMAGE_FILENAME_ASC", compare_image_paths)
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA busy_timeout = 30000")
        if self.database_path != ":memory:":
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute("PRAGMA synchronous = NORMAL")

    def _create_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions(
                session_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                state TEXT NOT NULL,
                action TEXT,
                result_queue TEXT,
                priority INTEGER,
                polling_interval INTEGER,
                resume_enabled INTEGER NOT NULL DEFAULT 0,
                started_at TEXT,
                ended_at TEXT
            );
            CREATE TABLE IF NOT EXISTS folders(
                session_id TEXT NOT NULL,
                folder_path TEXT NOT NULL,
                source_path TEXT NOT NULL,
                position INTEGER NOT NULL,
                recipes_json TEXT NOT NULL,
                recipe_alias TEXT NOT NULL,
                recipe_path TEXT NOT NULL,
                scan_state TEXT NOT NULL,
                total_count INTEGER NOT NULL DEFAULT 0,
                pending_count INTEGER NOT NULL DEFAULT 0,
                claimed_count INTEGER NOT NULL DEFAULT 0,
                sent_count INTEGER NOT NULL DEFAULT 0,
                running_count INTEGER NOT NULL DEFAULT 0,
                success_count INTEGER NOT NULL DEFAULT 0,
                fail_count INTEGER NOT NULL DEFAULT 0,
                timeout_count INTEGER NOT NULL DEFAULT 0,
                error_count INTEGER NOT NULL DEFAULT 0,
                cancelled_count INTEGER NOT NULL DEFAULT 0,
                held INTEGER NOT NULL DEFAULT 0,
                inaccessible_count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(session_id, folder_path),
                FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS tasks(
                session_id TEXT NOT NULL,
                request_id TEXT PRIMARY KEY,
                folder_path TEXT NOT NULL,
                image_path TEXT NOT NULL,
                recipe_alias TEXT NOT NULL,
                recipe_path TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                sent_at TEXT,
                completed_at TEXT,
                result_json TEXT,
                error_message TEXT,
                received_json TEXT,
                received_meta_json TEXT,
                action TEXT,
                result_queue TEXT,
                priority INTEGER NOT NULL DEFAULT 0,
                UNIQUE(session_id, image_path, recipe_path),
                FOREIGN KEY(session_id, folder_path)
                    REFERENCES folders(session_id, folder_path) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_tasks_session_status
                ON tasks(session_id, status);
            CREATE INDEX IF NOT EXISTS idx_tasks_folder_status
                ON tasks(session_id, folder_path, status);
            CREATE INDEX IF NOT EXISTS idx_folders_session_position
                ON folders(session_id, position);
            """
        )
        self._migrate_session_columns()
        self._migrate_folder_columns()
        self._migrate_recipe_queue_rows()
        self._connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_folders_session_source_recipe
            ON folders(session_id, source_path, recipe_path)
            """
        )
        self._connection.commit()

    def _migrate_session_columns(self) -> None:
        """Add restart metadata columns to databases created by earlier releases."""

        existing = {
            str(row["name"])
            for row in self._connection.execute("PRAGMA table_info(sessions)").fetchall()
        }
        definitions = {
            "action": "TEXT",
            "result_queue": "TEXT",
            "priority": "INTEGER",
            "polling_interval": "INTEGER",
            "resume_enabled": "INTEGER NOT NULL DEFAULT 0",
            "started_at": "TEXT",
            "ended_at": "TEXT",
        }
        for column, definition in definitions.items():
            if column not in existing:
                self._connection.execute(
                    f"ALTER TABLE sessions ADD COLUMN {column} {definition}"
                )

    def _migrate_folder_columns(self) -> None:
        existing = {
            str(row["name"])
            for row in self._connection.execute("PRAGMA table_info(folders)").fetchall()
        }
        definitions = {
            "held": "INTEGER NOT NULL DEFAULT 0",
            "inaccessible_count": "INTEGER NOT NULL DEFAULT 0",
            "source_path": "TEXT NOT NULL DEFAULT ''",
            "recipe_alias": "TEXT NOT NULL DEFAULT ''",
            "recipe_path": "TEXT NOT NULL DEFAULT ''",
        }
        for column, definition in definitions.items():
            if column not in existing:
                self._connection.execute(
                    f"ALTER TABLE folders ADD COLUMN {column} {definition}"
                )

    def _migrate_recipe_queue_rows(self) -> None:
        """Split legacy multi-recipe folders into one persisted queue row per recipe."""

        legacy_rows = self._connection.execute(
            """
            SELECT * FROM folders
            WHERE source_path = '' OR recipe_path = ''
            ORDER BY session_id, position
            """
        ).fetchall()
        if not legacy_rows:
            return

        folder_columns = [
            str(row["name"])
            for row in self._connection.execute("PRAGMA table_info(folders)").fetchall()
        ]
        counter_columns = tuple(_COUNTER_COLUMN.values())
        sessions_to_reposition: set[str] = set()
        for row in legacy_rows:
            session_id = str(row["session_id"])
            legacy_key = str(row["folder_path"])
            source_path = str(row["source_path"] or legacy_key)
            loaded = json.loads(str(row["recipes_json"] or "[]"))
            recipes = _normalize_recipes(
                (str(item[0]), str(item[1]))
                for item in loaded
                if isinstance(item, (list, tuple)) and len(item) >= 2
            )
            if not recipes:
                recipes = [(source_path, str(row["recipe_path"] or ""))]

            for recipe_index, (recipe_alias, recipe_path) in enumerate(recipes):
                queue_key = legacy_key
                if recipe_index:
                    queue_key = (
                        f"recipe-queue:{uuid5(NAMESPACE_URL, source_path + chr(31) + recipe_path)}"
                    )
                    values = {column: row[column] for column in folder_columns}
                    values.update(
                        {
                            "folder_path": queue_key,
                            "source_path": source_path,
                            "recipe_alias": recipe_alias,
                            "recipe_path": recipe_path,
                            "recipes_json": json.dumps(
                                [(recipe_alias, recipe_path)], ensure_ascii=False
                            ),
                            "position": int(row["position"]) + recipe_index,
                        }
                    )
                    for column in counter_columns:
                        values[column] = 0
                    values["inaccessible_count"] = 0
                    placeholders = ", ".join("?" for _ in folder_columns)
                    self._connection.execute(
                        f"INSERT OR IGNORE INTO folders({', '.join(folder_columns)}) "
                        f"VALUES ({placeholders})",
                        [values[column] for column in folder_columns],
                    )
                    self._connection.execute(
                        """
                        UPDATE tasks SET folder_path = ?
                        WHERE session_id = ? AND folder_path = ? AND recipe_path = ?
                        """,
                        (queue_key, session_id, legacy_key, recipe_path),
                    )
                else:
                    self._connection.execute(
                        """
                        UPDATE folders
                        SET source_path = ?, recipe_alias = ?, recipe_path = ?, recipes_json = ?
                        WHERE session_id = ? AND folder_path = ?
                        """,
                        (
                            source_path,
                            recipe_alias,
                            recipe_path,
                            json.dumps([(recipe_alias, recipe_path)], ensure_ascii=False),
                            session_id,
                            legacy_key,
                        ),
                    )
            sessions_to_reposition.add(session_id)

        for session_id in sessions_to_reposition:
            queue_rows = self._connection.execute(
                """
                SELECT folder_path FROM folders
                WHERE session_id = ? ORDER BY position, rowid
                """,
                (session_id,),
            ).fetchall()
            for position, queue_row in enumerate(queue_rows):
                self._connection.execute(
                    "UPDATE folders SET position = ? WHERE session_id = ? AND folder_path = ?",
                    (position, session_id, queue_row["folder_path"]),
                )

        self._rebuild_folder_counters()

    def _rebuild_folder_counters(self) -> None:
        """Recompute aggregate counters after splitting legacy folder rows."""

        counter_columns = tuple(_COUNTER_COLUMN.values())
        self._connection.execute(
            "UPDATE folders SET " + ", ".join(f"{column} = 0" for column in counter_columns)
        )
        rows = self._connection.execute(
            """
            SELECT session_id, folder_path, status, COUNT(*) AS count
            FROM tasks GROUP BY session_id, folder_path, status
            """
        ).fetchall()
        for row in rows:
            status = TaskStatus(str(row["status"]))
            counter = _COUNTER_COLUMN[status]
            self._connection.execute(
                f"""
                UPDATE folders SET {counter} = ?
                WHERE session_id = ? AND folder_path = ?
                """,
                (int(row["count"]), row["session_id"], row["folder_path"]),
            )
        self._connection.execute(
            """
            UPDATE folders SET total_count = pending_count + claimed_count + sent_count
                + running_count + success_count + fail_count + timeout_count
                + error_count + cancelled_count
            """
        )

    def _resume_or_create_session(self) -> str:
        row = self._connection.execute(
            """
            SELECT session_id FROM sessions
            WHERE state IN ('ACTIVE', 'PAUSED_BY_USER') ORDER BY updated_at DESC LIMIT 1
            """
        ).fetchone()
        if row is not None:
            return str(row[0])
        return self._create_session()

    def _create_session(self) -> str:
        session_id = str(uuid4())
        now = _now_text()
        self._connection.execute(
            "INSERT INTO sessions(session_id, created_at, updated_at, state) VALUES (?, ?, ?, 'ACTIVE')",
            (session_id, now, now),
        )
        self._connection.commit()
        return session_id

    def _archive_and_create_session(self, state: str) -> None:
        now = _now_text()
        with self._transaction() as cursor:
            cursor.execute(
                """
                UPDATE sessions SET state = ?, updated_at = ?, ended_at = ?, resume_enabled = 0
                WHERE session_id = ?
                """,
                (state, now, now, self.session_id),
            )
        self.session_id = self._create_session()

    def _transaction(self, immediate: bool = False):
        return _RepositoryTransaction(self, immediate=immediate)


class _RepositoryTransaction:
    def __init__(self, repository: TaskRepository, immediate: bool) -> None:
        self._repository = repository
        self._immediate = immediate

    def __enter__(self) -> sqlite3.Cursor:
        self._repository._lock.acquire()
        self._repository._connection.execute("BEGIN IMMEDIATE" if self._immediate else "BEGIN")
        return self._repository._connection.cursor()

    def __exit__(self, exc_type, exc, traceback) -> None:  # noqa: ANN001
        try:
            if exc_type is None:
                self._repository._connection.execute(
                    "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
                    (_now_text(), self._repository.session_id),
                )
                self._repository._connection.commit()
            else:
                self._repository._connection.rollback()
        finally:
            self._repository._lock.release()


def _row_to_task(row: sqlite3.Row, status: TaskStatus | None = None) -> ImageTask:
    return ImageTask(
        request_id=str(row["request_id"]),
        image_path=str(row["image_path"]),
        folder_path=str(row["folder_path"]),
        recipe_alias=str(row["recipe_alias"]),
        recipe_path=str(row["recipe_path"]),
        status=status or TaskStatus(str(row["status"])),
        created_at=_parse_datetime(row["created_at"]) or datetime.now(timezone.utc),
        sent_at=_parse_datetime(row["sent_at"]),
        completed_at=_parse_datetime(row["completed_at"]),
        result=list(json.loads(str(row["result_json"] or "[]"))),
        error_message=str(row["error_message"]) if row["error_message"] else None,
        received_message=json.loads(str(row["received_json"])) if row["received_json"] else None,
        received_meta=json.loads(str(row["received_meta_json"] or "{}")),
    )


def _folder_row_to_summary(row: sqlite3.Row) -> FolderSummary:
    total = int(row["total_count"])
    completed = sum(
        int(row[column])
        for column in ("success_count", "fail_count", "timeout_count", "error_count", "cancelled_count")
    )
    inflight = int(row["claimed_count"] + row["sent_count"] + row["running_count"])
    if total == 0:
        status = TaskStatus.PENDING
    elif completed == total:
        status = TaskStatus.FAIL if any(
            int(row[column]) for column in ("fail_count", "timeout_count", "error_count")
        ) else TaskStatus.SUCCESS
    elif inflight or completed:
        status = TaskStatus.RUNNING
    else:
        status = TaskStatus.PENDING
    recipes = json.loads(str(row["recipes_json"] or "[]"))
    aliases = tuple(dict.fromkeys(str(item[0]) for item in recipes if item and str(item[0])))
    scan_state = str(row["scan_state"])
    held = bool(row["held"])
    if held:
        stage_label = "보류"
    elif scan_state == "WAITING":
        stage_label = "스캔 대기"
    elif scan_state == "SCANNING":
        stage_label = "스캔 중"
    elif scan_state == "EMPTY":
        stage_label = "이미지 없음"
    elif scan_state == "ERROR":
        stage_label = "스캔 오류"
    elif status.is_done:
        stage_label = "완료"
    elif inflight or completed:
        stage_label = "처리 중"
    else:
        stage_label = "전송 대기"
    return FolderSummary(
        folder_path=str(row["folder_path"]),
        total=total,
        completed=completed,
        success=int(row["success_count"]),
        fail=int(row["fail_count"]),
        timeout=int(row["timeout_count"]),
        error=int(row["error_count"]),
        progress=(completed / total * 100.0) if total else 0.0,
        status=status,
        recipe_aliases=aliases,
        stage_label=stage_label,
        held=held,
        queue_priority=int(row["position"]) + 1,
        source_path=str(row["source_path"] or row["folder_path"]),
    )


def _folder_is_controllable(row: sqlite3.Row) -> bool:
    """Return whether a folder has not entered publish/result processing."""

    return (
        int(row["total_count"]) == int(row["pending_count"])
        and not any(
            int(row[column])
            for column in (
                "claimed_count",
                "sent_count",
                "running_count",
                "success_count",
                "fail_count",
                "timeout_count",
                "error_count",
                "cancelled_count",
            )
        )
    )


def _normalize_recipes(recipes: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for alias, path in recipes:
        normalized_path = str(path).strip()
        if not normalized_path or normalized_path in seen:
            continue
        seen.add(normalized_path)
        result.append((str(alias).strip() or normalized_path, normalized_path))
    return result


def _count_by_folder(rows: Sequence[sqlite3.Row]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        folder_path = str(row["folder_path"])
        counts[folder_path] = counts.get(folder_path, 0) + 1
    return counts


def _now_text() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_datetime(value: Any) -> datetime | None:
    if value is None or not str(value):
        return None
    parsed = datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
