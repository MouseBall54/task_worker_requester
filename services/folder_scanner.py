"""Filesystem scan utilities for folder/image registration."""

from __future__ import annotations

import os
from typing import Iterable


class FolderScanner:
    """Collect image files grouped by containing folder."""

    def __init__(self, image_extensions: Iterable[str]) -> None:
        normalized = {ext.lower() if ext.startswith(".") else f".{ext.lower()}" for ext in image_extensions}
        self._extensions = normalized

    def scan_single_folder(self, folder_path: str) -> dict[str, list[str]]:
        """Collect images directly under selected folder only."""

        if not os.path.isdir(folder_path):
            return {}

        normalized_folder = os.path.normpath(folder_path)
        images = self._collect_images_in_folder(normalized_folder)
        if not images:
            return {}
        return {normalized_folder: images}

    def scan_subfolders(self, parent_folder: str, mode: str = "direct") -> dict[str, list[str]]:
        """Collect images under direct or recursive child folder scope.

        Parameters
        ----------
        parent_folder:
            Base folder selected by user.
        mode:
            "direct" for immediate children only, "recursive" for full depth.
        """

        if not os.path.isdir(parent_folder):
            return {}

        normalized_parent = os.path.normpath(parent_folder)
        folder_map: dict[str, list[str]] = {}

        child_dirs: list[str] = []
        try:
            with os.scandir(normalized_parent) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        child_dirs.append(os.path.normpath(entry.path))
        except OSError:
            return {}

        child_dirs.sort(key=lambda path: os.path.basename(path).lower())

        if mode == "recursive":
            for child_dir in child_dirs:
                self._scan_recursive(child_dir, folder_map)
            return folder_map

        for child_dir in child_dirs:
            images = self._collect_images_in_folder(child_dir)
            if images:
                folder_map[child_dir] = images

        return folder_map

    def _scan_recursive(self, folder: str, folder_map: dict[str, list[str]]) -> None:
        """Recursively collect all image-containing directories."""

        for root, dirs, files in os.walk(folder, topdown=True):
            dirs.sort(key=lambda item: item.lower())

            images: list[str] = []
            for file_name in sorted(files, key=lambda item: item.lower()):
                _, extension = os.path.splitext(file_name)
                if extension.lower() not in self._extensions:
                    continue
                images.append(os.path.normpath(os.path.join(root, file_name)))

            if images:
                folder_map[os.path.normpath(root)] = images

    def _collect_images_in_folder(self, folder: str) -> list[str]:
        """Return image paths directly inside folder (no recursion)."""

        images: list[str] = []
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    _, extension = os.path.splitext(entry.name)
                    if extension.lower() not in self._extensions:
                        continue
                    images.append(os.path.normpath(entry.path))
        except OSError:
            return []

        images.sort(key=lambda path: os.path.basename(path).lower())
        return images
