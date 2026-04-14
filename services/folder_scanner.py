"""Filesystem scan utilities for folder/image registration."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable


class FolderScanner:
    """Collect image files grouped by containing folder."""

    def __init__(self, image_extensions: Iterable[str]) -> None:
        normalized = {ext.lower() if ext.startswith(".") else f".{ext.lower()}" for ext in image_extensions}
        self._extensions = normalized

    def scan_single_folder(self, folder_path: str) -> dict[str, list[str]]:
        """Collect images directly under selected folder only."""

        path = Path(folder_path)
        if not path.exists() or not path.is_dir():
            return {}

        images = self._collect_images_in_folder(path)
        if not images:
            return {}
        return {str(path): images}

    def scan_subfolders(self, parent_folder: str, mode: str = "direct") -> dict[str, list[str]]:
        """Collect images under direct or recursive child folder scope.

        Parameters
        ----------
        parent_folder:
            Base folder selected by user.
        mode:
            "direct" for immediate children only, "recursive" for full depth.
        """

        parent = Path(parent_folder)
        if not parent.exists() or not parent.is_dir():
            return {}

        folder_map: dict[str, list[str]] = {}
        for child in sorted(parent.iterdir(), key=lambda p: p.name.lower()):
            if not child.is_dir():
                continue
            if mode == "recursive":
                self._scan_recursive(child, folder_map)
                continue

            images = self._collect_images_in_folder(child)
            if images:
                folder_map[str(child)] = images

        return folder_map

    def _scan_recursive(self, folder: Path, folder_map: dict[str, list[str]]) -> None:
        """Recursively collect all image-containing directories."""

        images = self._collect_images_in_folder(folder)
        if images:
            folder_map[str(folder)] = images

        for child in sorted(folder.iterdir(), key=lambda p: p.name.lower()):
            if child.is_dir():
                self._scan_recursive(child, folder_map)

    def _collect_images_in_folder(self, folder: Path) -> list[str]:
        """Return image paths directly inside folder (no recursion)."""

        images = [
            str(file_path)
            for file_path in sorted(folder.iterdir(), key=lambda p: p.name.lower())
            if file_path.is_file() and file_path.suffix.lower() in self._extensions
        ]
        return images
