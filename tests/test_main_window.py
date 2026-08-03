"""Lightweight UI tests for main window tab behavior."""

from __future__ import annotations

import unittest

from config.models import AppConfig, PublishConfig, RabbitMQConfig, RecipeConfig, RecipeItem, UiConfig
from models.task_models import FolderSummary, TaskStatus

try:
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from ui.main_window import MainWindow

    PYSIDE_AVAILABLE = True
except ImportError:  # pragma: no cover
    Qt = None  # type: ignore[assignment]
    QTest = None  # type: ignore[assignment]
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
            recipe_config=RecipeConfig(
                default_alias="Recipe A",
                recipes=[
                    RecipeItem(alias="Recipe A", path="recipes/a.json"),
                    RecipeItem(alias="Recipe B", path="recipes/b.json"),
                ],
            ),
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

    def test_help_menu_action_opens_searchable_help_dialog(self) -> None:
        window = self._make_window()
        try:
            self.assertTrue(hasattr(window, "action_open_help"))
            self.assertEqual(window.action_open_help.text(), "도움말 열기")
            self.assertEqual(window.action_open_help.shortcut().toString(), "F1")
            self.assertTrue(hasattr(window, "action_check_update"))
            self.assertEqual(window.action_check_update.text(), "업데이트 확인")

            window.show()
            self._app.processEvents()
            QTest.keyClick(window, Qt.Key_F1)
            self._app.processEvents()

            self.assertIsNotNone(window._help_dialog)
            dialog = window._help_dialog
            self.assertTrue(dialog.isVisible())
            self.assertGreaterEqual(dialog.topic_list.count(), 11)

            dialog.search_edit.setText("RabbitMQ")
            self._app.processEvents()
            self.assertRegex(dialog.match_count_label.text(), r"^\d+ / \d+$")

            first_count = dialog.match_count_label.text()
            QTest.keyClick(dialog.search_edit, Qt.Key_Return)
            self._app.processEvents()
            self.assertNotEqual(dialog.match_count_label.text(), first_count)

            QTest.keyClick(dialog.search_edit, Qt.Key_Return, Qt.ShiftModifier)
            self._app.processEvents()
            self.assertEqual(dialog.match_count_label.text(), first_count)

            window.action_open_help.trigger()
            self._app.processEvents()
            self.assertIs(window._help_dialog, dialog)
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
            window.show()
            self._app.processEvents()

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

    def test_horizontal_alignment_reset_preserves_tree_context(self) -> None:
        window = self._make_window()
        try:
            window.folder_tree.setColumnWidth(0, 1200)
            window.show()
            self._app.processEvents()
            scrollbar = window.folder_tree.horizontalScrollBar()
            self.assertGreater(scrollbar.maximum(), scrollbar.minimum())
            scrollbar.setValue(scrollbar.maximum())

            window._reset_horizontal_scrollbars_to_left()

            self.assertEqual(scrollbar.value(), scrollbar.maximum())
        finally:
            window.close()

    def test_deep_tree_index_is_centered_horizontally(self) -> None:
        anchor = MainWindow._calculate_tree_horizontal_anchor(
            depth=8,
            indentation=22,
            label_width=80,
        )
        scroll_value = MainWindow._calculate_centered_tree_scroll_value(
            anchor=anchor,
            viewport_width=280,
            minimum=0,
            maximum=900,
        )

        self.assertEqual(anchor, 266)
        self.assertEqual(scroll_value, 126)
        self.assertEqual(anchor - scroll_value, 140)

    def test_set_runtime_options_enabled_toggles_recipe_and_priority(self) -> None:
        window = self._make_window()
        try:
            window.set_runtime_options_enabled(False)
            self.assertFalse(window.recipe_combo.isEnabled())
            self.assertFalse(window.recipe_multi_check.isEnabled())
            self.assertFalse(window.priority_combo.isEnabled())

            window.set_runtime_options_enabled(True)
            self.assertTrue(window.recipe_combo.isEnabled())
            self.assertTrue(window.recipe_multi_check.isEnabled())
            self.assertTrue(window.priority_combo.isEnabled())
        finally:
            window.close()

    def test_multi_recipe_selector_returns_all_checked_recipes(self) -> None:
        window = self._make_window()
        try:
            self.assertEqual(window.current_recipe_selections(), [("Recipe A", "recipes/a.json")])

            window.recipe_multi_check.setChecked(True)
            window._recipe_actions[1].trigger()

            self.assertEqual(
                window.current_recipe_selections(),
                [("Recipe A", "recipes/a.json"), ("Recipe B", "recipes/b.json")],
            )
            self.assertEqual(window.recipe_multi_button.text(), "2개: Recipe A, Recipe B")
            self.assertIn("recipes/a.json", window.recipe_path_preview.text())
            self.assertIn("recipes/b.json", window.recipe_path_preview.text())

            window.set_runtime_options_enabled(False)
            self.assertFalse(window.recipe_multi_check.isEnabled())
            self.assertFalse(window.recipe_multi_button.isEnabled())
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main()
