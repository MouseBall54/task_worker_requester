"""Unit tests for configuration loading behavior."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import textwrap
import unittest

from config.config_loader import ConfigError, ConfigLoader


class ConfigLoaderTest(unittest.TestCase):
    """Validate external recipe config loading and related safeguards."""

    def test_load_with_external_recipe_config_and_queue_declare_settings(self) -> None:
        main_content = textwrap.dedent(
            """
            mock_mode: true
            recipe_config_path: "recipe_config.yaml"
            rabbitmq:
              host: "127.0.0.1"
              port: 5672
              username: "guest"
              password: "guest"
              request_queue: "task.request"
              result_queue_base: "task.result.client"
              request_queue_declare:
                durable: true
                exclusive: false
                auto_delete: false
                arguments:
                  module_group: "IPDK_WORKER"
                  x-max-priority: 9
              result_queue_declare:
                durable: true
                exclusive: false
                auto_delete: false
                arguments:
                  module_group: "default"
            publish:
              default_action: "RUN_RECIPE"
              initial_open_folders: 2
              max_active_open_folders: 4
            """
        ).strip()
        recipe_content = textwrap.dedent(
            """
            default_alias: "Precision"
            recipes:
              - alias: "Default"
                path: "recipes/default.json"
              - alias: "Precision"
                path: "recipes/precision.json"
            """
        ).strip()

        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            config_path = temp_path / "app.yaml"
            recipe_path = temp_path / "recipe_config.yaml"
            config_path.write_text(main_content, encoding="utf-8")
            recipe_path.write_text(recipe_content, encoding="utf-8")

            config = ConfigLoader.load(config_path)

        self.assertEqual(config.recipe_config_path, str(recipe_path.resolve()))
        self.assertEqual(config.recipe_config.default_alias, "Precision")
        self.assertEqual(config.recipe_config.default_path, "recipes/precision.json")
        self.assertEqual(len(config.recipe_config.recipes), 2)
        self.assertEqual(config.rabbitmq.request_queue_declare.arguments["x-max-priority"], 9)
        self.assertEqual(config.rabbitmq.result_queue_declare.arguments["module_group"], "default")
        self.assertEqual(config.publish.initial_open_folders, 2)
        self.assertEqual(config.publish.max_active_open_folders, 4)

    def test_missing_recipe_config_path_raises_helpful_error(self) -> None:
        content = textwrap.dedent(
            """
            rabbitmq:
              host: "127.0.0.1"
              port: 5672
              username: "guest"
              password: "guest"
            publish:
              initial_open_folders: 2
              max_active_open_folders: 3
            """
        ).strip()

        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "app.yaml"
            config_path.write_text(content, encoding="utf-8")

            with self.assertRaises(ConfigError) as ctx:
                ConfigLoader.load(config_path)

        self.assertIn("recipe_config_path", str(ctx.exception))

    def test_inline_recipe_config_in_main_file_raises_error(self) -> None:
        content = textwrap.dedent(
            """
            recipe_config_path: "recipe_config.yaml"
            rabbitmq:
              host: "127.0.0.1"
              port: 5672
              username: "guest"
              password: "guest"
            recipe_config:
              default_alias: "Default"
              recipes:
                - alias: "Default"
                  path: "recipes/default.json"
            """
        ).strip()

        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            config_path = temp_path / "app.yaml"
            recipe_path = temp_path / "recipe_config.yaml"
            config_path.write_text(content, encoding="utf-8")
            recipe_path.write_text("default_alias: Default\nrecipes: []\n", encoding="utf-8")

            with self.assertRaises(ConfigError) as ctx:
                ConfigLoader.load(config_path)

        self.assertIn("inline recipe_config", str(ctx.exception))

    def test_missing_external_recipe_file_raises_helpful_error(self) -> None:
        content = textwrap.dedent(
            """
            recipe_config_path: "missing_recipe_config.yaml"
            rabbitmq:
              host: "127.0.0.1"
              port: 5672
              username: "guest"
              password: "guest"
            """
        ).strip()

        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "app.yaml"
            config_path.write_text(content, encoding="utf-8")

            with self.assertRaises(ConfigError) as ctx:
                ConfigLoader.load(config_path)

        self.assertIn("recipe 설정 파일", str(ctx.exception))

    def test_invalid_external_default_alias_raises_error(self) -> None:
        main_content = textwrap.dedent(
            """
            recipe_config_path: "recipe_config.yaml"
            rabbitmq:
              host: "127.0.0.1"
              port: 5672
              username: "guest"
              password: "guest"
            """
        ).strip()
        recipe_content = textwrap.dedent(
            """
            default_alias: "NotFound"
            recipes:
              - alias: "Default"
                path: "recipes/default.json"
            """
        ).strip()

        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            config_path = temp_path / "app.yaml"
            recipe_path = temp_path / "recipe_config.yaml"
            config_path.write_text(main_content, encoding="utf-8")
            recipe_path.write_text(recipe_content, encoding="utf-8")

            with self.assertRaises(ConfigError) as ctx:
                ConfigLoader.load(config_path)

        self.assertIn("default_alias", str(ctx.exception))

    def test_initial_open_cannot_exceed_max_active(self) -> None:
        main_content = textwrap.dedent(
            """
            recipe_config_path: "recipe_config.yaml"
            rabbitmq:
              host: "127.0.0.1"
              port: 5672
              username: "guest"
              password: "guest"
            publish:
              initial_open_folders: 4
              max_active_open_folders: 3
            """
        ).strip()
        recipe_content = textwrap.dedent(
            """
            default_alias: "Default Recipe"
            recipes:
              - alias: "Default Recipe"
                path: "recipes/default.json"
            """
        ).strip()

        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            config_path = temp_path / "app.yaml"
            recipe_path = temp_path / "recipe_config.yaml"
            config_path.write_text(main_content, encoding="utf-8")
            recipe_path.write_text(recipe_content, encoding="utf-8")

            with self.assertRaises(ConfigError) as ctx:
                ConfigLoader.load(config_path)

        self.assertIn("initial_open_folders", str(ctx.exception))

    def test_default_priority_loads_when_within_queue_max_priority(self) -> None:
        main_content = textwrap.dedent(
            """
            recipe_config_path: "recipe_config.yaml"
            rabbitmq:
              host: "127.0.0.1"
              port: 5672
              username: "guest"
              password: "guest"
              request_queue_declare:
                arguments:
                  x-max-priority: 7
            publish:
              default_priority: 4
            """
        ).strip()
        recipe_content = textwrap.dedent(
            """
            default_alias: "Default Recipe"
            recipes:
              - alias: "Default Recipe"
                path: "recipes/default.json"
            """
        ).strip()

        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            config_path = temp_path / "app.yaml"
            recipe_path = temp_path / "recipe_config.yaml"
            config_path.write_text(main_content, encoding="utf-8")
            recipe_path.write_text(recipe_content, encoding="utf-8")

            config = ConfigLoader.load(config_path)

        self.assertEqual(config.publish.default_priority, 4)
        self.assertEqual(config.rabbitmq.request_queue_max_priority, 7)

    def test_default_priority_requires_request_queue_max_priority_when_non_zero(self) -> None:
        main_content = textwrap.dedent(
            """
            recipe_config_path: "recipe_config.yaml"
            rabbitmq:
              host: "127.0.0.1"
              port: 5672
              username: "guest"
              password: "guest"
              request_queue_declare:
                arguments:
                  module_group: "IPDK_WORKER"
            publish:
              default_priority: 2
            """
        ).strip()
        recipe_content = textwrap.dedent(
            """
            default_alias: "Default Recipe"
            recipes:
              - alias: "Default Recipe"
                path: "recipes/default.json"
            """
        ).strip()

        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            config_path = temp_path / "app.yaml"
            recipe_path = temp_path / "recipe_config.yaml"
            config_path.write_text(main_content, encoding="utf-8")
            recipe_path.write_text(recipe_content, encoding="utf-8")

            with self.assertRaises(ConfigError) as ctx:
                ConfigLoader.load(config_path)

        self.assertIn("default_priority", str(ctx.exception))

    def test_invalid_request_queue_max_priority_type_raises_error(self) -> None:
        main_content = textwrap.dedent(
            """
            recipe_config_path: "recipe_config.yaml"
            rabbitmq:
              host: "127.0.0.1"
              port: 5672
              username: "guest"
              password: "guest"
              request_queue_declare:
                arguments:
                  x-max-priority: "high"
            publish:
              default_priority: 0
            """
        ).strip()
        recipe_content = textwrap.dedent(
            """
            default_alias: "Default Recipe"
            recipes:
              - alias: "Default Recipe"
                path: "recipes/default.json"
            """
        ).strip()

        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            config_path = temp_path / "app.yaml"
            recipe_path = temp_path / "recipe_config.yaml"
            config_path.write_text(main_content, encoding="utf-8")
            recipe_path.write_text(recipe_content, encoding="utf-8")

            with self.assertRaises(ConfigError) as ctx:
                ConfigLoader.load(config_path)

        self.assertIn("x-max-priority", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
