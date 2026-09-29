"""Background streaming folder scanner for disk-backed task registration."""

from __future__ import annotations

from collections.abc import Callable
import os

from PySide6.QtCore import QObject, Signal, Slot

from services.folder_scanner import FolderScanner


class ScanWorker(QObject):
    """Stream one folder into bounded task batches without blocking the GUI."""

    batch_inserted = Signal(str, int, int)
    scan_completed = Signal(str, int)
    scan_stopped = Signal(str, int)
    scan_failed = Signal(str, str)
    access_issues_found = Signal(str, int)
    finished = Signal()

    def __init__(
        self,
        scanner: FolderScanner,
        folder_path: str,
        insert_batch: Callable[[str, list[str]], int],
        batch_size: int = 1000,
        queue_key: str | None = None,
    ) -> None:
        super().__init__()
        self._scanner = scanner
        self._scan_path = folder_path
        self._folder_path = queue_key or folder_path
        self._insert_batch = insert_batch
        self._batch_size = max(1, int(batch_size))
        self._stop_requested = False

    @Slot()
    def run(self) -> None:
        total_inserted = 0
        inaccessible_count = 0
        batch: list[str] = []
        try:
            for image_path in self._scanner.iter_images(self._scan_path):
                if self._stop_requested:
                    break
                if os.path.exists(image_path) and not os.access(image_path, os.R_OK):
                    inaccessible_count += 1
                    continue
                batch.append(image_path)
                if len(batch) < self._batch_size:
                    continue
                inserted = self._insert_batch(self._folder_path, batch)
                total_inserted += inserted
                self.batch_inserted.emit(self._folder_path, inserted, total_inserted)
                batch = []

            if batch:
                inserted = self._insert_batch(self._folder_path, batch)
                total_inserted += inserted
                self.batch_inserted.emit(self._folder_path, inserted, total_inserted)
            if self._stop_requested:
                self.scan_stopped.emit(self._folder_path, total_inserted)
            else:
                self.access_issues_found.emit(self._folder_path, inaccessible_count)
                self.scan_completed.emit(self._folder_path, total_inserted)
        except Exception as exc:  # pylint: disable=broad-except
            self.scan_failed.emit(self._folder_path, str(exc))
        finally:
            self.finished.emit()

    @Slot()
    def stop(self) -> None:
        self._stop_requested = True
