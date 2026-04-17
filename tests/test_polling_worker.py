"""Tests for the consumer-driven polling worker."""

from __future__ import annotations

import threading
import time
import unittest

from services.broker.base import (
    AbstractBrokerClient,
    BrokerConsumeCallback,
    BrokerConsumeDecision,
    BrokerResultEnvelope,
)

try:
    from PySide6.QtCore import QCoreApplication
    from services.workers.polling_worker import PollingWorker

    PYSIDE_AVAILABLE = True
except ImportError:  # pragma: no cover
    QCoreApplication = None  # type: ignore[assignment]
    PollingWorker = None  # type: ignore[assignment]
    PYSIDE_AVAILABLE = False


class FakeConsumerBroker(AbstractBrokerClient):
    """Small in-memory broker double used to drive worker behavior."""

    def __init__(self) -> None:
        self.connected = False
        self.queue_name: str | None = None
        self.callback: BrokerConsumeCallback | None = None
        self.prefetch_count = 0
        self.consumer_stopped = False
        self.requeued: list[BrokerResultEnvelope] = []
        self.envelopes = [
            BrokerResultEnvelope(payload={"request_id": "req-1"}, message_id="req-1", correlation_id="req-1")
        ]

    def connect(self) -> None:
        self.connected = True

    def close(self) -> None:
        self.connected = False

    def declare_result_queue(self, queue_name: str) -> str:
        self.queue_name = queue_name
        return queue_name

    def publish_task(self, task_message) -> None:  # noqa: ANN001
        _ = task_message

    def start_result_consumer(
        self,
        queue_name: str,
        on_envelope: BrokerConsumeCallback,
        prefetch_count: int,
    ) -> None:
        self.queue_name = queue_name
        self.callback = on_envelope
        self.prefetch_count = prefetch_count

    def pump_events(self, time_limit_seconds: float) -> int:
        time.sleep(min(0.01, max(0.0, time_limit_seconds)))
        if self.callback is None or not self.envelopes:
            return 0

        envelope = self.envelopes.pop(0)
        decision = self.callback(envelope)
        if decision == BrokerConsumeDecision.REQUEUE_AND_PAUSE:
            self.requeued.append(envelope)
            self.stop_result_consumer()
        elif decision == BrokerConsumeDecision.REQUEUE:
            self.requeued.append(envelope)
        return 1

    def stop_result_consumer(self) -> None:
        self.consumer_stopped = True
        self.callback = None

    def ping(self) -> bool:
        return self.connected


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 is required for polling worker tests.")
class PollingWorkerTest(unittest.TestCase):
    """Validate worker behavior with consumer-style broker interface."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QCoreApplication.instance() or QCoreApplication([])

    def test_worker_emits_results_and_ticks_then_stops_consumer(self) -> None:
        broker = FakeConsumerBroker()
        worker = PollingWorker(
            broker_provider=lambda: broker,
            queue_name="result.q",
            polling_interval_seconds=1,
            max_messages_per_poll=5,
            tracked_request_ids={"req-1"},
        )

        received_payloads: list[dict] = []
        tick_counts: list[int] = []
        logs: list[str] = []

        worker.result_received.connect(lambda envelope: received_payloads.append(envelope.payload))
        worker.poll_cycle.connect(tick_counts.append)
        worker.log.connect(logs.append)

        stopper = threading.Timer(1.25, worker.stop)
        stopper.start()
        try:
            worker.run()
        finally:
            stopper.cancel()

        self.assertEqual(broker.prefetch_count, 5)
        self.assertTrue(broker.consumer_stopped)
        self.assertEqual(received_payloads, [{"request_id": "req-1"}])
        self.assertTrue(any(count >= 1 for count in tick_counts))
        self.assertTrue(any("consumer active" in line for line in logs))

    def test_worker_consumes_and_ignores_request_id_mismatch(self) -> None:
        broker = FakeConsumerBroker()
        broker.envelopes = [
            BrokerResultEnvelope(
                payload={"request_id": "foreign-1"},
                message_id="foreign-1",
                correlation_id="foreign-1",
            )
        ]
        worker = PollingWorker(
            broker_provider=lambda: broker,
            queue_name="result.q",
            polling_interval_seconds=1,
            max_messages_per_poll=5,
            tracked_request_ids={"req-1"},
        )

        received_payloads: list[dict] = []
        logs: list[str] = []
        worker.result_received.connect(lambda envelope: received_payloads.append(envelope.payload))
        worker.log.connect(logs.append)

        stopper = threading.Timer(0.4, worker.stop)
        stopper.start()
        try:
            worker.run()
        finally:
            stopper.cancel()

        self.assertEqual(received_payloads, [])
        self.assertTrue(broker.consumer_stopped)
        self.assertEqual(broker.requeued, [])
        self.assertTrue(
            any("request_id mismatch - consumed and ignored: foreign-1" in line for line in logs)
        )

    def test_worker_consumes_and_ignores_missing_request_id(self) -> None:
        broker = FakeConsumerBroker()
        broker.envelopes = [BrokerResultEnvelope(payload={"status": "DONE"})]
        worker = PollingWorker(
            broker_provider=lambda: broker,
            queue_name="result.q",
            polling_interval_seconds=1,
            max_messages_per_poll=5,
            tracked_request_ids={"req-1"},
        )

        received_payloads: list[dict] = []
        logs: list[str] = []
        worker.result_received.connect(lambda envelope: received_payloads.append(envelope.payload))
        worker.log.connect(logs.append)

        stopper = threading.Timer(0.4, worker.stop)
        stopper.start()
        try:
            worker.run()
        finally:
            stopper.cancel()

        self.assertEqual(received_payloads, [])
        self.assertEqual(broker.requeued, [])
        self.assertTrue(
            any("request_id mismatch - consumed and ignored: <missing>" in line for line in logs)
        )


if __name__ == "__main__":
    unittest.main()
