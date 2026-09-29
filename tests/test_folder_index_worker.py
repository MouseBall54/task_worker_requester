"""Tests for the background favorite-root index worker contract."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from services.workers.folder_index_worker import FolderIndexWorker
from state.folder_index_repository import FolderIndexRepository


class FolderIndexWorkerTest(unittest.TestCase):
    def test_worker_refreshes_index_without_owning_search_results(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "root"
            target = root / "fresh_target"
            target.mkdir(parents=True)
            repository = FolderIndexRepository()
            repository.add_favorite(str(root))
            worker = FolderIndexWorker(
                repository,
                [str(root)],
                full=True,
            )
            payloads: list[dict] = []
            failures: list[str] = []
            worker.completed.connect(payloads.append)
            worker.failed.connect(failures.append)
            try:
                worker.run()

                self.assertEqual(failures, [])
                self.assertEqual(len(payloads), 1)
                self.assertNotIn("results", payloads[0])
                self.assertEqual(repository.search("fresh_target")[0].path, str(target))
                self.assertFalse(payloads[0]["cancelled"])
            finally:
                repository.close()

    def test_worker_cancel_preserves_existing_cache(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "root"
            (root / "cached").mkdir(parents=True)
            repository = FolderIndexRepository()
            repository.add_favorite(str(root))
            repository.refresh_root(str(root), full=True)
            worker = FolderIndexWorker(repository, [str(root)], full=True)
            payloads: list[dict] = []
            worker.completed.connect(payloads.append)
            try:
                worker.cancel()
                worker.run()

                self.assertTrue(payloads[0]["cancelled"])
                self.assertEqual(len(repository.search("cached")), 1)
            finally:
                repository.close()


if __name__ == "__main__":
    unittest.main()
