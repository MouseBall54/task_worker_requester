"""Application package exports."""

from __future__ import annotations

from typing import Any


def run_app(*args: Any, **kwargs: Any) -> int:
    """Lazy wrapper so non-GUI tests can import ``app`` without PySide6."""

    from app.bootstrap import run_app as _run_app

    return _run_app(*args, **kwargs)


__all__ = ["run_app"]
