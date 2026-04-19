"""Worker that periodically samples queue-level broker metrics."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from PySide6.QtCore import QObject, Signal, Slot

from services.broker.base import AbstractBrokerClient, BrokerQueueStats


class QueueMetricsWorker(QObject):
    """Background monitor for queue consumer/message counters."""

    metrics_updated = Signal(int, int)
    log = Signal(str)
    finished = Signal()

    def __init__(
        self,
        broker_provider: Callable[[], AbstractBrokerClient],
        queue_name: str,
        interval_seconds: int = 5,
    ) -> None:
        super().__init__()
        self._broker_provider = broker_provider
        self._queue_name = queue_name
        self._interval_seconds = max(1, int(interval_seconds))
        self._stop_event = threading.Event()

    @Slot()
    def run(self) -> None:
        """Poll queue stats until stop is requested."""

        broker = self._broker_provider()
        was_healthy: bool | None = None

        try:
            while not self._stop_event.is_set():
                try:
                    broker.connect()
                    stats = broker.get_queue_stats(self._queue_name)
                    self._emit_stats(stats)
                    if was_healthy is not True:
                        self.log.emit(f"Queue metrics monitor active: {self._queue_name}")
                    was_healthy = True
                except Exception as exc:  # pylint: disable=broad-except
                    self.metrics_updated.emit(-1, -1)
                    if was_healthy is not False:
                        self.log.emit(f"Queue metrics 조회 실패: {exc}")
                    was_healthy = False
                    try:
                        broker.close()
                    except Exception:  # pragma: no cover - best effort cleanup
                        pass
                    broker = self._broker_provider()

                self._stop_event.wait(self._interval_seconds)
        finally:
            try:
                broker.close()
            except Exception:  # pragma: no cover - best effort cleanup
                pass
            self.finished.emit()

    @Slot()
    def stop(self) -> None:
        """Request graceful shutdown for metrics loop."""

        self._stop_event.set()

    def _emit_stats(self, stats: BrokerQueueStats) -> None:
        """Normalize and emit one metrics snapshot."""

        consumer_count = max(0, int(stats.consumer_count))
        message_count = max(0, int(stats.message_count))
        self.metrics_updated.emit(consumer_count, message_count)
