"""Tests for request-queue metrics monitoring worker."""

from __future__ import annotations

import threading
import unittest

from services.broker.base import (
    AbstractBrokerClient,
    BrokerConsumeCallback,
    BrokerQueueStats,
    BrokerResultEnvelope,
)

try:
    from PySide6.QtCore import QCoreApplication
    from services.workers.queue_metrics_worker import QueueMetricsWorker

    PYSIDE_AVAILABLE = True
except ImportError:  # pragma: no cover
    QCoreApplication = None  # type: ignore[assignment]
    QueueMetricsWorker = None  # type: ignore[assignment]
    PYSIDE_AVAILABLE = False


class FakeQueueStatsBroker(AbstractBrokerClient):
    """Broker double with configurable queue stats or failures."""

    def __init__(self, *, should_fail: bool = False) -> None:
        self.connected = False
        self.should_fail = should_fail
        self.consumer_count = 2
        self.message_count = 15

    def connect(self) -> None:
        self.connected = True

    def close(self) -> None:
        self.connected = False

    def declare_result_queue(self, queue_name: str) -> str:
        return queue_name

    def publish_task(self, task_message) -> None:  # noqa: ANN001
        _ = task_message

    def start_result_consumer(
        self,
        queue_name: str,
        on_envelope: BrokerConsumeCallback,
        prefetch_count: int,
    ) -> None:
        _ = (queue_name, on_envelope, prefetch_count)

    def pump_events(self, time_limit_seconds: float) -> int:
        _ = time_limit_seconds
        return 0

    def stop_result_consumer(self) -> None:
        return

    def get_queue_stats(self, queue_name: str) -> BrokerQueueStats:
        _ = queue_name
        if self.should_fail:
            raise RuntimeError("metrics unavailable")
        return BrokerQueueStats(
            consumer_count=self.consumer_count,
            message_count=self.message_count,
        )

    def ping(self) -> bool:
        return self.connected


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 is required for queue metrics worker tests.")
class QueueMetricsWorkerTest(unittest.TestCase):
    """Validate periodic queue metrics updates and failure fallback."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QCoreApplication.instance() or QCoreApplication([])

    def test_worker_emits_queue_stats(self) -> None:
        broker = FakeQueueStatsBroker()
        worker = QueueMetricsWorker(
            broker_provider=lambda: broker,
            queue_name="task.request",
            interval_seconds=1,
        )

        snapshots: list[tuple[int, int]] = []
        worker.metrics_updated.connect(lambda consumers, messages: snapshots.append((consumers, messages)))

        stopper = threading.Timer(0.25, worker.stop)
        stopper.start()
        try:
            worker.run()
        finally:
            stopper.cancel()

        self.assertTrue(any(consumers == 2 and messages == 15 for consumers, messages in snapshots))

    def test_worker_emits_unavailable_snapshot_on_failure(self) -> None:
        broker = FakeQueueStatsBroker(should_fail=True)
        worker = QueueMetricsWorker(
            broker_provider=lambda: broker,
            queue_name="task.request",
            interval_seconds=1,
        )

        snapshots: list[tuple[int, int]] = []
        worker.metrics_updated.connect(lambda consumers, messages: snapshots.append((consumers, messages)))

        stopper = threading.Timer(0.25, worker.stop)
        stopper.start()
        try:
            worker.run()
        finally:
            stopper.cancel()

        self.assertTrue(any(consumers == -1 and messages == -1 for consumers, messages in snapshots))


if __name__ == "__main__":
    unittest.main()
