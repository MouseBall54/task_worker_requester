"""Tests for bounded streaming scan batches."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from services.folder_scanner import FolderScanner
from services.workers.scan_worker import ScanWorker


class ScanWorkerTest(unittest.TestCase):
    def test_worker_inserts_bounded_batches(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for index in range(12):
                (root / f"{index}.jpg").write_text("x", encoding="utf-8")

            batch_sizes: list[int] = []
            completions: list[int] = []

            def insert_batch(_folder_path: str, paths: list[str]) -> int:
                batch_sizes.append(len(paths))
                return len(paths)

            worker = ScanWorker(
                scanner=FolderScanner([".jpg"]),
                folder_path=str(root),
                insert_batch=insert_batch,
                batch_size=5,
            )
            worker.scan_completed.connect(lambda _path, total: completions.append(total))

            worker.run()

            self.assertEqual(batch_sizes, [5, 5, 2])
            self.assertEqual(completions, [12])

    def test_stop_flushes_partial_batch_and_reports_incomplete_scan(self) -> None:
        inserted_batches: list[list[str]] = []
        stopped: list[tuple[str, int]] = []
        completed: list[tuple[str, int]] = []
        worker: ScanWorker

        class StoppingScanner:
            @staticmethod
            def iter_images(_folder_path: str):
                yield "a.jpg"
                yield "b.jpg"
                worker.stop()
                yield "c.jpg"

        def insert_batch(_folder_path: str, image_paths: list[str]) -> int:
            inserted_batches.append(list(image_paths))
            return len(image_paths)

        worker = ScanWorker(StoppingScanner(), "folder", insert_batch, batch_size=10)  # type: ignore[arg-type]
        worker.scan_stopped.connect(lambda path, count: stopped.append((path, count)))
        worker.scan_completed.connect(lambda path, count: completed.append((path, count)))

        worker.run()

        self.assertEqual(inserted_batches, [["a.jpg", "b.jpg"]])
        self.assertEqual(stopped, [("folder", 2)])
        self.assertEqual(completed, [])


if __name__ == "__main__":
    unittest.main()
