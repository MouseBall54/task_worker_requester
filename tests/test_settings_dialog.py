"""Qt tests for app and recipe settings dialogs."""

from __future__ import annotations

from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication, QLabel, QLineEdit

from config.config_loader import ConfigLoader
from ui.settings_dialog import AppConfigSettingsDialog, RecipeConfigSettingsDialog


class SettingsDialogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])

    def _copy_configs(self, temp_dir: str) -> tuple[Path, Path]:
        project_root = Path(__file__).resolve().parents[1]
        app_path = Path(temp_dir) / "app_config.yaml"
        recipe_path = Path(temp_dir) / "recipe_config.yaml"
        shutil.copy2(project_root / "config" / "app_config.yaml", app_path)
        shutil.copy2(project_root / "config" / "recipe_config.yaml", recipe_path)
        return app_path, recipe_path

    def test_app_dialog_shows_current_values_and_saves_runtime_file(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_path, _recipe_path = self._copy_configs(temp_dir)
            dialog = AppConfigSettingsDialog(app_path)
            try:
                self.assertEqual(dialog.windowTitle(), "MQ 연결 설정")
                self.assertFalse(
                    any(
                        "프로그램을 다시 시작" in label.text()
                        for label in dialog.findChildren(QLabel)
                    )
                )
                self.assertEqual(dialog.host_edit.text(), "127.0.0.1")
                self.assertEqual(dialog.request_queue_edit.text(), "IPDK_WORKER_INTERFACE")
                self.assertEqual(dialog.timeout_spin.value(), 86400)
                self.assertEqual(dialog.password_edit.echoMode(), QLineEdit.Password)
                dialog.host_edit.setText("192.168.0.20")
                dialog.timeout_spin.setValue(4321)
                with patch("ui.settings_dialog.QMessageBox.information"):
                    dialog._save()

                loaded = ConfigLoader.load(app_path)
                self.assertEqual(loaded.rabbitmq.host, "192.168.0.20")
                self.assertEqual(loaded.publish.timeout_seconds, 4321)
            finally:
                dialog.close()

    def test_recipe_dialog_shows_rows_and_saves_edited_recipe_list(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_path, recipe_path = self._copy_configs(temp_dir)
            dialog = RecipeConfigSettingsDialog(app_path, recipe_path)
            try:
                self.assertEqual(dialog.windowTitle(), "Recipe 설정")
                self.assertFalse(
                    any(
                        "프로그램을 다시 시작" in label.text()
                        for label in dialog.findChildren(QLabel)
                    )
                )
                self.assertEqual(dialog.default_alias_edit.text(), "Default Recipe")
                self.assertEqual(dialog.recipe_table.rowCount(), 3)
                dialog.default_alias_edit.setText("Custom")
                dialog._append_recipe("Custom", "recipes/custom.json")
                with patch("ui.settings_dialog.QMessageBox.information"):
                    dialog._save()

                loaded = ConfigLoader.load(app_path)
                self.assertEqual(loaded.recipe_config.default_alias, "Custom")
                self.assertEqual(loaded.recipe_config.recipes[-1].path, "recipes/custom.json")
            finally:
                dialog.close()

    def test_settings_theme_and_recipe_cell_editor_remain_readable(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_path, recipe_path = self._copy_configs(temp_dir)
            project_root = Path(__file__).resolve().parents[1]
            previous_stylesheet = self._app.styleSheet()
            self._app.setStyleSheet(
                (project_root / "ui" / "styles.qss").read_text(encoding="utf-8")
            )
            app_dialog = AppConfigSettingsDialog(app_path)
            recipe_dialog = RecipeConfigSettingsDialog(app_path, recipe_path)
            try:
                app_dialog.show()
                recipe_dialog.show()
                recipe_dialog.recipe_table.editItem(
                    recipe_dialog.recipe_table.item(1, 1)
                )
                self._app.processEvents()

                tab_page = app_dialog.settings_tabs.widget(0)
                self.assertEqual(
                    tab_page.palette().color(QPalette.Window).name(), "#111b2d"
                )
                self.assertEqual(
                    app_dialog.palette().color(QPalette.Window).name(), "#111827"
                )
                cell_editors = recipe_dialog.recipe_table.findChildren(QLineEdit)
                self.assertEqual(len(cell_editors), 1)
                self.assertGreaterEqual(cell_editors[0].height(), 30)
                self.assertGreaterEqual(cell_editors[0].fontMetrics().height(), 13)
            finally:
                app_dialog.close()
                recipe_dialog.close()
                self._app.setStyleSheet(previous_stylesheet)


if __name__ == "__main__":
    unittest.main()
