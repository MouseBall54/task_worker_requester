"""Background discovery of image-containing subfolders."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from PySide6.QtCore import QObject, Signal, Slot

from services.folder_scanner import FolderScanner


class FolderDiscoveryWorker(QObject):
    """Discover folder descriptors in bounded batches outside the GUI thread."""

    batch_registered = Signal(int, int)
    discovery_completed = Signal(int)
    discovery_failed = Signal(str)
    finished = Signal()

    def __init__(
        self,
        scanner: FolderScanner,
        root_paths: Sequence[str],
        mode: str,
        recipe_selections: Sequence[tuple[str, str]],
        register_batch: Callable[[list[str], list[tuple[str, str]]], int],
        batch_size: int = 100,
    ) -> None:
        super().__init__()
        self._scanner = scanner
        self._root_paths = list(root_paths)
        self._mode = mode
        self._recipe_selections = list(recipe_selections)
        self._register_batch = register_batch
        self._batch_size = max(1, int(batch_size))
        self._stop_requested = False

    @Slot()
    def run(self) -> None:
        batch: list[str] = []
        seen: set[str] = set()
        total_added = 0
        try:
            for root_path in self._root_paths:
                for folder_path in self._scanner.discover_image_folders(root_path, self._mode):
                    if self._stop_requested:
                        break
                    if folder_path in seen:
                        continue
                    seen.add(folder_path)
                    batch.append(folder_path)
                    if len(batch) < self._batch_size:
                        continue
                    added = self._register_batch(batch, self._recipe_selections)
                    total_added += added
                    self.batch_registered.emit(added, total_added)
                    batch = []
                if self._stop_requested:
                    break
            if batch and not self._stop_requested:
                added = self._register_batch(batch, self._recipe_selections)
                total_added += added
                self.batch_registered.emit(added, total_added)
            self.discovery_completed.emit(total_added)
        except Exception as exc:  # pylint: disable=broad-except
            self.discovery_failed.emit(str(exc))
        finally:
            self.finished.emit()

    @Slot()
    def stop(self) -> None:
        self._stop_requested = True
