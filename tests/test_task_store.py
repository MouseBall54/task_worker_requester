"""Unit tests for centralized task store."""

from __future__ import annotations

import unittest

from models.task_models import TaskResult, TaskStatus
from state.task_store import TaskStore


class TaskStoreTest(unittest.TestCase):
    """Validate summary aggregation and duplicate result handling."""

    def setUp(self) -> None:
        self.store = TaskStore()
        self.store.register_folder_map(
            {
                "folder_a": ["folder_a/img1.jpg", "folder_a/img2.jpg"],
                "folder_b": ["folder_b/img3.jpg"],
            }
        )

    def test_build_pending_messages_one_image_per_message(self) -> None:
        messages = self.store.build_pending_messages(
            action="RUN_RECIPE",
            result_queue_name="result.client.1",
            recipe_path="recipe.json",
        )
        self.assertEqual(len(messages), 3)
        self.assertTrue(all(len(message.IMG_LIST) == 1 for message in messages))

    def test_apply_result_updates_summary(self) -> None:
        messages = self.store.build_pending_messages("RUN", "result.q", "r.json")
        target = messages[0]
        self.store.mark_task_sent(target.request_id)

        changed = self.store.apply_result(
            TaskResult(
                request_id=target.request_id,
                result=["PASS"],
                status="DONE",
            )
        )

        self.assertTrue(changed)
        task = self.store.get_task(target.request_id)
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.status, TaskStatus.SUCCESS)

    def test_duplicate_result_is_ignored(self) -> None:
        message = self.store.build_pending_messages("RUN", "result.q", "r.json")[0]
        self.store.mark_task_sent(message.request_id)

        first = self.store.apply_result(TaskResult(request_id=message.request_id, result=["FAIL"], status="DONE"))
        second = self.store.apply_result(TaskResult(request_id=message.request_id, result=["PASS"], status="DONE"))

        self.assertTrue(first)
        self.assertFalse(second)


if __name__ == "__main__":
    unittest.main()
