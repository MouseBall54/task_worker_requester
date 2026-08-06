"""SQLite-backed task persistence for bounded-memory large job runs."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any
from uuid import uuid4

from models.task_models import FolderSummary, ImageTask, TaskStatus
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
        """Delete all folders/tasks in the current session while keeping its identity."""

        with self._transaction() as cursor:
            cursor.execute("DELETE FROM folders WHERE session_id = ?", (self.session_id,))
            cursor.execute(
                """
                UPDATE sessions SET state = 'ACTIVE', updated_at = ?, action = NULL,
                    result_queue = NULL, priority = NULL, polling_interval = NULL
                WHERE session_id = ?
                """,
                (_now_text(), self.session_id),
            )

    def save_runtime_settings(
        self,
        action: str,
        result_queue: str,
        priority: int,
        polling_interval: int,
    ) -> None:
        """Persist session-level settings needed for deterministic restart recovery."""

        with self._transaction() as cursor:
            cursor.execute(
                """
                UPDATE sessions SET action = ?, result_queue = ?, priority = ?,
                    polling_interval = ?
                WHERE session_id = ?
                """,
                (
                    action,
                    result_queue,
                    max(0, int(priority)),
                    max(1, int(polling_interval)),
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

    def register_folder_descriptors(
        self,
        folder_paths: Sequence[str],
        recipe_selections: Sequence[tuple[str, str]],
    ) -> int:
        """Register lightweight folder descriptors without enumerating images."""

        normalized_recipes = _normalize_recipes(recipe_selections)
        if not normalized_recipes:
            return 0
        recipes_json = json.dumps(normalized_recipes, ensure_ascii=False)
        added = 0
        with self._transaction() as cursor:
            next_position = int(
                cursor.execute(
                    "SELECT COALESCE(MAX(position), -1) + 1 FROM folders WHERE session_id = ?",
                    (self.session_id,),
                ).fetchone()[0]
            )
            for folder_path in dict.fromkeys(str(path).strip() for path in folder_paths if str(path).strip()):
                cursor.execute(
                    """
                    INSERT OR IGNORE INTO folders(
                        session_id, folder_path, position, recipes_json, scan_state
                    ) VALUES (?, ?, ?, ?, 'WAITING')
                    """,
                    (self.session_id, folder_path, next_position, recipes_json),
                )
                if cursor.rowcount:
                    added += 1
                    next_position += 1
        return added

    def list_folder_descriptors(self, states: Sequence[str] | None = None) -> list[sqlite3.Row]:
        query = "SELECT * FROM folders WHERE session_id = ?"
        params: list[Any] = [self.session_id]
        if states:
            placeholders = ",".join("?" for _ in states)
            query += f" AND scan_state IN ({placeholders})"
            params.extend(states)
        query += " ORDER BY position"
        with self._lock:
            return list(self._connection.execute(query, params).fetchall())

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
                polling_interval INTEGER
            );
            CREATE TABLE IF NOT EXISTS folders(
                session_id TEXT NOT NULL,
                folder_path TEXT NOT NULL,
                position INTEGER NOT NULL,
                recipes_json TEXT NOT NULL,
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
        }
        for column, definition in definitions.items():
            if column not in existing:
                self._connection.execute(
                    f"ALTER TABLE sessions ADD COLUMN {column} {definition}"
                )

    def _resume_or_create_session(self) -> str:
        row = self._connection.execute(
            """
            SELECT session_id FROM sessions
            WHERE state = 'ACTIVE' ORDER BY updated_at DESC LIMIT 1
            """
        ).fetchone()
        if row is not None:
            return str(row[0])
        session_id = str(uuid4())
        now = _now_text()
        self._connection.execute(
            "INSERT INTO sessions(session_id, created_at, updated_at, state) VALUES (?, ?, ?, 'ACTIVE')",
            (session_id, now, now),
        )
        self._connection.commit()
        return session_id

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
    if scan_state == "WAITING":
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
