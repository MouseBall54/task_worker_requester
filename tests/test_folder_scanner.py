"""Unit tests for folder scanning service."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from services.folder_scanner import FolderScanner


class FolderScannerTest(unittest.TestCase):
    """Verify folder and subfolder image collection behavior."""

    def test_scan_single_folder_returns_images_only(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "a.jpg").write_text("x", encoding="utf-8")
            (root / "b.png").write_text("x", encoding="utf-8")
            (root / "c.txt").write_text("x", encoding="utf-8")

            scanner = FolderScanner([".jpg", ".png"])
            result = scanner.scan_single_folder(str(root))

            self.assertIn(str(root), result)
            self.assertEqual(len(result[str(root)]), 2)

    def test_scan_subfolders_direct_mode(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sub_1 = root / "sub1"
            sub_2 = root / "sub2"
            nested = sub_1 / "nested"
            sub_1.mkdir()
            sub_2.mkdir()
            nested.mkdir()
            (sub_1 / "a.jpg").write_text("x", encoding="utf-8")
            (nested / "b.jpg").write_text("x", encoding="utf-8")

            scanner = FolderScanner([".jpg"])
            result = scanner.scan_subfolders(str(root), mode="direct")

            self.assertIn(str(sub_1), result)
            self.assertNotIn(str(nested), result)
            self.assertNotIn(str(sub_2), result)

    def test_scan_subfolders_recursive_mode(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sub_1 = root / "sub1"
            nested = sub_1 / "nested"
            sub_1.mkdir()
            nested.mkdir()
            (nested / "x.png").write_text("x", encoding="utf-8")

            scanner = FolderScanner([".png"])
            result = scanner.scan_subfolders(str(root), mode="recursive")

            self.assertIn(str(nested), result)
            self.assertEqual(len(result[str(nested)]), 1)


if __name__ == "__main__":
    unittest.main()
