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
        query: str = "",
        search_limit: int = 200,
    ) -> None:
        super().__init__()
        self._repository = repository
        self._root_paths = list(dict.fromkeys(root_paths))
        self._full = bool(full)
        self._query = query.strip()
        self._search_limit = max(1, int(search_limit))
        self._cancelled = threading.Event()

    @Slot()
    def run(self) -> None:
        outcomes = []
        try:
            for root_path in self._root_paths:
                if self._cancelled.is_set():
                    break
                result = self._repository.refresh_root(
                    root_path,
                    full=self._full,
                    should_cancel=self._cancelled.is_set,
                    progress=lambda count, path=root_path: self.progress.emit(path, count),
                )
                outcomes.append(result)
                self.root_status.emit(root_path, result.online)
            results = (
                self._repository.search(self._query, limit=self._search_limit)
                if self._query and not self._cancelled.is_set()
                else []
            )
            self.completed.emit(
                {
                    "query": self._query,
                    "results": results,
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
