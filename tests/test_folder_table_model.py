"""Unit tests for folder table model row maintenance helpers."""

from __future__ import annotations

import unittest

from models.task_models import FolderSummary, TaskStatus

try:
    from ui.models.folder_table_model import FolderTableModel

    PYSIDE_AVAILABLE = True
except ImportError:  # pragma: no cover
    PYSIDE_AVAILABLE = False


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 is required for folder model tests.")
class FolderTableModelTest(unittest.TestCase):
    """Validate remove/upsert behaviors for folder rows."""

    def _summary(self, folder_path: str, status: TaskStatus) -> FolderSummary:
        return FolderSummary(
            folder_path=folder_path,
            total=10,
            completed=5,
            success=4,
            fail=1,
            timeout=0,
            error=0,
            progress=50.0,
            status=status,
        )

    def test_remove_by_folder_path_updates_index_map(self) -> None:
        model = FolderTableModel()
        model.set_rows(
            [
                self._summary("folder_a", TaskStatus.RUNNING),
                self._summary("folder_b", TaskStatus.PENDING),
                self._summary("folder_c", TaskStatus.SUCCESS),
            ]
        )

        model.remove_by_folder_path("folder_b")

        self.assertEqual(model.rowCount(), 2)
        self.assertFalse(model.has_folder("folder_b"))
        self.assertTrue(model.has_folder("folder_a"))
        self.assertTrue(model.has_folder("folder_c"))
        self.assertEqual(model.folder_at(1), "folder_c")

    def test_upsert_after_remove_keeps_consistent_rows(self) -> None:
        model = FolderTableModel()
        model.set_rows(
            [
                self._summary("folder_a", TaskStatus.RUNNING),
                self._summary("folder_b", TaskStatus.PENDING),
            ]
        )
        model.remove_by_folder_path("folder_a")
        model.upsert_summary(self._summary("folder_c", TaskStatus.SUCCESS))

        self.assertEqual(model.rowCount(), 2)
        self.assertEqual(model.folder_at(0), "folder_b")
        self.assertEqual(model.folder_at(1), "folder_c")


if __name__ == "__main__":
    unittest.main()
