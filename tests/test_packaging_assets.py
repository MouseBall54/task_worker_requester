"""Regression tests for UI resources included in the Windows bundle."""

from __future__ import annotations

from pathlib import Path
import re
import unittest


class PackagingAssetsTest(unittest.TestCase):
    """Keep source SVG additions from disappearing in installed builds."""

    def test_pyinstaller_spec_bundles_every_svg_ui_icon(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        spec_text = (project_root / "packaging" / "IPDK_plus.spec").read_text(
            encoding="utf-8"
        )

        self.assertIn('UI_ICON_DIR.glob("*.svg")', spec_text)

    def test_build_validation_requires_folder_action_icons(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        script_text = (project_root / "scripts" / "build_windows.ps1").read_text(
            encoding="utf-8"
        )
        required_icons = {
            "folder_move_top.svg",
            "folder_move_up.svg",
            "folder_move_down.svg",
            "folder_move_bottom.svg",
            "folder_hold.svg",
            "folder_release.svg",
        }

        for icon_name in required_icons:
            self.assertIn(icon_name, script_text)

    def test_favorite_and_search_rows_match_folder_tree_density(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        stylesheet = (project_root / "ui" / "styles.qss").read_text(encoding="utf-8")
        list_rule = re.search(
            r"QListWidget#favoriteRootList::item,\s*"
            r"QListWidget#folderSearchResults::item\s*\{(?P<body>[^}]*)\}",
            stylesheet,
        )
        tree_rule = re.search(
            r"QTreeView#folderTree::item\s*\{(?P<body>[^}]*)\}", stylesheet
        )

        self.assertIsNotNone(list_rule)
        self.assertIsNotNone(tree_rule)
        assert list_rule is not None and tree_rule is not None
        for declaration in ("height: 26px", "margin: 1px 2px", "padding: 0 8px"):
            self.assertIn(declaration, list_rule.group("body"))
            self.assertIn(declaration, tree_rule.group("body"))


if __name__ == "__main__":
    unittest.main()
