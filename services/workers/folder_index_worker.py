"""Background refresh/search worker for favorite folder roots."""

from __future__ import annotations

import threading

from PySide6.QtCore import QObject, Signal, Slot

from state.folder_index_repository import FolderIndexRepository


class FolderIndexWorker(QObject):
    """Refresh folder metadata without blocking the Qt GUI thread."""

    progress = Signal(str, int)
    root_status = Signal(str, bool)
    completed = Signal(object)
    failed = Signal(str)
    finished = Signal()

    def __init__(
        self,
        repository: FolderIndexRepository,
        root_paths: list[str],
        *,
        full: bool = False,
        max_folders_per_root: int | None = None,
        retry_errors: bool = False,
    ) -> None:
        super().__init__()
        self._repository = repository
        self._root_paths = list(dict.fromkeys(root_paths))
        self._full = bool(full)
        self._max_folders_per_root = max_folders_per_root
        self._retry_errors = bool(retry_errors)
        self._cancelled = threading.Event()

    @Slot()
    def run(self) -> None:
        outcomes = []
        try:
            for root_path in self._root_paths:
                if self._cancelled.is_set():
                    break
                if self._full:
                    self._repository.prepare_full_scan(root_path)
                result = self._repository.resume_scan(
                    root_path,
                    max_folders=self._max_folders_per_root,
                    should_cancel=self._cancelled.is_set,
                    progress=lambda count, path=root_path: self.progress.emit(path, count),
                    retry_errors=self._retry_errors,
                )
                outcomes.append(result)
                self.root_status.emit(root_path, result.online)
            self.completed.emit(
                {
                    "outcomes": outcomes,
                    "cancelled": self._cancelled.is_set(),
                }
            )
        except Exception as exc:  # pylint: disable=broad-except
            self.failed.emit(str(exc))
        finally:
            self.finished.emit()

    @Slot()
    def cancel(self) -> None:
        self._cancelled.set()
