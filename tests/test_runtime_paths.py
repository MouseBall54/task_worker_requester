"""Tests for runtime config/resource path resolution used by packaged builds."""

from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from app.runtime_paths import (
    ensure_user_config_seeded,
    migrate_legacy_appdata_dir,
    resolve_default_config_path,
    resolve_stylesheet_path,
)


class RuntimePathsTest(unittest.TestCase):
    """Validate AppData seeding and runtime resource lookup behavior."""

    def test_resolve_default_config_path_prefers_explicit_cli_path(self) -> None:
        with TemporaryDirectory() as temp_dir:
            explicit = Path(temp_dir) / "custom.yaml"
            explicit.write_text("mock_mode: true\n", encoding="utf-8")

            resolved = resolve_default_config_path(explicit)

        self.assertEqual(resolved, explicit.resolve())

    def test_ensure_user_config_seeded_copies_default_templates_into_appdata(self) -> None:
        with TemporaryDirectory() as runtime_dir, TemporaryDirectory() as appdata_root:
            base = Path(runtime_dir)
            (base / "config").mkdir(parents=True, exist_ok=True)
            (base / "config" / "app_config.yaml").write_text(
                'recipe_config_path: "recipe_config.yaml"\n'
                "rabbitmq:\n"
                '  host: "127.0.0.1"\n'
                "  port: 5672\n"
                '  username: "guest"\n'
                '  password: "guest"\n',
                encoding="utf-8",
            )
            (base / "config" / "recipe_config.yaml").write_text(
                "default_alias: Default Recipe\nrecipes:\n"
                "  - alias: Default Recipe\n"
                '    path: "recipes/default.json"\n',
                encoding="utf-8",
            )

            with (
                patch.dict(os.environ, {"APPDATA": appdata_root}, clear=False),
                patch("app.runtime_paths.resolve_runtime_base_dir", return_value=base),
                patch("app.runtime_paths.resolve_install_dir", return_value=base),
                patch("app.runtime_paths._development_root", return_value=base),
            ):
                resolved = ensure_user_config_seeded()

            self.assertTrue(resolved.user_config_path.exists())
            self.assertTrue(resolved.user_recipe_config_path.exists())
            self.assertIn("recipe_config_path", resolved.user_config_path.read_text(encoding="utf-8"))

    def test_resolve_default_config_path_prefers_seeded_appdata_over_install_dir(self) -> None:
        with TemporaryDirectory() as runtime_dir, TemporaryDirectory() as install_dir, TemporaryDirectory() as appdata_root:
            runtime_base = Path(runtime_dir)
            install_base = Path(install_dir)
            (runtime_base / "config").mkdir(parents=True, exist_ok=True)
            (runtime_base / "config" / "app_config.yaml").write_text(
                'recipe_config_path: "recipe_config.yaml"\n'
                "rabbitmq:\n"
                '  host: "127.0.0.1"\n'
                "  port: 5672\n"
                '  username: "guest"\n'
                '  password: "guest"\n',
                encoding="utf-8",
            )
            (runtime_base / "config" / "recipe_config.yaml").write_text(
                "default_alias: Default Recipe\nrecipes:\n"
                "  - alias: Default Recipe\n"
                '    path: "recipes/default.json"\n',
                encoding="utf-8",
            )
            (install_base / "config").mkdir(parents=True, exist_ok=True)
            (install_base / "config" / "app_config.yaml").write_text("install: true\n", encoding="utf-8")

            with (
                patch.dict(os.environ, {"APPDATA": appdata_root}, clear=False),
                patch("app.runtime_paths.resolve_runtime_base_dir", return_value=runtime_base),
                patch("app.runtime_paths.resolve_install_dir", return_value=install_base),
                patch("app.runtime_paths._development_root", return_value=runtime_base),
            ):
                resolved = resolve_default_config_path()

            self.assertEqual(resolved, Path(appdata_root) / "IPDK_plus" / "app_config.yaml")
            self.assertIn("recipe_config_path", resolved.read_text(encoding="utf-8"))

    def test_migrate_legacy_appdata_dir_copies_old_folder_when_new_one_is_empty(self) -> None:
        with TemporaryDirectory() as appdata_root:
            legacy_dir = Path(appdata_root) / "TaskWorkerRequester"
            legacy_dir.mkdir(parents=True, exist_ok=True)
            (legacy_dir / "app_config.yaml").write_text("ui:\n  app_name: IPDK+\n", encoding="utf-8")
            (legacy_dir / "recipe_config.yaml").write_text(
                "default_alias: Default Recipe\nrecipes:\n"
                "  - alias: Default Recipe\n"
                '    path: "recipes/default.json"\n',
                encoding="utf-8",
            )

            with patch.dict(os.environ, {"APPDATA": appdata_root}, clear=False):
                migrated_dir = migrate_legacy_appdata_dir()

            self.assertEqual(migrated_dir, Path(appdata_root) / "IPDK_plus")
            self.assertTrue((Path(appdata_root) / "IPDK_plus" / "app_config.yaml").exists())
            self.assertTrue((Path(appdata_root) / "IPDK_plus" / "recipe_config.yaml").exists())

    def test_migrate_legacy_appdata_dir_does_not_overwrite_existing_ipdk_plus_dir(self) -> None:
        with TemporaryDirectory() as appdata_root:
            legacy_dir = Path(appdata_root) / "TaskWorkerRequester"
            legacy_dir.mkdir(parents=True, exist_ok=True)
            (legacy_dir / "app_config.yaml").write_text("legacy: true\n", encoding="utf-8")

            new_dir = Path(appdata_root) / "IPDK_plus"
            new_dir.mkdir(parents=True, exist_ok=True)
            (new_dir / "app_config.yaml").write_text("current: true\n", encoding="utf-8")

            with patch.dict(os.environ, {"APPDATA": appdata_root}, clear=False):
                migrated_dir = migrate_legacy_appdata_dir()

            self.assertIsNone(migrated_dir)
            self.assertEqual(
                (new_dir / "app_config.yaml").read_text(encoding="utf-8"),
                "current: true\n",
            )

    def test_resolve_stylesheet_path_finds_bundled_qss(self) -> None:
        with TemporaryDirectory() as runtime_dir:
            base = Path(runtime_dir)
            (base / "ui").mkdir(parents=True, exist_ok=True)
            stylesheet = base / "ui" / "styles.qss"
            stylesheet.write_text("QWidget { color: white; }\n", encoding="utf-8")

            with (
                patch("app.runtime_paths.resolve_runtime_base_dir", return_value=base),
                patch("app.runtime_paths.resolve_install_dir", return_value=base),
                patch("app.runtime_paths._development_root", return_value=base),
            ):
                resolved = resolve_stylesheet_path()

            self.assertIsNotNone(resolved)
            assert resolved is not None
            self.assertEqual(resolved.resolve(), stylesheet.resolve())


if __name__ == "__main__":
    unittest.main()
