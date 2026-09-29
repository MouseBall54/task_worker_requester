"""Tests for background subfolder descriptor discovery."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from services.folder_scanner import FolderScanner
from services.workers.folder_discovery_worker import FolderDiscoveryWorker


class FolderDiscoveryWorkerTest(unittest.TestCase):
    def test_discovery_registers_bounded_folder_batches(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for index in range(7):
                folder = root / f"folder_{index}"
                folder.mkdir()
                (folder / "image.jpg").write_text("x", encoding="utf-8")

            batches: list[int] = []
            completed: list[int] = []

            def register(paths: list[str], _recipes: list[tuple[str, str]]) -> int:
                batches.append(len(paths))
                return len(paths)

            worker = FolderDiscoveryWorker(
                scanner=FolderScanner([".jpg"]),
                root_paths=[str(root)],
                mode="direct",
                recipe_selections=[("R", "r.json")],
                register_batch=register,
                batch_size=3,
            )
            worker.discovery_completed.connect(completed.append)

            worker.run()

            self.assertEqual(batches, [3, 3, 1])
            self.assertEqual(completed, [7])


if __name__ == "__main__":
    unittest.main()
