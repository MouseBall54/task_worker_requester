"""Status badge delegate for table cells."""

from __future__ import annotations

from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QStyledItemDelegate


class StatusBadgeDelegate(QStyledItemDelegate):
    """Render status values as rounded color badges."""

    STATUS_COLORS = {
        "PENDING": ("#4B5563", "#D1D5DB"),
        "SENT": ("#1E3A8A", "#BFDBFE"),
        "RUNNING": ("#1D4ED8", "#DBEAFE"),
        "SUCCESS": ("#166534", "#DCFCE7"),
        "FAIL": ("#991B1B", "#FEE2E2"),
        "TIMEOUT": ("#9A3412", "#FFEDD5"),
        "ERROR": ("#7F1D1D", "#FECACA"),
        "CANCELLED": ("#374151", "#E5E7EB"),
    }

    def paint(self, painter: QPainter, option, index) -> None:  # type: ignore[override]
        value = str(index.data(Qt.DisplayRole) or "").upper()
        bg_hex, text_hex = self.STATUS_COLORS.get(value, ("#334155", "#E2E8F0"))

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)

        rect = option.rect.adjusted(8, 6, -8, -6)
        badge_rect = QRectF(rect)

        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(bg_hex))
        painter.drawRoundedRect(badge_rect, 9, 9)

        painter.setPen(QColor(text_hex))
        painter.drawText(rect, Qt.AlignCenter, value)
        painter.restore()
