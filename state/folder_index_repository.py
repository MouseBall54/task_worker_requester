"""Persistent, resumable folder indexing for favorite navigation roots."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import sqlite3
import stat
import threading
from typing import Callable
from uuid import uuid4

from utils.time_utils import format_seoul_iso


INDEX_EMPTY = "EMPTY"
INDEX_INDEXING = "INDEXING"
INDEX_PAUSED = "PAUSED"
INDEX_INCOMPLETE = "INCOMPLETE"
INDEX_READY = "READY"
INDEX_OFFLINE = "OFFLINE"
INDEX_EXCLUDED = "EXCLUDED"

SCOPE_FULL = "FULL"
SCOPE_DEPTH = "DEPTH"
SCOPE_EXCLUDED = "EXCLUDED"

QUEUE_PENDING = "PENDING"
QUEUE_PROCESSING = "PROCESSING"
QUEUE_DONE = "DONE"
QUEUE_ERROR = "ERROR"


@dataclass(frozen=True, slots=True)
class FavoriteRoot:
    path: str
    position: int
    online: bool
    last_checked: str | None
    scope_mode: str = SCOPE_FULL
    max_depth: int | None = None
    index_status: str = INDEX_EMPTY
    indexed_count: int = 0
    pending_count: int = 0
    error_count: int = 0
    last_completed: str | None = None


@dataclass(frozen=True, slots=True)
class FolderSearchResult:
    path: str
    name: str
    parent_path: str
    root_path: str
    root_online: bool


@dataclass(frozen=True, slots=True)
class FolderSearchPage:
    results: list[FolderSearchResult]
    total_count: int
    offset: int
    limit: int

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.results) < self.total_count


@dataclass(frozen=True, slots=True)
class FolderRefreshResult:
    root_path: str
    online: bool
    indexed_count: int
    removed_count: int
    complete: bool
    cancelled: bool = False
    pending_count: int = 0
    error_count: int = 0
    status: str = INDEX_EMPTY


@dataclass(frozen=True, slots=True)
class FolderIndexError:
    root_path: str
    folder_path: str
    error_kind: str
    message: str
    retry_count: int
    updated_at: str


@dataclass(slots=True)
class _ScanSuccess:
    folder_path: str
    depth: int
    generation: str
    mtime_ns: int | None
    children: list[str]
    removed_paths: list[str]
    update_index: bool


class FolderIndexRepository:
    """Store complete folder navigation state separately from task state."""

    SCHEMA_VERSION = 3
    DEFAULT_BATCH_SIZE = 500

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
        self._fts_available = False
        try:
            self._create_schema()
            self._recover_interrupted_scans()
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
                    scan_generation TEXT NOT NULL DEFAULT '',
                    depth INTEGER NOT NULL DEFAULT 0,
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
            root_columns = self._table_columns("favorite_roots")
            additions = {
                "scope_mode": "TEXT NOT NULL DEFAULT 'FULL'",
                "max_depth": "INTEGER",
                "index_status": "TEXT NOT NULL DEFAULT 'INCOMPLETE'",
                "scan_generation": "TEXT NOT NULL DEFAULT ''",
                "indexed_count": "INTEGER NOT NULL DEFAULT 0",
                "pending_count": "INTEGER NOT NULL DEFAULT 0",
                "error_count": "INTEGER NOT NULL DEFAULT 0",
                "last_completed": "TEXT",
                "scan_kind": "TEXT NOT NULL DEFAULT 'FULL'",
            }
            for column, definition in additions.items():
                if column not in root_columns:
                    self._connection.execute(
                        f"ALTER TABLE favorite_roots ADD COLUMN {column} {definition}"
                    )
            index_columns = self._table_columns("folder_index")
            if "exists_flag" not in index_columns:
                self._connection.execute(
                    "ALTER TABLE folder_index ADD COLUMN exists_flag INTEGER NOT NULL DEFAULT 1"
                )
            if "scan_generation" not in index_columns:
                self._connection.execute(
                    "ALTER TABLE folder_index ADD COLUMN scan_generation TEXT NOT NULL DEFAULT ''"
                )
            if "depth" not in index_columns:
                self._connection.execute(
                    "ALTER TABLE folder_index ADD COLUMN depth INTEGER NOT NULL DEFAULT 0"
                )
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS folder_scan_queue(
                    root_path TEXT NOT NULL,
                    folder_path TEXT NOT NULL,
                    depth INTEGER NOT NULL,
                    state TEXT NOT NULL DEFAULT 'PENDING',
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    generation TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    work_kind TEXT NOT NULL DEFAULT 'DISCOVER',
                    PRIMARY KEY(root_path, folder_path),
                    FOREIGN KEY(root_path) REFERENCES favorite_roots(path) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_folder_scan_pending
                    ON folder_scan_queue(root_path, generation, state, depth);
                CREATE TABLE IF NOT EXISTS folder_index_errors(
                    root_path TEXT NOT NULL,
                    folder_path TEXT NOT NULL,
                    error_kind TEXT NOT NULL,
                    message TEXT NOT NULL,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    generation TEXT NOT NULL,
                    blocking INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(root_path, folder_path, generation),
                    FOREIGN KEY(root_path) REFERENCES favorite_roots(path) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS folder_index_meta(
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            queue_columns = self._table_columns("folder_scan_queue")
            if "work_kind" not in queue_columns:
                self._connection.execute(
                    "ALTER TABLE folder_scan_queue ADD COLUMN work_kind TEXT NOT NULL DEFAULT 'DISCOVER'"
                )
            previous_version_row = self._connection.execute(
                "SELECT value FROM folder_index_meta WHERE key = 'schema_version'"
            ).fetchone()
            previous_version = int(previous_version_row[0]) if previous_version_row else 0
            if previous_version < self.SCHEMA_VERSION:
                self._connection.execute(
                    """
                    UPDATE favorite_roots
                    SET index_status = CASE
                        WHEN scope_mode = 'EXCLUDED' THEN 'EXCLUDED'
                        WHEN EXISTS(SELECT 1 FROM folder_index i WHERE i.root_path = favorite_roots.path)
                            THEN 'INCOMPLETE'
                        ELSE 'EMPTY'
                    END,
                    indexed_count = (
                        SELECT COUNT(*) FROM folder_index i WHERE i.root_path = favorite_roots.path
                    )
                    """
                )
            self._connection.execute(
                "INSERT OR REPLACE INTO folder_index_meta(key, value) VALUES('schema_version', ?)",
                (str(self.SCHEMA_VERSION),),
            )
            self._create_fts_locked()
            self._connection.commit()

    def _table_columns(self, table: str) -> set[str]:
        return {
            str(row["name"])
            for row in self._connection.execute(f"PRAGMA table_info({table})").fetchall()
        }

    def _create_fts_locked(self) -> None:
        try:
            self._connection.executescript(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS folder_index_fts USING fts5(
                    name_norm, path_norm, tokenize='trigram'
                );
                CREATE TRIGGER IF NOT EXISTS folder_index_ai AFTER INSERT ON folder_index BEGIN
                    INSERT INTO folder_index_fts(rowid, name_norm, path_norm)
                    VALUES (new.rowid, new.name_norm, new.path_norm);
                END;
                CREATE TRIGGER IF NOT EXISTS folder_index_ad AFTER DELETE ON folder_index BEGIN
                    DELETE FROM folder_index_fts WHERE rowid = old.rowid;
                END;
                CREATE TRIGGER IF NOT EXISTS folder_index_au AFTER UPDATE ON folder_index BEGIN
                    DELETE FROM folder_index_fts WHERE rowid = old.rowid;
                    INSERT INTO folder_index_fts(rowid, name_norm, path_norm)
                    VALUES (new.rowid, new.name_norm, new.path_norm);
                END;
                """
            )
            index_count = int(
                self._connection.execute("SELECT COUNT(*) FROM folder_index").fetchone()[0]
            )
            fts_count = int(
                self._connection.execute("SELECT COUNT(*) FROM folder_index_fts").fetchone()[0]
            )
            if index_count != fts_count:
                self._connection.execute("DELETE FROM folder_index_fts")
                self._connection.execute(
                    """
                    INSERT INTO folder_index_fts(rowid, name_norm, path_norm)
                    SELECT rowid, name_norm, path_norm FROM folder_index
                    """
                )
            self._fts_available = True
        except sqlite3.OperationalError:
            self._fts_available = False

    def _recover_interrupted_scans(self) -> None:
        """Make process-killed scan work resumable without losing cached rows."""

        with self._lock:
            now = _now_text()
            self._connection.execute(
                "UPDATE folder_scan_queue SET state = 'PENDING', updated_at = ? WHERE state = 'PROCESSING'",
                (now,),
            )
            self._connection.execute(
                "UPDATE favorite_roots SET index_status = 'INCOMPLETE' WHERE index_status = 'INDEXING'"
            )
            self._refresh_all_counts_locked()
            self._connection.commit()

    def list_favorites(self) -> list[FavoriteRoot]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT path, position, online, last_checked, scope_mode, max_depth,
                       index_status, indexed_count, pending_count, error_count, last_completed
                FROM favorite_roots ORDER BY position
                """
            ).fetchall()
        return [self._favorite_from_row(row) for row in rows]

    def get_favorite(self, path: str) -> FavoriteRoot | None:
        normalized = _normalize_path(path)
        with self._lock:
            row = self._connection.execute(
                """
                SELECT path, position, online, last_checked, scope_mode, max_depth,
                       index_status, indexed_count, pending_count, error_count, last_completed
                FROM favorite_roots WHERE path = ?
                """,
                (normalized,),
            ).fetchone()
        return self._favorite_from_row(row) if row else None

    @staticmethod
    def _favorite_from_row(row: sqlite3.Row) -> FavoriteRoot:
        return FavoriteRoot(
            path=str(row["path"]),
            position=int(row["position"]),
            online=bool(row["online"]),
            last_checked=str(row["last_checked"]) if row["last_checked"] else None,
            scope_mode=str(row["scope_mode"]),
            max_depth=int(row["max_depth"]) if row["max_depth"] is not None else None,
            index_status=str(row["index_status"]),
            indexed_count=int(row["indexed_count"]),
            pending_count=int(row["pending_count"]),
            error_count=int(row["error_count"]),
            last_completed=str(row["last_completed"]) if row["last_completed"] else None,
        )

    def add_favorite(
        self,
        path: str,
        *,
        scope_mode: str = SCOPE_FULL,
        max_depth: int | None = None,
    ) -> bool:
        normalized = _normalize_path(path)
        if not normalized:
            return False
        scope_mode, max_depth = _validate_scope(scope_mode, max_depth)
        now = _now_text()
        online = _is_directory(normalized)
        with self._lock:
            next_position = int(
                self._connection.execute(
                    "SELECT COALESCE(MAX(position), -1) + 1 FROM favorite_roots"
                ).fetchone()[0]
            )
            cursor = self._connection.execute(
                """
                INSERT OR IGNORE INTO favorite_roots(
                    path, position, added_at, last_checked, online, scope_mode,
                    max_depth, index_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized,
                    next_position,
                    now,
                    now,
                    int(online),
                    scope_mode,
                    max_depth,
                    INDEX_EXCLUDED
                    if scope_mode == SCOPE_EXCLUDED
                    else (INDEX_EMPTY if online else INDEX_OFFLINE),
                ),
            )
            self._connection.commit()
        return bool(cursor.rowcount)

    def set_scope(self, path: str, scope_mode: str, max_depth: int | None = None) -> bool:
        normalized = _normalize_path(path)
        scope_mode, max_depth = _validate_scope(scope_mode, max_depth)
        with self._lock:
            cursor = self._connection.execute(
                "UPDATE favorite_roots SET scope_mode = ?, max_depth = ? WHERE path = ?",
                (scope_mode, max_depth, normalized),
            )
            if not cursor.rowcount:
                return False
            self._connection.execute(
                "DELETE FROM folder_scan_queue WHERE root_path = ?", (normalized,)
            )
            self._connection.execute(
                "DELETE FROM folder_index_errors WHERE root_path = ?", (normalized,)
            )
            self._connection.execute(
                """
                UPDATE favorite_roots
                SET index_status = ?, pending_count = 0, error_count = 0
                WHERE path = ?
                """,
                (INDEX_EXCLUDED if scope_mode == SCOPE_EXCLUDED else INDEX_INCOMPLETE, normalized),
            )
            self._connection.commit()
        return True

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
        return self.search_page(query, limit=limit).results

    def search_page(self, query: str, *, limit: int = 200, offset: int = 0) -> FolderSearchPage:
        normalized_query = query.strip().casefold()
        safe_limit = max(1, min(1000, int(limit)))
        safe_offset = max(0, int(offset))
        if not normalized_query:
            return FolderSearchPage([], 0, safe_offset, safe_limit)
        contains = f"%{_escape_like(normalized_query)}%"
        prefix = f"{_escape_like(normalized_query)}%"
        with self._lock:
            if self._fts_available and len(normalized_query) >= 3:
                match_query = '"' + normalized_query.replace('"', '""') + '"'
                from_clause = "folder_index_fts f JOIN folder_index i ON i.rowid = f.rowid"
                match_clause = "f.folder_index_fts MATCH ?"
                match_params: tuple[object, ...] = (match_query,)
            else:
                from_clause = "folder_index i"
                match_clause = "(i.name_norm LIKE ? ESCAPE '\\' OR i.path_norm LIKE ? ESCAPE '\\')"
                match_params = (contains, contains)
            common_where = (
                f"{match_clause} AND i.exists_flag = 1 AND r.scope_mode != 'EXCLUDED'"
            )
            total = int(
                self._connection.execute(
                    f"""
                    SELECT COUNT(*) FROM {from_clause}
                    JOIN favorite_roots r ON r.path = i.root_path
                    WHERE {common_where}
                    """,
                    match_params,
                ).fetchone()[0]
            )
            rows = self._connection.execute(
                f"""
                SELECT i.path, i.name, i.parent_path, i.root_path, r.online
                FROM {from_clause}
                JOIN favorite_roots r ON r.path = i.root_path
                WHERE {common_where}
                ORDER BY CASE WHEN i.name_norm = ? THEN 0
                              WHEN i.name_norm LIKE ? ESCAPE '\\' THEN 1
                              ELSE 2 END,
                         i.name_norm, i.path_norm
                LIMIT ? OFFSET ?
                """,
                (*match_params, normalized_query, prefix, safe_limit, safe_offset),
            ).fetchall()
        results = [
            FolderSearchResult(
                path=str(row["path"]),
                name=str(row["name"]),
                parent_path=str(row["parent_path"]),
                root_path=str(row["root_path"]),
                root_online=bool(row["online"]),
            )
            for row in rows
        ]
        return FolderSearchPage(results, total, safe_offset, safe_limit)

    def prepare_full_scan(self, root_path: str) -> bool:
        root_path = _normalize_path(root_path)
        favorite = self.get_favorite(root_path)
        if favorite is None or favorite.scope_mode == SCOPE_EXCLUDED:
            return False
        online = _is_directory(root_path)
        self._set_root_status(root_path, online)
        if not online:
            return False
        generation = str(uuid4())
        now = _now_text()
        with self._lock:
            self._connection.execute("DELETE FROM folder_scan_queue WHERE root_path = ?", (root_path,))
            self._connection.execute("DELETE FROM folder_index_errors WHERE root_path = ?", (root_path,))
            self._connection.execute(
                """
                INSERT INTO folder_scan_queue(
                    root_path, folder_path, depth, state, generation, updated_at, work_kind
                ) VALUES (?, ?, 0, 'PENDING', ?, ?, 'DISCOVER')
                """,
                (root_path, root_path, generation, now),
            )
            self._connection.execute(
                """
                UPDATE favorite_roots
                SET scan_generation = ?, index_status = 'INDEXING', pending_count = 1,
                    error_count = 0, last_checked = ?, online = 1, scan_kind = 'FULL'
                WHERE path = ?
                """,
                (generation, now, root_path),
            )
            self._connection.commit()
        return True

    def prepare_incremental_scan(self, root_path: str) -> bool:
        """Persist a low-load verification queue for all currently cached folders."""

        root_path = _normalize_path(root_path)
        favorite = self.get_favorite(root_path)
        if favorite is None or favorite.scope_mode == SCOPE_EXCLUDED:
            return False
        if not _is_directory(root_path):
            self._set_root_status(root_path, False)
            return False
        with self._lock:
            cached_count = int(
                self._connection.execute(
                    "SELECT COUNT(*) FROM folder_index WHERE root_path = ? AND exists_flag = 1",
                    (root_path,),
                ).fetchone()[0]
            )
        if cached_count == 0:
            return self.prepare_full_scan(root_path)
        generation = str(uuid4())
        now = _now_text()
        with self._lock:
            self._connection.execute("DELETE FROM folder_scan_queue WHERE root_path = ?", (root_path,))
            self._connection.execute("DELETE FROM folder_index_errors WHERE root_path = ?", (root_path,))
            self._connection.execute(
                """
                INSERT INTO folder_scan_queue(
                    root_path, folder_path, depth, state, generation, updated_at, work_kind
                )
                SELECT root_path, path, depth, 'PENDING', ?, ?, 'VERIFY'
                FROM folder_index WHERE root_path = ? AND exists_flag = 1
                """,
                (generation, now, root_path),
            )
            self._connection.execute(
                """
                UPDATE favorite_roots
                SET scan_generation = ?, scan_kind = 'INCREMENTAL', index_status = 'INDEXING',
                    pending_count = ?, error_count = 0, last_checked = ?, online = 1
                WHERE path = ?
                """,
                (generation, cached_count, now, root_path),
            )
            self._connection.commit()
        return True

    def resume_scan(
        self,
        root_path: str,
        *,
        max_folders: int | None = None,
        should_cancel: Callable[[], bool] | None = None,
        progress: Callable[[int], None] | None = None,
        retry_errors: bool = False,
    ) -> FolderRefreshResult:
        root_path = _normalize_path(root_path)
        cancelled = should_cancel or (lambda: False)
        favorite = self.get_favorite(root_path)
        if favorite is None:
            return FolderRefreshResult(root_path, False, 0, 0, False, status=INDEX_EMPTY)
        if favorite.scope_mode == SCOPE_EXCLUDED:
            return FolderRefreshResult(
                root_path, favorite.online, favorite.indexed_count, 0, True,
                status=INDEX_EXCLUDED,
            )
        if not _is_directory(root_path):
            self._set_root_status(root_path, False)
            return self._result_for_root(root_path, online=False)
        if favorite.index_status == INDEX_PAUSED and not retry_errors:
            return self._result_for_root(root_path, online=True)
        if retry_errors:
            self._retry_errors(root_path)
            favorite = self.get_favorite(root_path) or favorite
        elif (
            self._has_scan_queue(root_path)
            and favorite.pending_count == 0
            and favorite.error_count > 0
        ):
            # A bounded periodic reconciliation starts a fresh generation after
            # an exhausted/error-only generation instead of remaining stuck forever.
            if self._scan_kind(root_path) == "INCREMENTAL":
                self.prepare_incremental_scan(root_path)
            else:
                self.prepare_full_scan(root_path)
        if not self._has_scan_queue(root_path):
            if favorite.index_status == INDEX_READY:
                self.prepare_incremental_scan(root_path)
            else:
                self.prepare_full_scan(root_path)
        with self._lock:
            self._connection.execute(
                "UPDATE favorite_roots SET index_status = 'INDEXING', online = 1 WHERE path = ?",
                (root_path,),
            )
            self._connection.commit()

        processed = 0
        removed = 0
        batch_limit = self.DEFAULT_BATCH_SIZE
        remaining_limit = None if max_folders is None else max(0, int(max_folders))
        while remaining_limit is None or processed < remaining_limit:
            if cancelled():
                self._mark_scan_interrupted(root_path)
                result = self._result_for_root(root_path, online=True)
                return FolderRefreshResult(
                    root_path=result.root_path,
                    online=result.online,
                    indexed_count=result.indexed_count,
                    removed_count=0,
                    complete=False,
                    cancelled=True,
                    pending_count=result.pending_count,
                    error_count=result.error_count,
                    status=result.status,
                )
            take = batch_limit if remaining_limit is None else min(batch_limit, remaining_limit - processed)
            if take <= 0:
                break
            rows = self._claim_pending_batch(root_path, take)
            if not rows:
                break
            successes: list[_ScanSuccess] = []
            failures: list[tuple[str, str, int, str, str]] = []
            for row in rows:
                if cancelled():
                    self._return_processing_to_pending(root_path)
                    self._mark_scan_interrupted(root_path)
                    result = self._result_for_root(root_path, online=True)
                    return FolderRefreshResult(
                        root_path=result.root_path,
                        online=result.online,
                        indexed_count=result.indexed_count,
                        removed_count=0,
                        complete=False,
                        cancelled=True,
                        pending_count=result.pending_count,
                        error_count=result.error_count,
                        status=result.status,
                    )
                success, failure = self._inspect_queue_folder(root_path, row)
                if success is not None:
                    successes.append(success)
                if failure is not None:
                    failures.append(failure)
                processed += 1
            self._commit_scan_batch(root_path, successes, failures)
            if progress:
                progress(processed)

        removed = self._finalize_if_complete(root_path)
        if progress:
            progress(processed)
        result = self._result_for_root(root_path, online=True)
        return FolderRefreshResult(
            root_path=result.root_path,
            online=result.online,
            indexed_count=result.indexed_count,
            removed_count=removed,
            complete=result.complete,
            cancelled=False,
            pending_count=result.pending_count,
            error_count=result.error_count,
            status=result.status,
        )

    def refresh_root(
        self,
        root_path: str,
        *,
        full: bool,
        should_cancel: Callable[[], bool] | None = None,
        progress: Callable[[int], None] | None = None,
    ) -> FolderRefreshResult:
        """Compatibility entry point: full reindex or resumable reconciliation."""

        normalized = _normalize_path(root_path)
        if full:
            self.prepare_full_scan(normalized)
        return self.resume_scan(
            normalized,
            should_cancel=should_cancel,
            progress=progress,
            retry_errors=not full,
        )

    def pause_root(self, root_path: str) -> bool:
        root_path = _normalize_path(root_path)
        with self._lock:
            cursor = self._connection.execute(
                """
                UPDATE favorite_roots SET index_status = 'PAUSED'
                WHERE path = ? AND scope_mode != 'EXCLUDED'
                """,
                (root_path,),
            )
            self._connection.execute(
                "UPDATE folder_scan_queue SET state = 'PENDING' WHERE root_path = ? AND state = 'PROCESSING'",
                (root_path,),
            )
            self._refresh_counts_locked(root_path)
            self._connection.commit()
        return bool(cursor.rowcount)

    def resume_root(self, root_path: str) -> bool:
        root_path = _normalize_path(root_path)
        with self._lock:
            cursor = self._connection.execute(
                """
                UPDATE favorite_roots SET index_status = 'INCOMPLETE'
                WHERE path = ? AND index_status IN ('PAUSED', 'INCOMPLETE', 'OFFLINE')
                """,
                (root_path,),
            )
            self._connection.commit()
        return bool(cursor.rowcount)

    def list_errors(self, root_path: str) -> list[FolderIndexError]:
        root_path = _normalize_path(root_path)
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT root_path, folder_path, error_kind, message, retry_count, updated_at
                FROM folder_index_errors WHERE root_path = ?
                ORDER BY updated_at DESC, folder_path
                """,
                (root_path,),
            ).fetchall()
        return [FolderIndexError(**dict(row)) for row in rows]

    def mark_path_missing(self, root_path: str, folder_path: str) -> int:
        """Remove a verified-missing cached subtree and mark its root incomplete."""

        root_path = _normalize_path(root_path)
        folder_path = _normalize_path(folder_path)
        prefix = folder_path.rstrip("\\/") + os.sep
        with self._lock:
            rows = self._connection.execute(
                "SELECT path FROM folder_index WHERE root_path = ?",
                (root_path,),
            ).fetchall()
            targets = [
                str(row[0])
                for row in rows
                if str(row[0]) == folder_path or str(row[0]).startswith(prefix)
            ]
            self._connection.executemany(
                "DELETE FROM folder_index WHERE root_path = ? AND path = ?",
                [(root_path, path) for path in targets],
            )
            self._connection.execute(
                """
                UPDATE favorite_roots
                SET index_status = CASE WHEN scope_mode = 'EXCLUDED' THEN index_status ELSE 'INCOMPLETE' END
                WHERE path = ?
                """,
                (root_path,),
            )
            self._refresh_counts_locked(root_path)
            self._connection.commit()
        return len(targets)

    @staticmethod
    def path_is_directory(path: str) -> bool:
        return _is_directory(path)

    def _claim_pending_batch(self, root_path: str, limit: int) -> list[sqlite3.Row]:
        with self._lock:
            root = self._connection.execute(
                "SELECT scan_generation FROM favorite_roots WHERE path = ?", (root_path,)
            ).fetchone()
            if root is None:
                return []
            generation = str(root[0])
            rows = self._connection.execute(
                """
                SELECT folder_path, depth, retry_count, generation, work_kind
                FROM folder_scan_queue
                WHERE root_path = ? AND generation = ? AND state = 'PENDING'
                ORDER BY depth, folder_path LIMIT ?
                """,
                (root_path, generation, limit),
            ).fetchall()
            self._connection.executemany(
                """
                UPDATE folder_scan_queue SET state = 'PROCESSING', updated_at = ?
                WHERE root_path = ? AND folder_path = ?
                """,
                [(_now_text(), root_path, str(row["folder_path"])) for row in rows],
            )
            self._connection.commit()
        return rows

    def _inspect_queue_folder(
        self,
        root_path: str,
        row: sqlite3.Row,
    ) -> tuple[
        _ScanSuccess | None,
        tuple[str, str, int, str, str] | None,
    ]:
        folder_path = str(row["folder_path"])
        depth = int(row["depth"])
        generation = str(row["generation"])
        retry_count = int(row["retry_count"])
        work_kind = str(row["work_kind"])
        favorite = self.get_favorite(root_path)
        if favorite is None:
            return None, None
        try:
            if work_kind == "VERIFY":
                old_row = self._folder_index_row(root_path, folder_path)
                if old_row is None:
                    return _ScanSuccess(
                        folder_path, depth, generation, None, [], [], False
                    ), None
                mtime_ns = _stat_directory(folder_path)
                if mtime_ns == int(old_row["mtime_ns"]):
                    return _ScanSuccess(
                        folder_path, depth, generation, None, [], [], False
                    ), None
                include_children = favorite.max_depth is None or depth < favorite.max_depth
                mtime_ns, child_names = _read_directory(folder_path, include_children)
                actual_children = {
                    os.path.join(folder_path, name) for name in child_names
                }
                known_children = self._known_child_paths(root_path, folder_path)
                return _ScanSuccess(
                    folder_path=folder_path,
                    depth=depth,
                    generation=generation,
                    mtime_ns=mtime_ns,
                    children=sorted(actual_children - known_children),
                    removed_paths=sorted(known_children - actual_children),
                    update_index=True,
                ), None
            include_children = favorite.max_depth is None or depth < favorite.max_depth
            mtime_ns, child_names = _read_directory(folder_path, include_children)
            children = [os.path.join(folder_path, name) for name in child_names]
            return _ScanSuccess(
                folder_path, depth, generation, mtime_ns, children, [], True
            ), None
        except FileNotFoundError as exc:
            if work_kind == "VERIFY":
                return _ScanSuccess(
                    folder_path, depth, generation, None, [], [folder_path], False
                ), None
            return None, (
                folder_path, generation, retry_count + 1, "DISAPPEARED", str(exc)
            )
        except OSError as exc:
            return None, (
                folder_path,
                generation,
                retry_count + 1,
                _classify_os_error(exc),
                str(exc),
            )

    def _commit_scan_batch(
        self,
        root_path: str,
        successes: list[_ScanSuccess],
        failures: list[tuple[str, str, int, str, str]],
    ) -> None:
        now = _now_text()
        with self._lock:
            self._connection.execute("BEGIN")
            try:
                self._connection.executemany(
                    """
                    INSERT INTO folder_index(
                        root_path, path, name, name_norm, parent_path, path_norm,
                        mtime_ns, last_checked, scan_token, exists_flag, scan_generation, depth
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                    ON CONFLICT(root_path, path) DO UPDATE SET
                        name = excluded.name, name_norm = excluded.name_norm,
                        parent_path = excluded.parent_path, path_norm = excluded.path_norm,
                        mtime_ns = excluded.mtime_ns, last_checked = excluded.last_checked,
                        scan_token = excluded.scan_token, exists_flag = 1,
                        scan_generation = excluded.scan_generation, depth = excluded.depth
                    """,
                    [
                        (
                            root_path,
                            folder_path,
                            os.path.basename(folder_path) or folder_path,
                            (os.path.basename(folder_path) or folder_path).casefold(),
                            os.path.dirname(folder_path),
                            folder_path.casefold(),
                            mtime_ns,
                            now,
                            generation,
                            generation,
                            success.depth,
                        )
                        for success in successes
                        if success.update_index and success.mtime_ns is not None
                        for folder_path, generation, mtime_ns in [
                            (success.folder_path, success.generation, success.mtime_ns)
                        ]
                    ],
                )
                self._connection.executemany(
                    """
                    INSERT INTO folder_scan_queue(
                        root_path, folder_path, depth, state, generation, updated_at, work_kind
                    ) VALUES (?, ?, ?, 'PENDING', ?, ?, 'DISCOVER')
                    ON CONFLICT(root_path, folder_path) DO NOTHING
                    """,
                    (
                        (root_path, child, depth + 1, generation, now)
                        for success in successes
                        for child in success.children
                        for depth, generation in [(success.depth, success.generation)]
                    ),
                )
                self._connection.executemany(
                    """
                    UPDATE folder_scan_queue SET state = 'DONE', last_error = NULL, updated_at = ?
                    WHERE root_path = ? AND folder_path = ?
                    """,
                    [(now, root_path, success.folder_path) for success in successes],
                )
                self._connection.executemany(
                    "DELETE FROM folder_index_errors WHERE root_path = ? AND folder_path = ? AND generation = ?",
                    [
                        (root_path, folder_path, generation)
                        for success in successes
                        for folder_path, generation in [(success.folder_path, success.generation)]
                    ],
                )
                removed_paths = [
                    path for success in successes for path in success.removed_paths
                ]
                self._delete_subtrees_locked(root_path, removed_paths)
                self._connection.executemany(
                    """
                    INSERT INTO folder_index_errors(
                        root_path, folder_path, error_kind, message, retry_count,
                        generation, blocking, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 1, ?)
                    ON CONFLICT(root_path, folder_path, generation) DO UPDATE SET
                        error_kind = excluded.error_kind, message = excluded.message,
                        retry_count = excluded.retry_count, updated_at = excluded.updated_at
                    """,
                    [
                        (root_path, folder_path, kind, message, retry, generation, now)
                        for folder_path, generation, retry, kind, message in failures
                    ],
                )
                self._connection.executemany(
                    """
                    UPDATE folder_scan_queue
                    SET state = 'ERROR', retry_count = ?, last_error = ?, updated_at = ?
                    WHERE root_path = ? AND folder_path = ?
                    """,
                    [
                        (retry, message, now, root_path, folder_path)
                        for folder_path, _generation, retry, _kind, message in failures
                    ],
                )
                self._refresh_counts_locked(root_path)
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

    def _retry_errors(self, root_path: str) -> None:
        with self._lock:
            self._connection.execute(
                """
                UPDATE folder_scan_queue SET state = 'PENDING', updated_at = ?
                WHERE root_path = ? AND state = 'ERROR' AND retry_count < 3
                """,
                (_now_text(), root_path),
            )
            self._refresh_counts_locked(root_path)
            self._connection.commit()

    def _finalize_if_complete(self, root_path: str) -> int:
        with self._lock:
            root = self._connection.execute(
                "SELECT scan_generation, scan_kind FROM favorite_roots WHERE path = ?", (root_path,)
            ).fetchone()
            if root is None:
                return 0
            generation = str(root[0])
            scan_kind = str(root[1])
            pending = int(
                self._connection.execute(
                    """
                    SELECT COUNT(*) FROM folder_scan_queue
                    WHERE root_path = ? AND generation = ? AND state IN ('PENDING', 'PROCESSING')
                    """,
                    (root_path, generation),
                ).fetchone()[0]
            )
            errors = int(
                self._connection.execute(
                    """
                    SELECT COUNT(*) FROM folder_index_errors
                    WHERE root_path = ? AND generation = ? AND blocking = 1
                    """,
                    (root_path, generation),
                ).fetchone()[0]
            )
            removed = 0
            now = _now_text()
            if pending == 0 and errors == 0:
                if scan_kind == "FULL":
                    cursor = self._connection.execute(
                        "DELETE FROM folder_index WHERE root_path = ? AND scan_generation != ?",
                        (root_path, generation),
                    )
                    removed = int(cursor.rowcount)
                self._connection.execute(
                    "DELETE FROM folder_scan_queue WHERE root_path = ?", (root_path,)
                )
                self._connection.execute(
                    "DELETE FROM folder_index_errors WHERE root_path = ?", (root_path,)
                )
                self._connection.execute(
                    """
                    UPDATE favorite_roots
                    SET index_status = 'READY', pending_count = 0, error_count = 0,
                        last_completed = ?, last_checked = ?, online = 1
                    WHERE path = ?
                    """,
                    (now, now, root_path),
                )
            elif pending == 0:
                self._connection.execute(
                    "UPDATE favorite_roots SET index_status = 'INCOMPLETE' WHERE path = ?",
                    (root_path,),
                )
            else:
                self._connection.execute(
                    "UPDATE favorite_roots SET index_status = 'INCOMPLETE' WHERE path = ?",
                    (root_path,),
                )
            self._refresh_counts_locked(root_path)
            self._connection.commit()
        return removed

    def _result_for_root(self, root_path: str, *, online: bool) -> FolderRefreshResult:
        favorite = self.get_favorite(root_path)
        if favorite is None:
            return FolderRefreshResult(root_path, online, 0, 0, False, status=INDEX_EMPTY)
        return FolderRefreshResult(
            root_path=root_path,
            online=online,
            indexed_count=favorite.indexed_count,
            removed_count=0,
            complete=favorite.index_status in {INDEX_READY, INDEX_EXCLUDED},
            pending_count=favorite.pending_count,
            error_count=favorite.error_count,
            status=favorite.index_status,
        )

    def _mark_scan_interrupted(self, root_path: str) -> None:
        with self._lock:
            self._connection.execute(
                "UPDATE favorite_roots SET index_status = 'INCOMPLETE' WHERE path = ? AND index_status = 'INDEXING'",
                (root_path,),
            )
            self._refresh_counts_locked(root_path)
            self._connection.commit()

    def _return_processing_to_pending(self, root_path: str) -> None:
        with self._lock:
            self._connection.execute(
                "UPDATE folder_scan_queue SET state = 'PENDING', updated_at = ? WHERE root_path = ? AND state = 'PROCESSING'",
                (_now_text(), root_path),
            )
            self._connection.commit()

    def _set_root_status(self, root_path: str, online: bool) -> None:
        with self._lock:
            self._connection.execute(
                """
                UPDATE favorite_roots
                SET online = ?, last_checked = ?,
                    index_status = CASE
                        WHEN ? = 0 AND scope_mode != 'EXCLUDED' THEN 'OFFLINE'
                        WHEN ? = 1 AND index_status = 'OFFLINE' THEN 'INCOMPLETE'
                        ELSE index_status END
                WHERE path = ?
                """,
                (int(online), _now_text(), int(online), int(online), root_path),
            )
            self._connection.commit()

    def _has_scan_queue(self, root_path: str) -> bool:
        with self._lock:
            return self._connection.execute(
                "SELECT 1 FROM folder_scan_queue WHERE root_path = ? LIMIT 1", (root_path,)
            ).fetchone() is not None

    def _scan_kind(self, root_path: str) -> str:
        with self._lock:
            row = self._connection.execute(
                "SELECT scan_kind FROM favorite_roots WHERE path = ?", (root_path,)
            ).fetchone()
        return str(row[0]) if row else "FULL"

    def _folder_index_row(self, root_path: str, folder_path: str) -> sqlite3.Row | None:
        with self._lock:
            return self._connection.execute(
                "SELECT mtime_ns FROM folder_index WHERE root_path = ? AND path = ?",
                (root_path, folder_path),
            ).fetchone()

    def _known_child_paths(self, root_path: str, parent_path: str) -> set[str]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT path FROM folder_index WHERE root_path = ? AND parent_path = ?",
                (root_path, parent_path),
            ).fetchall()
        return {str(row[0]) for row in rows}

    def _delete_subtrees_locked(self, root_path: str, missing_paths: list[str]) -> int:
        if not missing_paths:
            return 0
        rows = self._connection.execute(
            "SELECT path FROM folder_index WHERE root_path = ?", (root_path,)
        ).fetchall()
        targets = [
            str(row[0])
            for row in rows
            if any(
                str(row[0]) == missing
                or str(row[0]).startswith(missing.rstrip("\\/") + os.sep)
                for missing in missing_paths
            )
        ]
        self._connection.executemany(
            "DELETE FROM folder_index WHERE root_path = ? AND path = ?",
            [(root_path, path) for path in targets],
        )
        return len(targets)

    def _refresh_counts_locked(self, root_path: str) -> None:
        self._connection.execute(
            """
            UPDATE favorite_roots SET
                indexed_count = (SELECT COUNT(*) FROM folder_index WHERE root_path = ?),
                pending_count = (
                    SELECT COUNT(*) FROM folder_scan_queue
                    WHERE root_path = ? AND state IN ('PENDING', 'PROCESSING')
                ),
                error_count = (
                    SELECT COUNT(*) FROM folder_index_errors
                    WHERE root_path = ? AND blocking = 1
                )
            WHERE path = ?
            """,
            (root_path, root_path, root_path, root_path),
        )

    def _refresh_all_counts_locked(self) -> None:
        roots = self._connection.execute("SELECT path FROM favorite_roots").fetchall()
        for row in roots:
            self._refresh_counts_locked(str(row[0]))


def _normalize_path(path: str) -> str:
    raw = os.path.expandvars(str(path).strip())
    if not raw:
        return ""
    return os.path.normpath(os.path.abspath(os.path.expanduser(raw)))


def _filesystem_path(path: str) -> str:
    """Use Windows extended paths internally while preserving display paths."""

    normalized = _normalize_path(path)
    if os.name != "nt" or normalized.startswith("\\\\?\\"):
        return normalized
    if normalized.startswith("\\\\"):
        return "\\\\?\\UNC\\" + normalized.lstrip("\\")
    return "\\\\?\\" + normalized


def _is_directory(path: str) -> bool:
    return os.path.isdir(_filesystem_path(path))


def _is_reparse_point(entry: os.DirEntry[str]) -> bool:
    try:
        attributes = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
        return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    except OSError:
        return False


def _read_directory(path: str, include_children: bool) -> tuple[int, list[str]]:
    """Return directory mtime and safe child directory names for one display path."""

    fs_path = _filesystem_path(path)
    stat_result = os.stat(fs_path, follow_symlinks=False)
    children: list[str] = []
    if include_children:
        with os.scandir(fs_path) as entries:
            children = _safe_child_directory_names(entries)
    return int(stat_result.st_mtime_ns), children


def _stat_directory(path: str) -> int:
    return int(os.stat(_filesystem_path(path), follow_symlinks=False).st_mtime_ns)


def _safe_child_directory_names(entries) -> list[str]:  # noqa: ANN001
    """Exclude links/reparse points and surface unreadable entries as scan errors."""

    children: list[str] = []
    for entry in entries:
        if not entry.is_dir(follow_symlinks=False):
            continue
        if entry.is_symlink() or _is_reparse_point(entry):
            continue
        children.append(entry.name)
    return children


def _classify_os_error(error: OSError) -> str:
    if isinstance(error, PermissionError):
        return "ACCESS_DENIED"
    if isinstance(error, FileNotFoundError):
        return "DISAPPEARED"
    winerror = getattr(error, "winerror", None)
    if winerror in {206, 123}:
        return "LONG_PATH"
    if winerror in {53, 64, 67, 121, 1231}:
        return "NETWORK"
    return "IO_ERROR"


def _validate_scope(scope_mode: str, max_depth: int | None) -> tuple[str, int | None]:
    mode = str(scope_mode).upper()
    if mode not in {SCOPE_FULL, SCOPE_DEPTH, SCOPE_EXCLUDED}:
        raise ValueError(f"Unsupported folder index scope: {scope_mode}")
    if mode == SCOPE_DEPTH:
        depth = 5 if max_depth is None else max(1, int(max_depth))
        return mode, depth
    return mode, None


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _now_text() -> str:
    return format_seoul_iso()
