"""Status badge delegate for table cells."""

from __future__ import annotations

from PySide6.QtCore import Qt, QRectF, QSize
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QStyledItemDelegate


class StatusBadgeDelegate(QStyledItemDelegate):
    """Render status values as rounded color badges."""

    OUTER_MARGIN_X = 6
    OUTER_MARGIN_Y = 4
    BADGE_PADDING_X = 12
    BADGE_PADDING_Y = 4
    MIN_BADGE_WIDTH = 84
    MIN_BADGE_HEIGHT = 24

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

    def sizeHint(self, option, index) -> QSize:  # type: ignore[override]
        value = str(index.data(Qt.DisplayRole) or "").upper() or "-"
        metrics = option.fontMetrics
        text_width = metrics.horizontalAdvance(value)
        text_height = metrics.height()

        badge_width = max(self.MIN_BADGE_WIDTH, text_width + self.BADGE_PADDING_X * 2)
        badge_height = max(self.MIN_BADGE_HEIGHT, text_height + self.BADGE_PADDING_Y * 2)

        return QSize(
            badge_width + self.OUTER_MARGIN_X * 2,
            badge_height + self.OUTER_MARGIN_Y * 2,
        )

    def paint(self, painter: QPainter, option, index) -> None:  # type: ignore[override]
        value = str(index.data(Qt.DisplayRole) or "").upper()
        if not value:
            value = "-"
        bg_hex, text_hex = self.STATUS_COLORS.get(value, ("#334155", "#E2E8F0"))
        metrics = option.fontMetrics

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)

        rect = option.rect.adjusted(
            self.OUTER_MARGIN_X,
            self.OUTER_MARGIN_Y,
            -self.OUTER_MARGIN_X,
            -self.OUTER_MARGIN_Y,
        )
        if rect.width() <= 0 or rect.height() <= 0:
            painter.restore()
            return

        requested_width = max(self.MIN_BADGE_WIDTH, metrics.horizontalAdvance(value) + self.BADGE_PADDING_X * 2)
        badge_width = min(requested_width, rect.width())
        badge_rect = QRectF(
            rect.x() + (rect.width() - badge_width) / 2,
            rect.y(),
            badge_width,
            rect.height(),
        )
        available_text_width = max(8, int(badge_rect.width()) - self.BADGE_PADDING_X * 2)
        draw_text = metrics.elidedText(value, Qt.ElideRight, available_text_width)

        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(bg_hex))
        painter.drawRoundedRect(badge_rect, 9, 9)

        painter.setPen(QColor(text_hex))
        painter.drawText(badge_rect.toRect(), Qt.AlignCenter, draw_text)
        painter.restore()
