"""Tests for persistent favorite roots and incremental folder search indexing."""

from __future__ import annotations

import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from state.folder_index_repository import (
    INDEX_INCOMPLETE,
    INDEX_READY,
    SCOPE_DEPTH,
    SCOPE_EXCLUDED,
    FolderIndexRepository,
    _filesystem_path,
    _safe_child_directory_names,
)


class FolderIndexRepositoryTest(unittest.TestCase):
    def test_index_schema_contains_required_navigation_metadata(self) -> None:
        with TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "folder_index.sqlite3"
            repository = FolderIndexRepository(database)
            repository.close()

            connection = sqlite3.connect(database)
            try:
                columns = {
                    str(row[1])
                    for row in connection.execute("PRAGMA table_info(folder_index)").fetchall()
                }
            finally:
                connection.close()

            self.assertTrue(
                {
                    "path",
                    "name",
                    "parent_path",
                    "mtime_ns",
                     "last_checked",
                     "exists_flag",
                     "scan_generation",
                     "depth",
                }.issubset(columns)
            )
            connection = sqlite3.connect(database)
            try:
                root_columns = {
                    str(row[1])
                    for row in connection.execute("PRAGMA table_info(favorite_roots)")
                }
                queue_columns = {
                    str(row[1])
                    for row in connection.execute("PRAGMA table_info(folder_scan_queue)")
                }
            finally:
                connection.close()
            self.assertTrue(
                {"scope_mode", "max_depth", "index_status", "scan_generation", "indexed_count", "pending_count", "error_count", "last_completed", "scan_kind"}.issubset(root_columns)
            )
            self.assertTrue(
                {"root_path", "folder_path", "depth", "state", "retry_count", "last_error", "generation", "updated_at", "work_kind"}.issubset(queue_columns)
            )

    def test_favorites_persist_and_reorder(self) -> None:
        with TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "folder_index.sqlite3"
            first = Path(temp_dir) / "first"
            second = Path(temp_dir) / "second"
            first.mkdir()
            second.mkdir()
            repository = FolderIndexRepository(database)
            self.assertTrue(repository.add_favorite(str(first)))
            self.assertTrue(repository.add_favorite(str(second)))
            self.assertFalse(repository.add_favorite(str(first)))
            self.assertTrue(repository.move_favorite(str(second), "up"))
            repository.close()

            restored = FolderIndexRepository(database)
            try:
                self.assertEqual(
                    [item.path for item in restored.list_favorites()],
                    [str(second), str(first)],
                )
                self.assertTrue(restored.remove_favorite(str(second)))
                self.assertEqual(
                    [(item.path, item.position) for item in restored.list_favorites()],
                    [(str(first), 0)],
                )
            finally:
                restored.close()

    def test_full_and_incremental_refresh_find_new_and_remove_deleted_folders(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "root"
            first = root / "alpha" / "nested"
            first.mkdir(parents=True)
            repository = FolderIndexRepository()
            try:
                repository.add_favorite(str(root))
                initial = repository.refresh_root(str(root), full=True)
                self.assertTrue(initial.complete)
                self.assertEqual(
                    [result.path for result in repository.search("nested")],
                    [str(first)],
                )

                new_folder = root / "beta" / "new_target"
                new_folder.mkdir(parents=True)
                os.utime(root, None)
                refreshed = repository.refresh_root(str(root), full=False)
                self.assertTrue(refreshed.complete)
                self.assertEqual(
                    [result.path for result in repository.search("new_target")],
                    [str(new_folder)],
                )

                new_folder.rmdir()
                os.utime(new_folder.parent, None)
                repository.refresh_root(str(root), full=False)
                self.assertEqual(repository.search("new_target"), [])
            finally:
                repository.close()

    def test_incremental_refresh_does_not_rescan_unchanged_directories(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "root"
            (root / "alpha" / "nested").mkdir(parents=True)
            repository = FolderIndexRepository()
            try:
                repository.add_favorite(str(root))
                repository.refresh_root(str(root), full=True)

                with patch(
                    "state.folder_index_repository._read_directory",
                    side_effect=AssertionError("unchanged directory was rescanned"),
                ):
                    refreshed = repository.refresh_root(str(root), full=False)

                self.assertTrue(refreshed.complete)
                self.assertEqual(refreshed.status, INDEX_READY)
                self.assertEqual(len(repository.search("nested")), 1)
            finally:
                repository.close()

    def test_offline_root_keeps_cached_results_and_reports_offline(self) -> None:
        with TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            root = base / "network_root"
            target = root / "cached_target"
            target.mkdir(parents=True)
            offline_root = base / "network_root_offline"
            repository = FolderIndexRepository()
            try:
                repository.add_favorite(str(root))
                repository.refresh_root(str(root), full=True)
                root.rename(offline_root)

                outcome = repository.refresh_root(str(root), full=False)
                results = repository.search("cached_target")

                self.assertFalse(outcome.online)
                self.assertEqual(len(results), 1)
                self.assertFalse(results[0].root_online)
            finally:
                repository.close()

    def test_cancelled_refresh_does_not_remove_cached_rows(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "root"
            (root / "kept").mkdir(parents=True)
            repository = FolderIndexRepository()
            try:
                repository.add_favorite(str(root))
                repository.refresh_root(str(root), full=True)

                outcome = repository.refresh_root(
                    str(root),
                    full=True,
                    should_cancel=lambda: True,
                )

                self.assertTrue(outcome.cancelled)
                self.assertEqual(len(repository.search("kept")), 1)
            finally:
                repository.close()

    def test_interrupted_scan_resumes_from_persistent_queue_after_restart(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "root"
            for index in range(12):
                (root / f"folder-{index}" / "nested").mkdir(parents=True)
            database = Path(temp_dir) / "folder_index.sqlite3"
            repository = FolderIndexRepository(database)
            repository.add_favorite(str(root))
            repository.prepare_full_scan(str(root))
            partial = repository.resume_scan(str(root), max_folders=3)
            self.assertEqual(partial.status, INDEX_INCOMPLETE)
            self.assertGreater(partial.pending_count, 0)
            partial_count = partial.indexed_count
            repository.close()

            restored = FolderIndexRepository(database)
            try:
                resumed = restored.resume_scan(str(root))
                self.assertEqual(resumed.status, INDEX_READY)
                self.assertEqual(resumed.pending_count, 0)
                self.assertGreater(resumed.indexed_count, partial_count)
                self.assertEqual(resumed.indexed_count, 25)
            finally:
                restored.close()

    def test_process_killed_while_batch_is_claimed_recovers_processing_rows(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "root"
            (root / "child").mkdir(parents=True)
            database = Path(temp_dir) / "folder_index.sqlite3"
            repository = FolderIndexRepository(database)
            repository.add_favorite(str(root))
            repository.prepare_full_scan(str(root))
            with repository._lock:  # simulate process death after claiming a batch
                repository._connection.execute(
                    "UPDATE folder_scan_queue SET state = 'PROCESSING' WHERE root_path = ?",
                    (str(root),),
                )
                repository._connection.execute(
                    "UPDATE favorite_roots SET index_status = 'INDEXING' WHERE path = ?",
                    (str(root),),
                )
                repository._connection.commit()
            repository.close()

            restored = FolderIndexRepository(database)
            try:
                favorite = restored.get_favorite(str(root))
                assert favorite is not None
                self.assertEqual(favorite.index_status, INDEX_INCOMPLETE)
                completed = restored.resume_scan(str(root))
                self.assertEqual(completed.status, INDEX_READY)
                self.assertEqual(completed.indexed_count, 2)
            finally:
                restored.close()

    def test_existing_database_cache_migrates_as_incomplete_without_data_loss(self) -> None:
        with TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "legacy.sqlite3"
            connection = sqlite3.connect(database)
            connection.executescript(
                """
                CREATE TABLE favorite_roots(
                    path TEXT PRIMARY KEY, position INTEGER NOT NULL,
                    added_at TEXT NOT NULL, last_checked TEXT, online INTEGER NOT NULL
                );
                CREATE TABLE folder_index(
                    root_path TEXT NOT NULL, path TEXT NOT NULL, name TEXT NOT NULL,
                    name_norm TEXT NOT NULL, parent_path TEXT NOT NULL,
                    path_norm TEXT NOT NULL, mtime_ns INTEGER NOT NULL,
                    last_checked TEXT NOT NULL, scan_token TEXT NOT NULL,
                    exists_flag INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY(root_path, path)
                );
                INSERT INTO favorite_roots VALUES('C:\\legacy', 0, 'now', 'now', 1);
                INSERT INTO folder_index VALUES(
                    'C:\\legacy', 'C:\\legacy\\cached', 'cached', 'cached',
                    'C:\\legacy', 'c:\\legacy\\cached', 1, 'now', '', 1
                );
                """
            )
            connection.commit()
            connection.close()

            repository = FolderIndexRepository(database)
            try:
                favorite = repository.list_favorites()[0]
                self.assertEqual(favorite.index_status, INDEX_INCOMPLETE)
                self.assertEqual(favorite.indexed_count, 1)
                self.assertEqual(len(repository.search("cached")), 1)
            finally:
                repository.close()

    def test_depth_limited_and_excluded_scopes_are_explicit(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "root"
            current = root
            for depth in range(1, 8):
                current = current / f"level-{depth}"
                current.mkdir(parents=True)
            repository = FolderIndexRepository()
            try:
                repository.add_favorite(str(root), scope_mode=SCOPE_DEPTH, max_depth=5)
                limited = repository.refresh_root(str(root), full=True)
                self.assertEqual(limited.status, INDEX_READY)
                self.assertEqual(limited.indexed_count, 6)
                self.assertEqual(repository.search("level-5")[0].path, str(root / "level-1" / "level-2" / "level-3" / "level-4" / "level-5"))
                self.assertEqual(repository.search("level-6"), [])

                repository.set_scope(str(root), SCOPE_EXCLUDED)
                self.assertEqual(repository.search("level-5"), [])
            finally:
                repository.close()

    def test_error_keeps_previous_generation_and_reports_incomplete(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "root"
            kept = root / "kept"
            kept.mkdir(parents=True)
            repository = FolderIndexRepository()
            try:
                repository.add_favorite(str(root))
                repository.refresh_root(str(root), full=True)
                with patch(
                    "state.folder_index_repository._read_directory",
                    side_effect=PermissionError("blocked"),
                ):
                    failed = repository.refresh_root(str(root), full=True)

                self.assertEqual(failed.status, INDEX_INCOMPLETE)
                self.assertEqual(failed.error_count, 1)
                self.assertEqual(len(repository.search("kept")), 1)
                errors = repository.list_errors(str(root))
                self.assertEqual(errors[0].error_kind, "ACCESS_DENIED")
            finally:
                repository.close()

    def test_pause_persists_and_resume_finishes_without_restarting_queue(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "root"
            for index in range(8):
                (root / f"folder-{index}").mkdir(parents=True)
            database = Path(temp_dir) / "folder_index.sqlite3"
            repository = FolderIndexRepository(database)
            repository.add_favorite(str(root))
            repository.prepare_full_scan(str(root))
            repository.resume_scan(str(root), max_folders=2)
            self.assertTrue(repository.pause_root(str(root)))
            repository.close()

            restored = FolderIndexRepository(database)
            try:
                paused = restored.get_favorite(str(root))
                assert paused is not None
                self.assertEqual(paused.index_status, "PAUSED")
                self.assertGreater(paused.pending_count, 0)
                self.assertTrue(restored.resume_root(str(root)))
                completed = restored.resume_scan(str(root))
                self.assertEqual(completed.status, INDEX_READY)
                self.assertEqual(completed.indexed_count, 9)
            finally:
                restored.close()

    def test_transient_error_can_be_retried_and_cleared(self) -> None:
        repository = FolderIndexRepository()
        root = r"C:\retry-root"
        calls = 0

        def read_directory(_path: str, _include_children: bool) -> tuple[int, list[str]]:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise PermissionError("temporarily blocked")
            return 1, []

        try:
            with (
                patch("state.folder_index_repository._is_directory", return_value=True),
                patch(
                    "state.folder_index_repository._read_directory",
                    side_effect=read_directory,
                ),
            ):
                repository.add_favorite(root)
                failed = repository.refresh_root(root, full=True)
                recovered = repository.refresh_root(root, full=False)

            self.assertEqual(failed.status, INDEX_INCOMPLETE)
            self.assertEqual(recovered.status, INDEX_READY)
            self.assertEqual(recovered.error_count, 0)
            self.assertEqual(repository.list_errors(root), [])
        finally:
            repository.close()

    def test_search_is_paginated_and_reports_total_count(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "root"
            for index in range(15):
                (root / f"target-{index:02d}").mkdir(parents=True)
            repository = FolderIndexRepository()
            try:
                repository.add_favorite(str(root))
                repository.refresh_root(str(root), full=True)
                first = repository.search_page("target", limit=6)
                second = repository.search_page("target", limit=6, offset=6)
                last = repository.search_page("target", limit=6, offset=12)

                self.assertEqual(first.total_count, 15)
                self.assertTrue(first.has_more)
                self.assertEqual(len(second.results), 6)
                self.assertEqual(len(last.results), 3)
                self.assertFalse(last.has_more)
            finally:
                repository.close()

    def test_virtual_tree_with_one_hundred_thousand_folders_is_complete(self) -> None:
        with TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "folder_index.sqlite3"
            repository = FolderIndexRepository(database)
            root = r"C:\virtual-root"
            child_names = [f"folder-{index:06d}" for index in range(100_000)]

            def read_directory(path: str, include_children: bool) -> tuple[int, list[str]]:
                return 1, child_names if path == root and include_children else []

            try:
                with (
                    patch("state.folder_index_repository._is_directory", return_value=True),
                    patch(
                        "state.folder_index_repository._read_directory",
                        side_effect=read_directory,
                    ),
                ):
                    repository.add_favorite(root)
                    outcome = repository.refresh_root(root, full=True)

                self.assertEqual(outcome.status, INDEX_READY)
                self.assertEqual(outcome.indexed_count, 100_001)
                self.assertEqual(outcome.pending_count, 0)
                self.assertEqual(outcome.error_count, 0)
                page = repository.search_page("folder-099999", limit=20)
                self.assertEqual(page.total_count, 1)
                self.assertTrue(page.results[0].path.endswith("folder-099999"))
            finally:
                repository.close()

    def test_virtual_tree_with_one_thousand_levels_avoids_recursion_limit(self) -> None:
        repository = FolderIndexRepository()
        root = r"C:\deep-root"

        def read_directory(path: str, include_children: bool) -> tuple[int, list[str]]:
            depth = path.count(os.sep) - root.count(os.sep)
            return 1, ([f"level-{depth + 1}"] if include_children and depth < 1000 else [])

        try:
            with (
                patch("state.folder_index_repository._is_directory", return_value=True),
                patch(
                    "state.folder_index_repository._read_directory",
                    side_effect=read_directory,
                ),
            ):
                repository.add_favorite(root)
                outcome = repository.refresh_root(root, full=True)

            self.assertEqual(outcome.status, INDEX_READY)
            self.assertEqual(outcome.indexed_count, 1001)
        finally:
            repository.close()

    def test_windows_filesystem_path_uses_extended_prefix_without_changing_cache_path(self) -> None:
        with patch("state.folder_index_repository.os.name", "nt"):
            self.assertEqual(
                _filesystem_path(r"C:\data\deep"),
                r"\\?\C:\data\deep",
            )
            self.assertEqual(
                _filesystem_path(r"\\server\share\deep"),
                r"\\?\UNC\server\share\deep",
            )

    def test_reparse_points_are_skipped_to_prevent_cycles(self) -> None:
        class FakeEntry:
            def __init__(self, name: str, *, reparse: bool = False) -> None:
                self.name = name
                self.reparse = reparse

            def is_dir(self, *, follow_symlinks: bool) -> bool:
                self.assert_follow = follow_symlinks
                return True

            def is_symlink(self) -> bool:
                return False

        normal = FakeEntry("normal")
        junction = FakeEntry("junction", reparse=True)
        with patch(
            "state.folder_index_repository._is_reparse_point",
            side_effect=lambda entry: entry.reparse,
        ):
            children = _safe_child_directory_names([normal, junction])

        self.assertEqual(children, ["normal"])
        self.assertFalse(normal.assert_follow)


if __name__ == "__main__":
    unittest.main()
