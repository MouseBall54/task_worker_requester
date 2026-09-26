"""Folder tree jump stays on target while the filesystem model keeps loading."""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from config.models import AppConfig, RabbitMQConfig, UpdateConfig

try:
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtWidgets import QApplication

    from ui.main_window import MainWindow

    PYSIDE_AVAILABLE = True
except ImportError:  # pragma: no cover
    PYSIDE_AVAILABLE = False


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 is required for folder tree tests.")
class FolderTreeJumpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        root = Path(self._temp.name)
        parent = root / "share" / "lot"
        parent.mkdir(parents=True)
        # Enough siblings that late, sorted loading pushes the target out of the first screen.
        for index in range(1500):
            (parent / f"folder_{index:05d}").mkdir()
        self.target = str(parent / "folder_00900")
        self.window = MainWindow(
            AppConfig(
                rabbitmq=RabbitMQConfig(host="127.0.0.1", port=5672, username="guest", password="guest"),
                update=UpdateConfig(enabled=False),
                mock_mode=True,
            ),
            folder_index_database_path=root / "index.sqlite3",
            ui_settings_path=root / "ui_state.ini",
        )
        self.window.resize(1400, 900)
        self.window.show()
        self._wait_for(lambda: self.window._pending_jump_target is None)

    def tearDown(self) -> None:
        self.window.close()
        self._temp.cleanup()

    def _wait_for(self, condition, seconds: float = 5.0) -> None:  # noqa: ANN001
        deadline = time.monotonic() + seconds
        while not condition() and time.monotonic() < deadline:
            self._app.processEvents()
            time.sleep(0.01)

    def _process_events(self, seconds: float) -> None:
        self._wait_for(lambda: False, seconds)

    def _target_state(self) -> tuple[bool, bool]:
        tree = self.window.folder_tree
        model = self.window.file_system_model
        current = tree.currentIndex()
        selected = self.window._paths_match(model.filePath(current), self.target)
        visible = current.isValid() and tree.viewport().rect().intersects(tree.visualRect(current))
        return selected, visible

    def test_jump_keeps_target_selected_and_visible_after_siblings_load(self) -> None:
        self.window.jump_to_path(self.target, show_feedback=False)
        self._process_events(2.0)

        self.assertEqual(self._target_state(), (True, True))

    def test_user_wheel_scroll_stops_refocusing(self) -> None:
        self.window.jump_to_path(self.target, show_feedback=False)
        self._wait_for(lambda: self.window._settle_jump_target is not None)
        self.assertIsNotNone(self.window._settle_jump_target)

        viewport = self.window.folder_tree.viewport()
        center = QPointF(viewport.rect().center())
        wheel = QWheelEvent(
            center,
            QPointF(viewport.mapToGlobal(center.toPoint())),
            QPoint(),
            QPoint(0, -120),
            Qt.NoButton,
            Qt.NoModifier,
            Qt.NoScrollPhase,
            False,
        )
        QApplication.sendEvent(viewport, wheel)

        self.assertIsNone(self.window._settle_jump_target)


if __name__ == "__main__":
    unittest.main()
