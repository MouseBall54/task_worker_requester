"""Tests for persistent favorite roots and incremental folder search indexing."""

from __future__ import annotations

import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from state.folder_index_repository import FolderIndexRepository


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
                }.issubset(columns)
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


if __name__ == "__main__":
    unittest.main()
