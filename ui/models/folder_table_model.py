"""Folder progress table model and delegates."""

from __future__ import annotations

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QStyledItemDelegate, QStyleOptionProgressBar, QApplication, QStyle

from models.task_models import FolderSummary


class FolderTableModel(QAbstractTableModel):
    """Table model that tracks folder-level progress rows."""

    HEADERS = ["폴더", "총", "완료", "성공", "실패", "타임아웃", "진행률", "상태"]

    def __init__(self) -> None:
        super().__init__()
        self._rows: list[FolderSummary] = []
        self._index_map: dict[str, int] = {}

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        if parent.isValid():
            return 0
        return len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        if parent.isValid():
            return 0
        return len(self.HEADERS)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole) -> str | int | float | None:
        if not index.isValid():
            return None

        row = self._rows[index.row()]
        column = index.column()

        if role == Qt.DisplayRole:
            if column == 0:
                return row.folder_path
            if column == 1:
                return row.total
            if column == 2:
                return row.completed
            if column == 3:
                return row.success
            if column == 4:
                return row.fail + row.error
            if column == 5:
                return row.timeout
            if column == 6:
                return round(row.progress, 1)
            if column == 7:
                return row.status.value

        if role == Qt.UserRole:
            return row

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
        """Reset all rows."""

        self.beginResetModel()
        self._rows = []
        self._index_map = {}
        self.endResetModel()

    def set_rows(self, rows: list[FolderSummary]) -> None:
        """Replace all rows at once."""

        self.beginResetModel()
        self._rows = list(rows)
        self._index_map = {row.folder_path: idx for idx, row in enumerate(self._rows)}
        self.endResetModel()

    def upsert_summary(self, summary: FolderSummary) -> None:
        """Insert or update one folder row efficiently."""

        row_idx = self._index_map.get(summary.folder_path)
        if row_idx is None:
            insert_at = len(self._rows)
            self.beginInsertRows(QModelIndex(), insert_at, insert_at)
            self._rows.append(summary)
            self._index_map[summary.folder_path] = insert_at
            self.endInsertRows()
            return

        self._rows[row_idx] = summary
        start = self.index(row_idx, 0)
        end = self.index(row_idx, self.columnCount() - 1)
        self.dataChanged.emit(start, end, [Qt.DisplayRole, Qt.UserRole])

    def folder_at(self, row: int) -> str | None:
        """Return folder path for selected row."""

        if 0 <= row < len(self._rows):
            return self._rows[row].folder_path
        return None


class ProgressBarDelegate(QStyledItemDelegate):
    """Paint folder progress percentage as native progress bar."""

    def paint(self, painter: QPainter, option, index: QModelIndex) -> None:  # type: ignore[override]
        value = index.data(Qt.DisplayRole)
        try:
            progress = max(0, min(100, int(float(value))))
        except (TypeError, ValueError):
            progress = 0

        progress_option = QStyleOptionProgressBar()
        progress_option.rect = option.rect.adjusted(6, 6, -6, -6)
        progress_option.minimum = 0
        progress_option.maximum = 100
        progress_option.progress = progress
        progress_option.text = f"{progress}%"
        progress_option.textVisible = True
        progress_option.state = option.state

        QApplication.style().drawControl(QStyle.CE_ProgressBar, progress_option, painter)
