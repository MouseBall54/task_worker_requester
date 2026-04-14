"""Minimal Qt compatibility layer for non-GUI unit test environments."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

try:  # pragma: no cover - exercised when PySide6 is installed
    from PySide6.QtCore import QObject, Signal, Slot
except ImportError:  # pragma: no cover - exercised in lightweight CI/unit envs

    class _BoundSignal:
        """Simple in-process signal implementation for tests."""

        def __init__(self) -> None:
            self._subscribers: list[Callable[..., Any]] = []

        def connect(self, callback: Callable[..., Any]) -> None:
            self._subscribers.append(callback)

        def emit(self, *args: Any, **kwargs: Any) -> None:
            for callback in list(self._subscribers):
                callback(*args, **kwargs)

    class Signal:
        """Descriptor-based signal fallback with per-instance subscriptions."""

        def __init__(self, *args: Any) -> None:
            self._storage_name = ""

        def __set_name__(self, owner: type, name: str) -> None:
            self._storage_name = f"__signal_{name}"

        def __get__(self, instance: Any, owner: type | None = None) -> _BoundSignal | Signal:
            if instance is None:
                return self
            bound = instance.__dict__.get(self._storage_name)
            if bound is None:
                bound = _BoundSignal()
                instance.__dict__[self._storage_name] = bound
            return bound

    class QObject:
        """QObject placeholder used when Qt is unavailable."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__()

    def Slot(*types: Any, **kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """No-op Slot decorator fallback."""

        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            return func

        return decorator


__all__ = ["QObject", "Signal", "Slot"]
