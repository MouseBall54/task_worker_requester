"""Unit tests for centralized task store."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
            priority=4,
        )
        self.assertEqual(len(messages), 3)
        self.assertTrue(all(len(message.IMG_LIST) == 1 for message in messages))
        self.assertTrue(all(message.priority == 4 for message in messages))
        payload_keys = set(messages[0].to_dict().keys())
        self.assertEqual(
            payload_keys,
            {"request_id", "action", "QUEUE_NAME", "RECIPE_PATH", "IMG_LIST"},
        )
        self.assertNotIn("sent_at", payload_keys)

    def test_build_pending_messages_by_folder_keeps_folder_order(self) -> None:
        grouped = self.store.build_pending_messages_by_folder(
            action="RUN_RECIPE",
            result_queue_name="result.client.1",
            recipe_path="recipe.json",
        )
        self.assertEqual(len(grouped), 2)
        self.assertEqual(grouped[0][0], "folder_a")
        self.assertEqual(grouped[1][0], "folder_b")
        self.assertEqual(len(grouped[0][1]), 2)
        self.assertEqual(len(grouped[1][1]), 1)
        self.assertTrue(all(len(message.IMG_LIST) == 1 for _, messages in grouped for message in messages))

    def test_build_pending_messages_for_folders_filters_targets_and_exclusions(self) -> None:
        all_grouped = self.store.build_pending_messages_by_folder(
            action="RUN_RECIPE",
            result_queue_name="result.client.1",
            recipe_path="recipe.json",
        )
        excluded_request_id = all_grouped[0][1][0].request_id
        grouped = self.store.build_pending_messages_for_folders(
            action="RUN_RECIPE",
            result_queue_name="result.client.1",
            recipe_path="recipe.json",
            folder_paths=["folder_a"],
            exclude_request_ids={excluded_request_id},
        )

        self.assertEqual(len(grouped), 1)
        self.assertEqual(grouped[0][0], "folder_a")
        self.assertEqual(len(grouped[0][1]), 1)
        self.assertNotEqual(grouped[0][1][0].request_id, excluded_request_id)

    def test_remove_pending_only_folders_blocks_non_pending_folders(self) -> None:
        running_task = self.store.get_image_tasks("folder_b")[0]
        self.store.mark_task_sent(running_task.request_id)

        removed_folders, blocked_folders, removed_request_ids, removed_task_count = (
            self.store.remove_pending_only_folders(["folder_a", "folder_b"])
        )

        self.assertEqual(removed_folders, ["folder_a"])
        self.assertEqual(blocked_folders, ["folder_b"])
        self.assertEqual(removed_task_count, 2)
        self.assertEqual(len(removed_request_ids), 2)
        self.assertIsNone(self.store.get_folder_summary("folder_a"))
        self.assertIsNotNone(self.store.get_folder_summary("folder_b"))

    def test_has_inflight_tasks_only_for_sent_or_running(self) -> None:
        first_task = self.store.get_image_tasks("folder_a")[0]
        self.assertFalse(self.store.has_inflight_tasks())

        self.store.mark_task_sent(first_task.request_id)
        self.assertTrue(self.store.has_inflight_tasks())

        first_task.status = TaskStatus.SUCCESS
        self.assertFalse(self.store.has_inflight_tasks())

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
            active_result_queue="task.result.client",
            runtime_action="RUNTIME_RUN",
            runtime_recipe_path="runtime_recipe.json",
        )

        self.assertIsNotNone(preview)
        assert preview is not None
        self.assertEqual(preview["connection"]["host"], "127.0.0.1")
        self.assertEqual(preview["message"]["request_id"], message.request_id)
        self.assertEqual(preview["payload"]["expected"]["request_id"], message.request_id)
        self.assertEqual(preview["payload"]["expected"]["action"], "RUN")
        self.assertEqual(preview["connection"]["predicted_result_queue"], "task.result.client")
        self.assertIn("request_queue_declare", preview["connection"])
        self.assertIn("result_queue_declare", preview["connection"])
        self.assertEqual(preview["connection"]["request_queue_max_priority"], 5)
        self.assertEqual(preview["message"]["selected_priority"], 0)
        self.assertNotIn("sent_at", preview["payload"]["expected"])
        self.assertNotIn("sent_at", preview["payload"]["published"])
        self.assertEqual(preview["payload"]["received"], {})
        self.assertEqual(preview["message"]["received_meta"], {})

    def test_build_mq_preview_generates_expected_payload_for_pending_task(self) -> None:
        message = self.store.build_pending_messages("RUN", "result.q", "r.json")[0]
        config = AppConfig(
            rabbitmq=RabbitMQConfig(
                host="127.0.0.1",
                port=5672,
                username="guest",
                password="guest",
                request_exchange="",
                request_routing_key="custom.route",
                request_queue="task.request.queue",
                result_queue_base="task.result.client",
            )
        )

        preview = self.store.build_mq_preview(
            request_id=message.request_id,
            app_config=config,
            active_result_queue=None,
            runtime_action="RUN_PREVIEW",
            runtime_recipe_path="recipes/preview.json",
            runtime_priority=3,
        )

        self.assertIsNotNone(preview)
        assert preview is not None
        task = self.store.get_task(message.request_id)
        self.assertIsNotNone(task)
        assert task is not None
        expected_payload = preview["payload"]["expected"]
        self.assertEqual(expected_payload["request_id"], message.request_id)
        self.assertEqual(expected_payload["action"], "RUN_PREVIEW")
        self.assertEqual(expected_payload["RECIPE_PATH"], "recipes/preview.json")
        self.assertEqual(expected_payload["QUEUE_NAME"], "task.result.client")
        self.assertEqual(expected_payload["IMG_LIST"], [task.image_path])
        self.assertEqual(preview["payload"]["published"], {})
        self.assertEqual(preview["connection"]["predicted_result_queue"], "task.result.client")
        self.assertEqual(preview["message"]["publish_meta"]["routing_key"], "task.request.queue")
        self.assertEqual(preview["message"]["selected_priority"], 3)
        self.assertEqual(preview["message"]["publish_meta"]["priority"], 3)

    def test_build_mq_preview_uses_active_result_queue_when_present(self) -> None:
        message = self.store.build_pending_messages("RUN", "result.q", "r.json")[0]
        config = AppConfig(
            rabbitmq=RabbitMQConfig(
                host="127.0.0.1",
                port=5672,
                username="guest",
                password="guest",
                result_queue_base="task.result.client",
            )
        )

        preview = self.store.build_mq_preview(
            request_id=message.request_id,
            app_config=config,
            active_result_queue="task.result.client",
            runtime_action="RUN_PREVIEW",
            runtime_recipe_path="recipes/preview.json",
        )

        self.assertIsNotNone(preview)
        assert preview is not None
        self.assertEqual(preview["payload"]["expected"]["QUEUE_NAME"], "task.result.client")
        self.assertEqual(preview["connection"]["active_result_queue"], "task.result.client")
        self.assertEqual(preview["connection"]["predicted_result_queue"], "task.result.client")

    def test_build_pending_messages_by_folder_keeps_runtime_priority(self) -> None:
        grouped = self.store.build_pending_messages_by_folder(
            action="RUN_RECIPE",
            result_queue_name="result.client.1",
            recipe_path="recipe.json",
            priority=2,
        )

        self.assertTrue(grouped)
        self.assertTrue(all(message.priority == 2 for _, messages in grouped for message in messages))

    def test_set_task_received_message_keeps_first_payload(self) -> None:
        message = self.store.build_pending_messages("RUN", "result.q", "r.json")[0]
        self.store.set_task_received_message(
            request_id=message.request_id,
            payload={"request_id": message.request_id, "result": ["FAIL"], "status": "FAILED"},
            meta={"matched_by": "payload.request_id"},
        )
        self.store.set_task_received_message(
            request_id=message.request_id,
            payload={"request_id": message.request_id, "result": ["PASS"], "status": "DONE"},
            meta={"matched_by": "correlation_id"},
        )

        task = self.store.get_task(message.request_id)
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.received_message, {"request_id": message.request_id, "result": ["FAIL"], "status": "FAILED"})
        self.assertEqual(task.received_meta.get("matched_by"), "payload.request_id")

    def test_build_mq_preview_contains_received_payload_and_meta(self) -> None:
        message = self.store.build_pending_messages("RUN", "result.q", "r.json")[0]
        self.store.set_task_received_message(
            request_id=message.request_id,
            payload={
                "request_id": message.request_id,
                "result": ["PASS", "label_ok"],
                "status": "DONE",
            },
            meta={
                "message_id": message.request_id,
                "correlation_id": message.request_id,
                "matched_by": "payload.request_id",
                "received_at": "2026-04-15T10:00:00+09:00",
            },
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
            active_result_queue="task.result.client",
            runtime_action="RUN_PREVIEW",
            runtime_recipe_path="recipes/preview.json",
        )

        self.assertIsNotNone(preview)
        assert preview is not None
        self.assertEqual(preview["payload"]["received"]["status"], "DONE")
        self.assertEqual(preview["message"]["received_meta"]["matched_by"], "payload.request_id")

    def test_reset_clears_received_message_cache(self) -> None:
        message = self.store.build_pending_messages("RUN", "result.q", "r.json")[0]
        self.store.set_task_received_message(
            request_id=message.request_id,
            payload={"request_id": message.request_id, "status": "DONE"},
            meta={"matched_by": "payload.request_id"},
        )

        config = AppConfig(
            rabbitmq=RabbitMQConfig(
                host="127.0.0.1",
                port=5672,
                username="guest",
                password="guest",
            )
        )
        preview_before = self.store.build_mq_preview(
            request_id=message.request_id,
            app_config=config,
            active_result_queue="task.result.client",
            runtime_action="RUN_PREVIEW",
            runtime_recipe_path="recipes/preview.json",
        )
        self.assertIsNotNone(preview_before)
        assert preview_before is not None
        self.assertTrue(preview_before["payload"]["received"])

        self.store.reset()

        preview_after = self.store.build_mq_preview(
            request_id=message.request_id,
            app_config=config,
            active_result_queue="task.result.client",
            runtime_action="RUN_PREVIEW",
            runtime_recipe_path="recipes/preview.json",
        )
        self.assertIsNone(preview_after)

    def test_mark_inflight_running_promotes_sent_tasks(self) -> None:
        messages = self.store.build_pending_messages("RUN", "result.q", "r.json")
        for message in messages:
            self.store.mark_task_sent(message.request_id)

        changed_count = self.store.mark_inflight_running()
        self.assertEqual(changed_count, 3)
        for message in messages:
            task = self.store.get_task(message.request_id)
            self.assertIsNotNone(task)
            assert task is not None
            self.assertEqual(task.status, TaskStatus.RUNNING)

        summaries = {summary.folder_path: summary for summary in self.store.get_folder_summaries()}
        self.assertEqual(summaries["folder_a"].status, TaskStatus.RUNNING)
        self.assertEqual(summaries["folder_b"].status, TaskStatus.RUNNING)

    def test_timeout_keeps_running_path(self) -> None:
        message = self.store.build_pending_messages("RUN", "result.q", "r.json")[0]
        self.store.mark_task_sent(message.request_id)
        self.store.mark_inflight_running()

        task = self.store.get_task(message.request_id)
        assert task is not None
        task.sent_at = datetime.now(timezone.utc) - timedelta(seconds=5)

        timed_out = self.store.mark_timeouts(timeout_seconds=1)
        self.assertIn(message.request_id, timed_out)
        self.assertEqual(task.status, TaskStatus.TIMEOUT)

    def test_overall_stats_includes_avg_and_eta(self) -> None:
        message = self.store.build_pending_messages("RUN", "result.q", "r.json")[0]
        self.store.mark_task_sent(message.request_id)

        task = self.store.get_task(message.request_id)
        assert task is not None
        task.sent_at = datetime.now(timezone.utc) - timedelta(seconds=4)
        task.completed_at = datetime.now(timezone.utc)
        task.status = TaskStatus.SUCCESS

        stats = self.store.overall_stats()
        self.assertIsInstance(stats["avg_processing_seconds"], float)
        self.assertGreater(float(stats["avg_processing_seconds"] or 0), 0.0)
        self.assertIsInstance(stats["eta_seconds"], float)
        self.assertGreaterEqual(float(stats["eta_seconds"] or 0), 0.0)

    def test_overall_stats_uses_elapsed_per_completed_for_avg(self) -> None:
        messages = self.store.build_pending_messages("RUN", "result.q", "r.json")
        now = datetime.now(timezone.utc)

        for message in messages[:2]:
            self.store.mark_task_sent(message.request_id)

        task_a = self.store.get_task(messages[0].request_id)
        task_b = self.store.get_task(messages[1].request_id)
        assert task_a is not None
        assert task_b is not None

        task_a.sent_at = now - timedelta(seconds=10)
        task_a.completed_at = now - timedelta(seconds=2)
        task_a.status = TaskStatus.SUCCESS

        task_b.sent_at = now - timedelta(seconds=10)
        task_b.completed_at = now - timedelta(seconds=1)
        task_b.status = TaskStatus.FAIL

        stats = self.store.overall_stats()
        avg_seconds = float(stats["avg_processing_seconds"] or 0.0)
        eta_seconds = float(stats["eta_seconds"] or 0.0)
        elapsed = 10.0
        completed = 2
        remaining = 1

        self.assertAlmostEqual(avg_seconds, elapsed / completed, delta=0.5)
        self.assertAlmostEqual(eta_seconds, (elapsed / completed) * remaining, delta=0.7)

    def test_overall_stats_includes_terminal_statuses_in_completed_count(self) -> None:
        messages = self.store.build_pending_messages("RUN", "result.q", "r.json")
        now = datetime.now(timezone.utc)

        for message in messages:
            self.store.mark_task_sent(message.request_id)

        task_success = self.store.get_task(messages[0].request_id)
        task_timeout = self.store.get_task(messages[1].request_id)
        task_error = self.store.get_task(messages[2].request_id)
        assert task_success is not None
        assert task_timeout is not None
        assert task_error is not None

        task_success.sent_at = now - timedelta(seconds=5)
        task_success.completed_at = now - timedelta(seconds=3)
        task_success.status = TaskStatus.SUCCESS

        task_timeout.sent_at = now - timedelta(seconds=120)
        task_timeout.completed_at = now
        task_timeout.status = TaskStatus.TIMEOUT

        task_error.sent_at = now - timedelta(seconds=90)
        task_error.completed_at = now
        task_error.status = TaskStatus.ERROR

        stats = self.store.overall_stats()
        self.assertEqual(int(stats["completed"] or 0), 3)
        self.assertIsInstance(stats["avg_processing_seconds"], float)
        self.assertEqual(float(stats["eta_seconds"] or 0.0), 0.0)

    def test_overall_stats_returns_none_when_completed_zero(self) -> None:
        now = datetime.now(timezone.utc)
        for message in self.store.build_pending_messages("RUN", "result.q", "r.json"):
            self.store.mark_task_sent(message.request_id)
            task = self.store.get_task(message.request_id)
            assert task is not None
            task.sent_at = now - timedelta(seconds=5)
            task.status = TaskStatus.RUNNING

        stats = self.store.overall_stats()
        self.assertIsNone(stats["avg_processing_seconds"])
        self.assertIsNone(stats["eta_seconds"])

    def test_overall_stats_returns_none_when_elapsed_not_positive(self) -> None:
        message = self.store.build_pending_messages("RUN", "result.q", "r.json")[0]
        self.store.mark_task_sent(message.request_id)

        now = datetime.now(timezone.utc)
        task = self.store.get_task(message.request_id)
        assert task is not None
        task.sent_at = now + timedelta(seconds=30)
        task.completed_at = now + timedelta(seconds=31)
        task.status = TaskStatus.SUCCESS

        stats = self.store.overall_stats()
        self.assertIsNone(stats["avg_processing_seconds"])
        self.assertIsNone(stats["eta_seconds"])

    def test_overall_stats_handles_naive_aware_datetime_mix(self) -> None:
        message = self.store.build_pending_messages("RUN", "result.q", "r.json")[0]
        self.store.mark_task_sent(message.request_id)

        task = self.store.get_task(message.request_id)
        assert task is not None
        task.sent_at = datetime.utcnow() - timedelta(seconds=3)
        task.completed_at = datetime.now(timezone.utc)
        task.status = TaskStatus.SUCCESS

        stats = self.store.overall_stats()
        self.assertIsInstance(stats["avg_processing_seconds"], float)
        self.assertGreater(float(stats["avg_processing_seconds"] or 0.0), 0.0)

    def test_overall_stats_eta_decreases_as_completed_increases(self) -> None:
        messages = self.store.build_pending_messages("RUN", "result.q", "r.json")
        now = datetime.now(timezone.utc)

        for message in messages:
            self.store.mark_task_sent(message.request_id)
            task = self.store.get_task(message.request_id)
            assert task is not None
            task.sent_at = now - timedelta(seconds=12)

        task1 = self.store.get_task(messages[0].request_id)
        task2 = self.store.get_task(messages[1].request_id)
        assert task1 is not None
        assert task2 is not None

        task1.completed_at = now - timedelta(seconds=2)
        task1.status = TaskStatus.SUCCESS
        eta_after_one = float(self.store.overall_stats()["eta_seconds"] or 0.0)

        task2.completed_at = now - timedelta(seconds=1)
        task2.status = TaskStatus.FAIL
        eta_after_two = float(self.store.overall_stats()["eta_seconds"] or 0.0)

        self.assertGreater(eta_after_one, eta_after_two)


if __name__ == "__main__":
    unittest.main()
