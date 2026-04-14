"""Unit tests for centralized task store."""

from __future__ import annotations

import unittest

from config.models import AppConfig, RabbitMQConfig
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
        payload_keys = set(messages[0].to_dict().keys())
        self.assertEqual(
            payload_keys,
            {"request_id", "action", "QUEU_NAME", "RECIPE_PATH", "IMG_LIST"},
        )
        self.assertNotIn("sent_at", payload_keys)

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

    def test_build_mq_preview_contains_connection_and_payload(self) -> None:
        message = self.store.build_pending_messages("RUN", "result.q", "r.json")[0]
        self.store.set_task_expected_message(message.request_id, payload=message.to_dict())
        self.store.set_task_published_message(
            message.request_id,
            payload=message.to_dict(),
            meta={"routing_key": "task.request"},
        )

        config = AppConfig(
            rabbitmq=RabbitMQConfig(
                host="127.0.0.1",
                port=5672,
                username="guest",
                password="guest",
            )
        )
        preview = self.store.build_mq_preview(
            request_id=message.request_id,
            app_config=config,
            active_result_queue="task.result.client.abc123",
        )

        self.assertIsNotNone(preview)
        assert preview is not None
        self.assertEqual(preview["connection"]["host"], "127.0.0.1")
        self.assertEqual(preview["message"]["request_id"], message.request_id)
        self.assertEqual(preview["payload"]["expected"]["request_id"], message.request_id)
        self.assertNotIn("sent_at", preview["payload"]["expected"])
        self.assertNotIn("sent_at", preview["payload"]["published"])


if __name__ == "__main__":
    unittest.main()
