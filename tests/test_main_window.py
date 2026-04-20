"""Lightweight UI tests for main window tab behavior."""

from __future__ import annotations

import unittest

from config.models import AppConfig, PublishConfig, RabbitMQConfig, UiConfig
from models.task_models import FolderSummary, TaskStatus

try:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    from ui.main_window import MainWindow

    PYSIDE_AVAILABLE = True
except ImportError:  # pragma: no cover
    Qt = None  # type: ignore[assignment]
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
            self.assertFalse(hasattr(window, "action_edit"))
            self.assertFalse(hasattr(window, "polling_combo"))
            self.assertFalse(window.folder_tree.rootIndex().isValid())
            self.assertTrue(window.folder_tree.isHeaderHidden())
            self.assertTrue(hasattr(window, "main_splitter"))
            self.assertEqual(window.folder_tree.horizontalScrollBarPolicy(), Qt.ScrollBarAsNeeded)
            self.assertEqual(window.folder_tree.textElideMode(), Qt.ElideNone)
            self.assertIn("\n", window.connection_label.text())
            self.assertIn("127.0.0.1:5672", window.connection_label.text())
            self.assertIn("request_queue: task.request", window.connection_label.text())
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

    def test_status_sidebar_toggle_button_collapses_and_expands_panel(self) -> None:
        window = self._make_window()
        try:
            self.assertTrue(window.status_sidebar_panel.isVisible())
            self.assertTrue(window.status_tabs.isVisible())
            self.assertEqual(window.btn_toggle_sidebar.text(), "")
            self.assertTrue(window.btn_toggle_sidebar.autoRaise())
            self.assertFalse(window.status_sidebar_panel.isAncestorOf(window.btn_toggle_sidebar))
            self.assertTrue(window.center_panel.isAncestorOf(window.btn_toggle_sidebar))

            window.btn_toggle_sidebar.setChecked(True)
            self._app.processEvents()
            self.assertFalse(window.status_sidebar_panel.isVisible())
            self.assertFalse(window.status_tabs.isVisible())

            window.btn_toggle_sidebar.setChecked(False)
            self._app.processEvents()
            self.assertTrue(window.status_sidebar_panel.isVisible())
            self.assertTrue(window.status_tabs.isVisible())
        finally:
            window.close()

    def test_copy_folder_paths_to_clipboard(self) -> None:
        window = self._make_window()
        try:
            window._copy_folder_paths_to_clipboard(["folder_a", "folder_b"])
            clipboard_text = self._app.clipboard().text()
            self.assertEqual(clipboard_text, "folder_a\nfolder_b")
        finally:
            window.close()

    def test_initial_horizontal_scrollbars_are_aligned_to_left(self) -> None:
        window = self._make_window()
        try:
            window.show()
            self._app.processEvents()
            self._app.processEvents()

            widgets = [
                window.folder_tree,
                window.active_folder_table,
                window.completed_folder_table,
                window.image_table,
                window.log_text,
            ]
            for widget in widgets:
                scrollbar = widget.horizontalScrollBar()
                self.assertEqual(scrollbar.value(), scrollbar.minimum())
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main()
