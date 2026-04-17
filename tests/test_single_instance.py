"""Tests for duplicate-instance protection."""

from __future__ import annotations

import unittest
from uuid import uuid4

try:
    from PySide6.QtWidgets import QApplication
    from app.single_instance import ensure_single_instance

    PYSIDE_AVAILABLE = True
except ImportError:  # pragma: no cover
    QApplication = None  # type: ignore[assignment]
    ensure_single_instance = None  # type: ignore[assignment]
    PYSIDE_AVAILABLE = False


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 is required for single-instance tests.")
class SingleInstanceGuardTest(unittest.TestCase):
    """Verify that only one guard can own a given server key at a time."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])

    def test_guard_blocks_duplicate_instance_until_released(self) -> None:
        server_key = f"ipdk_plus_test_{uuid4().hex}"

        first_guard = ensure_single_instance(server_key)
        self.assertIsNotNone(first_guard)
        assert first_guard is not None

        second_guard = ensure_single_instance(server_key)
        self.assertIsNone(second_guard)

        first_guard.release()

        third_guard = ensure_single_instance(server_key)
        self.assertIsNotNone(third_guard)
        assert third_guard is not None
        third_guard.release()


if __name__ == "__main__":
    unittest.main()
