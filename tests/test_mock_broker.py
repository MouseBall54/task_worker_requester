"""Tests for the mock broker consumer interface."""

from __future__ import annotations

import time
import unittest

from models.task_models import TaskMessage
from services.broker.base import BrokerConsumeDecision
from services.broker.mock_broker import MockBrokerClient, _MockBackend


class MockBrokerClientTest(unittest.TestCase):
    """Validate consumer-style behavior of the mock broker."""

    def setUp(self) -> None:
        with _MockBackend._lock:
            _MockBackend._result_queues.clear()
            _MockBackend._scheduled.clear()

    def _make_message(self, request_id: str, queue_name: str = "result.q") -> TaskMessage:
        return TaskMessage(
            request_id=request_id,
            action="RUN_RECIPE",
            QUEUE_NAME=queue_name,
            RECIPE_PATH="recipe.json",
            IMG_LIST=[f"{request_id}.jpg"],
        )

    def _force_all_scheduled_due(self) -> None:
        with _MockBackend._lock:
            due_at = time.monotonic()
            for item in _MockBackend._scheduled:
                item.available_at = due_at

    def test_consumer_receives_due_results(self) -> None:
        broker = MockBrokerClient()
        broker.connect()
        broker.declare_result_queue("result.q")

        received = []
        broker.start_result_consumer("result.q", received.append, prefetch_count=10)
        broker.publish_task(self._make_message("req-1"))
        self._force_all_scheduled_due()

        delivered = broker.pump_events(0.0)

        self.assertEqual(delivered, 1)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].payload["request_id"], "req-1")

    def test_stop_result_consumer_prevents_delivery(self) -> None:
        broker = MockBrokerClient()
        broker.connect()
        broker.declare_result_queue("result.q")

        received = []
        broker.start_result_consumer("result.q", received.append, prefetch_count=10)
        broker.stop_result_consumer()
        broker.publish_task(self._make_message("req-2"))
        self._force_all_scheduled_due()

        delivered = broker.pump_events(0.0)

        self.assertEqual(delivered, 0)
        self.assertEqual(received, [])

    def test_prefetch_count_limits_each_pump(self) -> None:
        broker = MockBrokerClient()
        broker.connect()
        broker.declare_result_queue("result.q")

        received = []
        broker.start_result_consumer("result.q", received.append, prefetch_count=1)
        broker.publish_task(self._make_message("req-3"))
        broker.publish_task(self._make_message("req-4"))
        self._force_all_scheduled_due()

        first_delivered = broker.pump_events(0.0)
        second_delivered = broker.pump_events(0.0)

        self.assertEqual(first_delivered, 1)
        self.assertEqual(second_delivered, 1)
        self.assertEqual([item.payload["request_id"] for item in received], ["req-3", "req-4"])

    def test_requeue_and_pause_keeps_message_available(self) -> None:
        broker = MockBrokerClient()
        broker.connect()
        broker.declare_result_queue("result.q")

        decisions: list[str] = []

        def _callback(envelope):  # noqa: ANN001
            decisions.append(envelope.payload["request_id"])
            return BrokerConsumeDecision.REQUEUE_AND_PAUSE

        broker.start_result_consumer("result.q", _callback, prefetch_count=5)
        broker.publish_task(self._make_message("req-pause"))
        self._force_all_scheduled_due()

        delivered = broker.pump_events(0.0)

        self.assertEqual(delivered, 1)
        self.assertEqual(decisions, ["req-pause"])
        self.assertIsNone(broker._consumer_queue_name)
        queued = _MockBackend.collect_results("result.q", max_messages=5)
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0].payload["request_id"], "req-pause")


if __name__ == "__main__":
    unittest.main()
