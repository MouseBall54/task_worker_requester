"""Unit tests for configuration loading behavior."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import textwrap
import unittest

from config.config_loader import ConfigLoader


class ConfigLoaderTest(unittest.TestCase):
    """Validate recipe preset parsing and backward compatibility."""

    def test_load_with_recipe_presets(self) -> None:
        content = textwrap.dedent(
            """
            mock_mode: true
            rabbitmq:
              host: "127.0.0.1"
              port: 5672
              username: "guest"
              password: "guest"
            publish:
              default_action: "RUN_RECIPE"
              default_recipe_alias: "Precision"
              recipe_presets:
                - alias: "Default"
                  path: "recipes/default.json"
                - alias: "Precision"
                  path: "recipes/precision.json"
            """
        ).strip()

        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "app.yaml"
            config_path.write_text(content, encoding="utf-8")

            config = ConfigLoader.load(config_path)

        self.assertEqual(config.publish.default_recipe_alias, "Precision")
        self.assertEqual(config.publish.default_recipe_path, "recipes/precision.json")
        self.assertEqual(len(config.publish.recipe_presets), 2)

    def test_load_without_recipe_presets_uses_default_path(self) -> None:
        content = textwrap.dedent(
            """
            rabbitmq:
              host: "127.0.0.1"
              port: 5672
              username: "guest"
              password: "guest"
            publish:
              default_recipe_path: "recipes/legacy.json"
            """
        ).strip()

        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "app.yaml"
            config_path.write_text(content, encoding="utf-8")

            config = ConfigLoader.load(config_path)

        self.assertEqual(config.publish.default_recipe_alias, "Default Recipe")
        self.assertEqual(config.publish.default_recipe_path, "recipes/legacy.json")
        self.assertEqual(len(config.publish.recipe_presets), 1)
        self.assertEqual(config.publish.recipe_presets[0].alias, "Default Recipe")


if __name__ == "__main__":
    unittest.main()
