"""Lightweight UI tests for main window tab behavior."""

from __future__ import annotations

import unittest

from config.models import AppConfig, PublishConfig, RabbitMQConfig, UiConfig
from models.task_models import FolderSummary, TaskStatus

try:
    from PySide6.QtWidgets import QApplication
    from ui.main_window import MainWindow

    PYSIDE_AVAILABLE = True
except ImportError:  # pragma: no cover
    QApplication = None  # type: ignore[assignment]
    MainWindow = None  # type: ignore[assignment]
    PYSIDE_AVAILABLE = False


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 is required for main window tests.")
class MainWindowTest(unittest.TestCase):
    """Verify default status tab and folder-selection tab switching."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])

    def _make_window(self) -> MainWindow:
        config = AppConfig(
            rabbitmq=RabbitMQConfig(host="127.0.0.1", port=5672, username="guest", password="guest"),
            publish=PublishConfig(image_extensions=[".jpg"]),
            ui=UiConfig(),
            mock_mode=True,
        )
        return MainWindow(config)

    def test_status_sidebar_defaults_to_log_tab(self) -> None:
        window = self._make_window()
        try:
            self.assertEqual(window.status_tabs.currentIndex(), window.STATUS_TAB_LOG)
        finally:
            window.close()

    def test_window_uses_ipdk_branding_and_removes_drive_combo(self) -> None:
        window = self._make_window()
        try:
            self.assertEqual(window.windowTitle(), "IPDK_plus")
            self.assertFalse(hasattr(window, "brand_icon_label"))
            self.assertFalse(hasattr(window, "drive_combo"))
            self.assertFalse(window.folder_tree.rootIndex().isValid())
        finally:
            window.close()

    def test_active_folder_single_selection_switches_to_detail_tab(self) -> None:
        window = self._make_window()
        selected_paths: list[str] = []
        window.folder_row_selected.connect(selected_paths.append)
        try:
            window.set_folder_rows(
                [
                    FolderSummary(
                        folder_path="folder_a",
                        total=10,
                        completed=2,
                        success=2,
                        fail=0,
                        timeout=0,
                        error=0,
                        progress=20.0,
                        status=TaskStatus.RUNNING,
                    )
                ]
            )
            window.status_tabs.setCurrentIndex(window.STATUS_TAB_LOG)

            window.active_folder_table.selectRow(0)
            self._app.processEvents()

            self.assertEqual(window.status_tabs.currentIndex(), window.STATUS_TAB_DETAIL)
            self.assertEqual(selected_paths, ["folder_a"])
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main()
