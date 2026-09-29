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

    def _summary(
        self,
        folder_path: str,
        status: TaskStatus,
        queue_priority: int = 3,
    ) -> FolderSummary:
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
            recipe_aliases=("Recipe A", "Recipe B"),
            queue_priority=queue_priority,
        )

    def test_recipe_aliases_are_visible(self) -> None:
        model = FolderTableModel()
        model.set_rows([self._summary("folder_a", TaskStatus.PENDING)])

        self.assertEqual(model.data(model.index(0, 0)), 3)
        self.assertEqual(model.data(model.index(0, 4)), "Recipe A, Recipe B")

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

    def test_new_rows_are_immediately_inserted_in_queue_priority_order(self) -> None:
        model = FolderTableModel()

        model.upsert_summary(self._summary("folder_3", TaskStatus.PENDING, 3))
        model.upsert_summary(self._summary("folder_1", TaskStatus.PENDING, 1))
        model.upsert_summary(self._summary("folder_2", TaskStatus.PENDING, 2))

        self.assertEqual(
            [model.folder_at(index) for index in range(model.rowCount())],
            ["folder_1", "folder_2", "folder_3"],
        )

    def test_set_rows_and_priority_update_keep_queue_order(self) -> None:
        model = FolderTableModel()
        model.set_rows(
            [
                self._summary("folder_3", TaskStatus.PENDING, 3),
                self._summary("folder_1", TaskStatus.PENDING, 1),
                self._summary("folder_2", TaskStatus.PENDING, 2),
            ]
        )

        model.upsert_summary(self._summary("folder_3", TaskStatus.PENDING, 1))

        self.assertEqual(
            [model.folder_at(index) for index in range(model.rowCount())],
            ["folder_1", "folder_3", "folder_2"],
        )

    def test_recipe_queues_with_same_source_path_remain_separate_rows(self) -> None:
        model = FolderTableModel()
        first = self._summary("queue_a", TaskStatus.PENDING, 1)
        first.source_path = "shared_folder"
        second = self._summary("queue_b", TaskStatus.PENDING, 2)
        second.source_path = "shared_folder"

        model.upsert_summary(second)
        model.upsert_summary(first)

        self.assertEqual(model.rowCount(), 2)
        self.assertEqual([model.folder_at(0), model.folder_at(1)], ["queue_a", "queue_b"])
        self.assertEqual(model.data(model.index(0, 3)), "shared_folder")
        self.assertEqual(model.data(model.index(1, 3)), "shared_folder")


if __name__ == "__main__":
    unittest.main()
