"""Stable image-path ordering helpers shared by task stores."""

from __future__ import annotations

import ntpath


def image_path_sort_key(image_path: str) -> tuple[object, ...]:
    """Sort numeric filename stems by value, then other names case-insensitively."""

    normalized_path = str(image_path or "")
    filename = ntpath.basename(normalized_path)
    stem, extension = ntpath.splitext(filename)
    folded_path = normalized_path.casefold()

    if stem.isascii() and stem.isdecimal():
        canonical_number = stem.lstrip("0") or "0"
        return (
            0,
            len(canonical_number),
            canonical_number,
            len(stem),
            filename.casefold(),
            folded_path,
        )

    return (1, stem.casefold(), extension.casefold(), folded_path)


def compare_image_paths(left: str, right: str) -> int:
    """Return a SQLite-compatible comparison result for two image paths."""

    left_key = image_path_sort_key(left)
    right_key = image_path_sort_key(right)
    return (left_key > right_key) - (left_key < right_key)
