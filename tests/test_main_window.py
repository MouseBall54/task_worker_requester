"""Lightweight UI tests for main window tab behavior."""

from __future__ import annotations

import unittest

from config.models import AppConfig, PublishConfig, RabbitMQConfig, RecipeConfig, RecipeItem, UiConfig
from models.task_models import FolderSummary, TaskStatus

try:
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QComboBox, QLabel, QToolButton
    from ui.main_window import MainWindow, RecipePathRow, ResponsiveRecipeSettings

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

    def test_log_document_is_bounded_by_configuration(self) -> None:
        window = self._make_window()
        try:
            self.assertEqual(
                window.log_text.document().maximumBlockCount(),
                window._config.publish.ui_log_max_lines,
            )
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
            self.assertFalse(window.recipe_multi_button.isEnabled())
            self.assertFalse(window.priority_combo.isEnabled())

            window.set_runtime_options_enabled(True)
            self.assertTrue(window.recipe_multi_button.isEnabled())
            self.assertTrue(window.priority_combo.isEnabled())
        finally:
            window.close()

    def test_multi_recipe_selector_returns_all_checked_recipes(self) -> None:
        window = self._make_window()
        try:
            self.assertEqual(window.current_recipe_selections(), [("Recipe A", "recipes/a.json")])
            self.assertFalse(hasattr(window, "recipe_combo"))
            self.assertFalse(hasattr(window, "recipe_multi_check"))

            window._recipe_actions[1].trigger()

            self.assertEqual(
                window.current_recipe_selections(),
                [("Recipe A", "recipes/a.json"), ("Recipe B", "recipes/b.json")],
            )
            self.assertEqual(window.recipe_multi_button.text(), "2개 선택")
            self.assertEqual(
                [(row.alias, row.path) for row in window.recipe_path_rows],
                [("Recipe A", "recipes/a.json"), ("Recipe B", "recipes/b.json")],
            )

            window.set_runtime_options_enabled(False)
            self.assertFalse(window.recipe_multi_button.isEnabled())
        finally:
            window.close()

    def test_recipe_selector_supports_empty_and_select_all_states(self) -> None:
        window = self._make_window()
        try:
            window._clear_all_recipes()
            self.assertEqual(window.current_recipe_selections(), [])
            self.assertEqual(window.recipe_multi_button.text(), "Recipe 선택")
            self.assertTrue(window.recipe_path_empty_label.isVisibleTo(window.recipe_paths_panel))
            self.assertEqual(window.recipe_path_rows, [])

            window._select_all_recipes()
            self.assertEqual(len(window.current_recipe_selections()), 2)
            self.assertEqual(window.recipe_multi_button.text(), "2개 선택")
            self.assertFalse(window.recipe_path_empty_label.isVisible())
        finally:
            window.close()

    def test_recipe_selection_survives_refresh_and_drops_removed_recipe(self) -> None:
        window = self._make_window()
        try:
            window._select_all_recipes()
            window._populate_recipe_selector()
            self.assertEqual(len(window.current_recipe_selections()), 2)

            window._config.recipe_config.recipes = [
                RecipeItem(alias="Recipe B", path="recipes/b.json")
            ]
            window._populate_recipe_selector()
            self.assertEqual(
                window.current_recipe_selections(),
                [("Recipe B", "recipes/b.json")],
            )
        finally:
            window.close()

    def test_recipe_selector_ignores_duplicate_paths(self) -> None:
        window = self._make_window()
        try:
            window._config.recipe_config.recipes.append(
                RecipeItem(alias="Recipe A Duplicate", path="recipes/a.json")
            )
            window._populate_recipe_selector()
            window._select_all_recipes()

            self.assertEqual(len(window._recipe_actions), 2)
            self.assertEqual(
                window.current_recipe_selections(),
                [("Recipe A", "recipes/a.json"), ("Recipe B", "recipes/b.json")],
            )
        finally:
            window.close()

    def test_recipe_and_priority_layout_reflows_without_overlap(self) -> None:
        settings = ResponsiveRecipeSettings(
            QLabel("Recipe"),
            QToolButton(),
            QLabel("Priority"),
            QComboBox(),
        )
        try:
            settings.show()
            for width in (560, 700, 900):
                settings.resize(width, 100)
                self._app.processEvents()
                self.assertFalse(settings.is_compact)
                self.assertFalse(
                    settings.recipe_selector.geometry().intersects(
                        settings.priority_selector.geometry()
                    )
                )

            settings.resize(480, 100)
            self._app.processEvents()
            self.assertTrue(settings.is_compact)
            self.assertGreater(
                settings.priority_selector.geometry().top(),
                settings.recipe_selector.geometry().top(),
            )
        finally:
            settings.close()

    def test_recipe_path_row_reflows_and_elides_long_path(self) -> None:
        long_path = "D:/" + "/very-long-folder" * 20 + "/recipe.json"
        row = RecipePathRow("Very Long Recipe Alias", long_path)
        try:
            row.show()
            row.resize(600, 50)
            self._app.processEvents()
            self.assertFalse(row.is_compact)

            row.resize(380, 80)
            self._app.processEvents()
            self._app.processEvents()
            self.assertTrue(row.is_compact)
            self.assertGreater(row.path_label.geometry().top(), row.alias_label.geometry().top())
            self.assertEqual(row.path_label.toolTip(), long_path)
            self.assertIn("…", row.path_label.text())
        finally:
            row.close()


if __name__ == "__main__":
    unittest.main()
