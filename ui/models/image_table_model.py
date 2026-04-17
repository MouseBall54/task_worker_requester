"""Image task detail table model."""

from __future__ import annotations

from datetime import datetime
import os

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt

from models.task_models import ImageTask


class ImageTableModel(QAbstractTableModel):
    """Model for per-image detailed task status."""

    HEADERS = ["MQ", "이미지", "상태", "전송 시각", "완료 시각", "결과", "에러"]

    def __init__(self) -> None:
        super().__init__()
        self._rows: list[ImageTask] = []
        self._index_map: dict[str, int] = {}

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        if parent.isValid():
            return 0
        return len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        if parent.isValid():
            return 0
        return len(self.HEADERS)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole) -> str | None:
        if not index.isValid():
            return None

        task = self._rows[index.row()]
        column = index.column()

        if role == Qt.DisplayRole:
            if column == 0:
                return "보기"
            if column == 1:
                return os.path.basename(task.image_path) or task.image_path
            if column == 2:
                return task.status.value
            if column == 3:
                return self._format_datetime(task.sent_at)
            if column == 4:
                return self._format_datetime(task.completed_at)
            if column == 5:
                return ", ".join(task.result)
            if column == 6:
                return task.error_message or ""

        if role == Qt.UserRole:
            return task.request_id

        return None

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.DisplayRole,
    ) -> str | None:
        if role != Qt.DisplayRole:
            return None
        if orientation == Qt.Horizontal:
            return self.HEADERS[section]
        return str(section + 1)

    def clear(self) -> None:
        """Clear all rows."""

        self.beginResetModel()
        self._rows = []
        self._index_map = {}
        self.endResetModel()

    def set_tasks(self, tasks: list[ImageTask]) -> None:
        """Replace full row list for selected folder."""

        self.beginResetModel()
        self._rows = list(tasks)
        self._index_map = {task.request_id: idx for idx, task in enumerate(self._rows)}
        self.endResetModel()

    def update_task(self, task: ImageTask) -> None:
        """Update one row if it exists in current filtered view."""

        row = self._index_map.get(task.request_id)
        if row is None:
            return
        self._rows[row] = task
        start = self.index(row, 0)
        end = self.index(row, self.columnCount() - 1)
        self.dataChanged.emit(start, end, [Qt.DisplayRole])

    @staticmethod
    def _format_datetime(value: datetime | None) -> str:
        if value is None:
            return ""
        return value.astimezone().strftime("%Y-%m-%d %H:%M:%S")
