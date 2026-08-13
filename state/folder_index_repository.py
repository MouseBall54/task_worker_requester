"""Persistent favorite roots and bounded folder-name search index."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
import threading
from typing import Callable
from uuid import uuid4


@dataclass(frozen=True, slots=True)
class FavoriteRoot:
    path: str
    position: int
    online: bool
    last_checked: str | None


@dataclass(frozen=True, slots=True)
class FolderSearchResult:
    path: str
    name: str
    parent_path: str
    root_path: str
    root_online: bool


@dataclass(frozen=True, slots=True)
class FolderRefreshResult:
    root_path: str
    online: bool
    indexed_count: int
    removed_count: int
    complete: bool
    cancelled: bool = False


class FolderIndexRepository:
    """Store navigation data separately from RabbitMQ task state."""

    def __init__(self, database_path: str | Path = ":memory:") -> None:
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
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA busy_timeout = 30000")
        if self.database_path != ":memory:":
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute("PRAGMA synchronous = NORMAL")
        try:
            self._create_schema()
        except Exception:
            self._connection.close()
            raise

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _create_schema(self) -> None:
        with self._lock:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS favorite_roots(
                    path TEXT PRIMARY KEY,
                    position INTEGER NOT NULL,
                    added_at TEXT NOT NULL,
                    last_checked TEXT,
                    online INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS folder_index(
                    root_path TEXT NOT NULL,
                    path TEXT NOT NULL,
                    name TEXT NOT NULL,
                    name_norm TEXT NOT NULL,
                    parent_path TEXT NOT NULL,
                    path_norm TEXT NOT NULL,
                    mtime_ns INTEGER NOT NULL DEFAULT 0,
                    last_checked TEXT NOT NULL,
                    scan_token TEXT NOT NULL DEFAULT '',
                    exists_flag INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY(root_path, path),
                    FOREIGN KEY(root_path) REFERENCES favorite_roots(path) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_folder_index_name
                    ON folder_index(name_norm);
                CREATE INDEX IF NOT EXISTS idx_folder_index_parent
                    ON folder_index(root_path, parent_path);
                CREATE INDEX IF NOT EXISTS idx_folder_index_path
                    ON folder_index(path_norm);
                """
            )
            existing_columns = {
                str(row["name"])
                for row in self._connection.execute("PRAGMA table_info(folder_index)").fetchall()
            }
            if "exists_flag" not in existing_columns:
                self._connection.execute(
                    "ALTER TABLE folder_index ADD COLUMN exists_flag INTEGER NOT NULL DEFAULT 1"
                )
            self._connection.commit()

    def list_favorites(self) -> list[FavoriteRoot]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT path, position, online, last_checked FROM favorite_roots ORDER BY position"
            ).fetchall()
        return [
            FavoriteRoot(
                path=str(row["path"]),
                position=int(row["position"]),
                online=bool(row["online"]),
                last_checked=str(row["last_checked"]) if row["last_checked"] else None,
            )
            for row in rows
        ]

    def add_favorite(self, path: str) -> bool:
        normalized = _normalize_path(path)
        if not normalized:
            return False
        now = _now_text()
        with self._lock:
            next_position = int(
                self._connection.execute(
                    "SELECT COALESCE(MAX(position), -1) + 1 FROM favorite_roots"
                ).fetchone()[0]
            )
            cursor = self._connection.execute(
                """
                INSERT OR IGNORE INTO favorite_roots(
                    path, position, added_at, last_checked, online
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (normalized, next_position, now, now, int(os.path.isdir(normalized))),
            )
            self._connection.commit()
        return bool(cursor.rowcount)

    def remove_favorite(self, path: str) -> bool:
        normalized = _normalize_path(path)
        with self._lock:
            cursor = self._connection.execute(
                "DELETE FROM favorite_roots WHERE path = ?", (normalized,)
            )
            self._normalize_positions_locked()
            self._connection.commit()
        return bool(cursor.rowcount)

    def move_favorite(self, path: str, operation: str) -> bool:
        normalized = _normalize_path(path)
        with self._lock:
            order = [
                str(row[0])
                for row in self._connection.execute(
                    "SELECT path FROM favorite_roots ORDER BY position"
                ).fetchall()
            ]
            if normalized not in order or operation not in {"up", "down", "top", "bottom"}:
                return False
            current = order.index(normalized)
            target = {
                "up": max(0, current - 1),
                "down": min(len(order) - 1, current + 1),
                "top": 0,
                "bottom": len(order) - 1,
            }[operation]
            if current == target:
                return False
            order.pop(current)
            order.insert(target, normalized)
            self._connection.executemany(
                "UPDATE favorite_roots SET position = ? WHERE path = ?",
                [(position, root_path) for position, root_path in enumerate(order)],
            )
            self._connection.commit()
        return True

    def _normalize_positions_locked(self) -> None:
        rows = self._connection.execute(
            "SELECT path FROM favorite_roots ORDER BY position"
        ).fetchall()
        self._connection.executemany(
            "UPDATE favorite_roots SET position = ? WHERE path = ?",
            [(position, str(row[0])) for position, row in enumerate(rows)],
        )

    def search(self, query: str, limit: int = 200) -> list[FolderSearchResult]:
        normalized_query = query.strip().casefold()
        if not normalized_query:
            return []
        contains = f"%{_escape_like(normalized_query)}%"
        prefix = f"{_escape_like(normalized_query)}%"
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT i.path, i.name, i.parent_path, i.root_path, r.online
                FROM folder_index i
                JOIN favorite_roots r ON r.path = i.root_path
                WHERE i.exists_flag = 1
                  AND (i.name_norm LIKE ? ESCAPE '\\'
                       OR i.path_norm LIKE ? ESCAPE '\\')
                ORDER BY
                    CASE WHEN i.name_norm = ? THEN 0
                         WHEN i.name_norm LIKE ? ESCAPE '\\' THEN 1
                         ELSE 2 END,
                    i.name_norm, i.path_norm
                LIMIT ?
                """,
                (contains, contains, normalized_query, prefix, max(1, int(limit))),
            ).fetchall()
        return [
            FolderSearchResult(
                path=str(row["path"]),
                name=str(row["name"]),
                parent_path=str(row["parent_path"]),
                root_path=str(row["root_path"]),
                root_online=bool(row["online"]),
            )
            for row in rows
        ]

    def refresh_root(
        self,
        root_path: str,
        *,
        full: bool,
        should_cancel: Callable[[], bool] | None = None,
        progress: Callable[[int], None] | None = None,
    ) -> FolderRefreshResult:
        normalized_root = _normalize_path(root_path)
        cancelled = should_cancel or (lambda: False)
        if cancelled():
            return FolderRefreshResult(normalized_root, False, 0, 0, False, True)
        online = os.path.isdir(normalized_root)
        self._set_root_status(normalized_root, online)
        if not online:
            return FolderRefreshResult(normalized_root, False, 0, 0, False)
        if full or not self._has_index_rows(normalized_root):
            return self._full_refresh(normalized_root, cancelled, progress)
        return self._incremental_refresh(normalized_root, cancelled, progress)

    def _full_refresh(
        self,
        root_path: str,
        should_cancel: Callable[[], bool],
        progress: Callable[[int], None] | None,
    ) -> FolderRefreshResult:
        scan_token = str(uuid4())
        indexed = 0
        complete = True
        stack = [root_path]
        batch: list[tuple[str, str, str, int]] = []
        while stack:
            if should_cancel():
                self._upsert_batch(root_path, batch, scan_token)
                return FolderRefreshResult(root_path, True, indexed, 0, False, True)
            current = stack.pop()
            try:
                stat_result = os.stat(current, follow_symlinks=False)
                batch.append(
                    (current, os.path.basename(current) or current, os.path.dirname(current), stat_result.st_mtime_ns)
                )
                indexed += 1
                with os.scandir(current) as entries:
                    children = [
                        entry.path
                        for entry in entries
                        if entry.is_dir(follow_symlinks=False) and not entry.is_symlink()
                    ]
                stack.extend(reversed(children))
            except OSError:
                complete = False
            if len(batch) >= 500:
                self._upsert_batch(root_path, batch, scan_token)
                batch = []
                if progress:
                    progress(indexed)
        self._upsert_batch(root_path, batch, scan_token)
        removed = 0
        if complete:
            with self._lock:
                cursor = self._connection.execute(
                    "DELETE FROM folder_index WHERE root_path = ? AND scan_token != ?",
                    (root_path, scan_token),
                )
                removed = int(cursor.rowcount)
                self._connection.commit()
        self._set_root_status(root_path, True)
        if progress:
            progress(indexed)
        return FolderRefreshResult(root_path, True, indexed, removed, complete)

    def _incremental_refresh(
        self,
        root_path: str,
        should_cancel: Callable[[], bool],
        progress: Callable[[int], None] | None,
    ) -> FolderRefreshResult:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT path, parent_path, mtime_ns FROM folder_index
                WHERE root_path = ? ORDER BY length(path)
                """,
                (root_path,),
            ).fetchall()
        cached = {str(row["path"]): int(row["mtime_ns"]) for row in rows}
        children_by_parent: dict[str, set[str]] = {}
        for row in rows:
            children_by_parent.setdefault(str(row["parent_path"]), set()).add(str(row["path"]))
        checked = 0
        removed_paths: set[str] = set()
        new_roots: list[str] = []
        changed_records: list[tuple[str, str, str, int]] = []
        complete = True
        for path, old_mtime in cached.items():
            if should_cancel():
                return FolderRefreshResult(root_path, True, checked, 0, False, True)
            if any(path == missing or path.startswith(missing + os.sep) for missing in removed_paths):
                continue
            try:
                stat_result = os.stat(path, follow_symlinks=False)
            except FileNotFoundError:
                removed_paths.add(path)
                continue
            except OSError:
                complete = False
                continue
            checked += 1
            if stat_result.st_mtime_ns == old_mtime:
                continue
            changed_records.append(
                (path, os.path.basename(path) or path, os.path.dirname(path), stat_result.st_mtime_ns)
            )
            try:
                with os.scandir(path) as entries:
                    actual_children = {
                        entry.path
                        for entry in entries
                        if entry.is_dir(follow_symlinks=False) and not entry.is_symlink()
                    }
            except OSError:
                complete = False
                continue
            known_children = children_by_parent.get(path, set())
            removed_paths.update(known_children - actual_children)
            new_roots.extend(sorted(actual_children - known_children))
            if progress and checked % 500 == 0:
                progress(checked)
        token = str(uuid4())
        self._upsert_batch(root_path, changed_records, token)
        indexed = len(changed_records)
        for new_root in new_roots:
            if should_cancel():
                return FolderRefreshResult(root_path, True, checked + indexed, 0, False, True)
            discovered, subtree_complete = self._scan_new_subtree(
                root_path, new_root, token, should_cancel
            )
            indexed += discovered
            complete = complete and subtree_complete
        removed = self._delete_subtrees(root_path, removed_paths)
        self._set_root_status(root_path, True)
        if progress:
            progress(checked + indexed)
        return FolderRefreshResult(root_path, True, checked + indexed, removed, complete)

    def _scan_new_subtree(
        self,
        root_path: str,
        start_path: str,
        scan_token: str,
        should_cancel: Callable[[], bool],
    ) -> tuple[int, bool]:
        stack = [start_path]
        batch: list[tuple[str, str, str, int]] = []
        count = 0
        complete = True
        while stack:
            if should_cancel():
                complete = False
                break
            current = stack.pop()
            try:
                stat_result = os.stat(current, follow_symlinks=False)
                batch.append(
                    (current, os.path.basename(current) or current, os.path.dirname(current), stat_result.st_mtime_ns)
                )
                count += 1
                with os.scandir(current) as entries:
                    stack.extend(
                        entry.path
                        for entry in entries
                        if entry.is_dir(follow_symlinks=False) and not entry.is_symlink()
                    )
            except OSError:
                complete = False
            if len(batch) >= 500:
                self._upsert_batch(root_path, batch, scan_token)
                batch = []
        self._upsert_batch(root_path, batch, scan_token)
        return count, complete

    def _upsert_batch(
        self,
        root_path: str,
        records: list[tuple[str, str, str, int]],
        scan_token: str,
    ) -> None:
        if not records:
            return
        now = _now_text()
        rows = [
            (
                root_path,
                path,
                name,
                name.casefold(),
                parent_path,
                path.casefold(),
                int(mtime_ns),
                now,
                scan_token,
            )
            for path, name, parent_path, mtime_ns in records
        ]
        with self._lock:
            root_exists = self._connection.execute(
                "SELECT 1 FROM favorite_roots WHERE path = ?", (root_path,)
            ).fetchone()
            if root_exists is None:
                return
            self._connection.executemany(
                """
                INSERT INTO folder_index(
                    root_path, path, name, name_norm, parent_path, path_norm,
                    mtime_ns, last_checked, scan_token, exists_flag
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(root_path, path) DO UPDATE SET
                    name = excluded.name,
                    name_norm = excluded.name_norm,
                    parent_path = excluded.parent_path,
                    path_norm = excluded.path_norm,
                    mtime_ns = excluded.mtime_ns,
                    last_checked = excluded.last_checked,
                    scan_token = excluded.scan_token,
                    exists_flag = 1
                """,
                rows,
            )
            self._connection.commit()

    def _delete_subtrees(self, root_path: str, missing_paths: set[str]) -> int:
        if not missing_paths:
            return 0
        with self._lock:
            rows = self._connection.execute(
                "SELECT path FROM folder_index WHERE root_path = ?",
                (root_path,),
            ).fetchall()
            targets = [
                str(row["path"])
                for row in rows
                if any(
                    str(row["path"]) == missing
                    or str(row["path"]).startswith(missing + os.sep)
                    for missing in missing_paths
                )
            ]
            self._connection.executemany(
                "DELETE FROM folder_index WHERE root_path = ? AND path = ?",
                [(root_path, path) for path in targets],
            )
            self._connection.commit()
        return len(targets)

    def _set_root_status(self, root_path: str, online: bool) -> None:
        with self._lock:
            self._connection.execute(
                """
                UPDATE favorite_roots SET online = ?, last_checked = ? WHERE path = ?
                """,
                (int(online), _now_text(), root_path),
            )
            self._connection.commit()

    def _has_index_rows(self, root_path: str) -> bool:
        with self._lock:
            row = self._connection.execute(
                "SELECT 1 FROM folder_index WHERE root_path = ? LIMIT 1", (root_path,)
            ).fetchone()
        return row is not None


def _normalize_path(path: str) -> str:
    raw = os.path.expandvars(str(path).strip())
    if not raw:
        return ""
    return os.path.normpath(os.path.abspath(os.path.expanduser(raw)))


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _now_text() -> str:
    return datetime.now(timezone.utc).isoformat()
