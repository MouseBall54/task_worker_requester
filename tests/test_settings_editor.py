"""Tests for validated runtime YAML settings updates."""

from __future__ import annotations

from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest

import yaml

from config.config_loader import ConfigLoader
from config.settings_editor import (
    SettingsEditorError,
    update_app_config,
    update_recipe_config,
)


class SettingsEditorTest(unittest.TestCase):
    def _copy_configs(self, temp_dir: str) -> tuple[Path, Path]:
        project_root = Path(__file__).resolve().parents[1]
        app_path = Path(temp_dir) / "app_config.yaml"
        recipe_path = Path(temp_dir) / "recipe_config.yaml"
        shutil.copy2(project_root / "config" / "app_config.yaml", app_path)
        shutil.copy2(project_root / "config" / "recipe_config.yaml", recipe_path)
        return app_path, recipe_path

    def test_app_update_changes_supported_values_and_preserves_other_sections(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_path, _recipe_path = self._copy_configs(temp_dir)
            before = yaml.safe_load(app_path.read_text(encoding="utf-8"))

            update_app_config(
                app_path,
                {"host": "10.0.0.25", "port": 5673, "request_queue": "NEW_QUEUE"},
                {"timeout_seconds": 1234, "max_messages_per_poll": 77},
            )

            after = yaml.safe_load(app_path.read_text(encoding="utf-8"))
            loaded = ConfigLoader.load(app_path)
            self.assertEqual(loaded.rabbitmq.host, "10.0.0.25")
            self.assertEqual(loaded.rabbitmq.port, 5673)
            self.assertEqual(loaded.rabbitmq.request_queue, "NEW_QUEUE")
            self.assertEqual(loaded.publish.timeout_seconds, 1234)
            self.assertEqual(loaded.publish.max_messages_per_poll, 77)
            self.assertEqual(after["rabbitmq"]["request_queue_declare"], before["rabbitmq"]["request_queue_declare"])
            self.assertEqual(after["ui"], before["ui"])
            self.assertEqual(after["update"], before["update"])

    def test_invalid_app_update_does_not_replace_original_file(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_path, _recipe_path = self._copy_configs(temp_dir)
            original = app_path.read_bytes()

            with self.assertRaises(SettingsEditorError):
                update_app_config(
                    app_path,
                    {},
                    {"initial_open_folders": 9, "max_active_open_folders": 2},
                )

            self.assertEqual(app_path.read_bytes(), original)

    def test_recipe_update_validates_and_preserves_unrelated_keys(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_path, recipe_path = self._copy_configs(temp_dir)
            raw = yaml.safe_load(recipe_path.read_text(encoding="utf-8"))
            raw["operator_note"] = "keep-me"
            recipe_path.write_text(
                yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8"
            )

            update_recipe_config(
                app_path,
                recipe_path,
                "Recipe B",
                [("Recipe A", "a.json"), ("Recipe B", "b.json")],
            )

            saved = yaml.safe_load(recipe_path.read_text(encoding="utf-8"))
            loaded = ConfigLoader.load(app_path)
            self.assertEqual(saved["operator_note"], "keep-me")
            self.assertEqual(loaded.recipe_config.default_alias, "Recipe B")
            self.assertEqual(
                [(item.alias, item.path) for item in loaded.recipe_config.recipes],
                [("Recipe A", "a.json"), ("Recipe B", "b.json")],
            )

    def test_invalid_recipe_update_does_not_replace_original_file(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_path, recipe_path = self._copy_configs(temp_dir)
            original = recipe_path.read_bytes()

            with self.assertRaises(SettingsEditorError):
                update_recipe_config(
                    app_path,
                    recipe_path,
                    "Duplicate",
                    [("Duplicate", "a.json"), ("duplicate", "b.json")],
                )

            self.assertEqual(recipe_path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
