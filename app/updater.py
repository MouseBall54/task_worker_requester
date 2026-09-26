"""Find newer installers published to the shared NAS folder."""

from __future__ import annotations

import re
from pathlib import Path

_INSTALLER_PATTERN = re.compile(r"IPDK_plusSetup_(\d+(?:\.\d+)*)\.exe", re.IGNORECASE)


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def find_newer_installer(share_dir: str | Path, current_version: str) -> tuple[str, Path] | None:
    """Return ``(version, path)`` of the newest installer if it beats ``current_version``.

    Raises ``OSError`` when the share folder cannot be read.
    """

    newest: tuple[str, Path] | None = None
    for path in Path(share_dir).iterdir():
        match = _INSTALLER_PATTERN.fullmatch(path.name)
        if match and (newest is None or _version_key(match[1]) > _version_key(newest[0])):
            newest = (match[1], path)
    if newest and _version_key(newest[0]) > _version_key(current_version):
        return newest
    return None
