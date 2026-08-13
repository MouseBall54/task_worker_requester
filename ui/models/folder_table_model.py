"""Folder progress table model and delegates."""

from __future__ import annotations

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, QSize
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QStyle, QStyledItemDelegate

from models.task_models import FolderSummary


class FolderTableModel(QAbstractTableModel):
    """Table model that tracks folder-level progress rows."""

    HEADERS = ["우선순위", "진행률", "상태", "폴더", "Recipe", "총", "완료", "성공", "실패", "타임아웃"]

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
                return row.queue_priority if row.queue_priority > 0 else "-"
            if column == 1:
                return round(row.progress, 1)
            if column == 2:
                return row.stage_label or row.status.value
            if column == 3:
                return row.source_path or row.folder_path
            if column == 4:
                return ", ".join(row.recipe_aliases)
            if column == 5:
                return row.total
            if column == 6:
                return row.completed
            if column == 7:
                return row.success
            if column == 8:
                return row.fail + row.error
            if column == 9:
                return row.timeout

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
        self._rows = sorted(rows, key=self._priority_sort_key)
        self._index_map = {row.folder_path: idx for idx, row in enumerate(self._rows)}
        self.endResetModel()

    def upsert_summary(self, summary: FolderSummary) -> None:
        """Insert or update one folder row efficiently."""

        row_idx = self._index_map.get(summary.folder_path)
        if row_idx is None:
            insert_at = self._priority_insert_index(summary)
            self.beginInsertRows(QModelIndex(), insert_at, insert_at)
            self._rows.insert(insert_at, summary)
            self.endInsertRows()
            self._rebuild_index_map()
            return

        previous = self._rows[row_idx]
        if previous.queue_priority != summary.queue_priority:
            self.beginResetModel()
            self._rows[row_idx] = summary
            self._rows.sort(key=self._priority_sort_key)
            self._rebuild_index_map()
            self.endResetModel()
            return

        self._rows[row_idx] = summary
        start = self.index(row_idx, 0)
        end = self.index(row_idx, self.columnCount() - 1)
        self.dataChanged.emit(start, end, [Qt.DisplayRole, Qt.UserRole])

    def remove_by_folder_path(self, folder_path: str) -> None:
        """Remove one row by folder path when it exists."""

        row_idx = self._index_map.get(folder_path)
        if row_idx is None:
            return

        self.beginRemoveRows(QModelIndex(), row_idx, row_idx)
        self._rows.pop(row_idx)
        self.endRemoveRows()
        self._rebuild_index_map()

    @staticmethod
    def _priority_sort_key(summary: FolderSummary) -> tuple[bool, int]:
        """Sort persisted queue priorities first while keeping legacy zero rows stable."""

        priority = int(summary.queue_priority)
        return priority <= 0, priority if priority > 0 else 0

    def _priority_insert_index(self, summary: FolderSummary) -> int:
        """Find the ordered insertion point without reloading or sorting all rows."""

        target_key = self._priority_sort_key(summary)
        low = 0
        high = len(self._rows)
        while low < high:
            middle = (low + high) // 2
            if self._priority_sort_key(self._rows[middle]) <= target_key:
                low = middle + 1
            else:
                high = middle
        return low

    def _rebuild_index_map(self) -> None:
        self._index_map = {row.folder_path: idx for idx, row in enumerate(self._rows)}

    def has_folder(self, folder_path: str) -> bool:
        """Return whether this model currently contains folder row."""

        return folder_path in self._index_map

    def folder_at(self, row: int) -> str | None:
        """Return the queue key for the selected row."""

        if 0 <= row < len(self._rows):
            return self._rows[row].folder_path
        return None

    def source_folder_at(self, row: int) -> str | None:
        """Return the physical folder path shown for the selected row."""

        if 0 <= row < len(self._rows):
            summary = self._rows[row]
            return summary.source_path or summary.folder_path
        return None


class ProgressBarDelegate(QStyledItemDelegate):
    """Paint folder progress as custom horizontal bar for stable visual layout."""

    def sizeHint(self, option, index: QModelIndex) -> QSize:  # type: ignore[override]
        """Return stable minimum room so progress bars remain readable."""

        metrics = option.fontMetrics
        text_width = metrics.horizontalAdvance("100%")
        min_width = max(170, text_width + 110)
        min_height = max(30, metrics.height() + 12)
        return QSize(min_width, min_height)

    def paint(self, painter: QPainter, option, index: QModelIndex) -> None:  # type: ignore[override]
        value = index.data(Qt.DisplayRole)
        try:
            progress = max(0.0, min(100.0, float(value)))
        except (TypeError, ValueError):
            progress = 0.0

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)

        container_rect = option.rect.adjusted(10, 8, -10, -8)
        if container_rect.width() <= 0 or container_rect.height() <= 0:
            painter.restore()
            return

        bar_height = min(18, container_rect.height())
        bar_rect = container_rect.adjusted(0, (container_rect.height() - bar_height) // 2, 0, 0)
        bar_rect.setHeight(bar_height)

        track_color = QColor("#1E293B")
        fill_color = QColor("#2563EB")
        if progress >= 99.95:
            fill_color = QColor("#16A34A")
        elif progress >= 50.0:
            fill_color = QColor("#1D4ED8")

        painter.setPen(Qt.NoPen)
        painter.setBrush(track_color)
        painter.drawRoundedRect(bar_rect, 8, 8)

        fill_width = int(round(bar_rect.width() * (progress / 100.0)))
        if fill_width > 0:
            fill_rect = bar_rect.adjusted(0, 0, -(bar_rect.width() - fill_width), 0)
            painter.setBrush(fill_color)
            painter.drawRoundedRect(fill_rect, 8, 8)

        text = f"{progress:.1f}%"
        if option.state & QStyle.State_Selected:
            text_color = QColor("#F8FAFC")
        else:
            text_color = QColor("#E2E8F0")
        painter.setPen(QPen(text_color))
        painter.drawText(bar_rect, Qt.AlignCenter, text)

        painter.restore()
