"""Delegate that renders and handles MQ preview buttons in table cells."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QModelIndex, Qt, Signal
from PySide6.QtGui import QMouseEvent, QPainter
from PySide6.QtWidgets import QApplication, QStyle, QStyleOptionButton, QStyledItemDelegate


class MQButtonDelegate(QStyledItemDelegate):
    """Button-like delegate that emits request id when clicked."""

    clicked = Signal(str)

    def paint(self, painter: QPainter, option, index: QModelIndex) -> None:  # type: ignore[override]
        button_option = QStyleOptionButton()
        button_option.rect = option.rect.adjusted(6, 5, -6, -5)
        button_option.text = str(index.data(Qt.DisplayRole) or "MQ")
        button_option.state = QStyle.State_Enabled

        if option.state & QStyle.State_MouseOver:
            button_option.state |= QStyle.State_MouseOver
        if option.state & QStyle.State_Selected:
            button_option.state |= QStyle.State_Raised

        QApplication.style().drawControl(QStyle.CE_PushButton, button_option, painter)

    def editorEvent(self, event, model, option, index: QModelIndex) -> bool:  # type: ignore[override]
        if index.column() != 0:
            return False

        if event.type() != QEvent.MouseButtonRelease:
            return False

        if not isinstance(event, QMouseEvent):
            return False

        if not option.rect.contains(event.position().toPoint()):
            return False

        request_id = str(index.data(Qt.UserRole) or "").strip()
        if not request_id:
            return False

        self.clicked.emit(request_id)
        return True
