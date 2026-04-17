"""In-memory broker implementation for offline UI/logic testing."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
import threading
import time

from models.task_models import TaskMessage
from services.broker.base import (
    AbstractBrokerClient,
    BrokerConsumeCallback,
    BrokerConsumeDecision,
    BrokerResultEnvelope,
)


@dataclass(slots=True)
class _ScheduledResult:
    """Delayed synthetic result used by mock mode."""

    available_at: float
    queue_name: str
    envelope: BrokerResultEnvelope


class _MockBackend:
    """Shared storage across multiple mock client instances."""

    _lock = threading.Lock()
    _result_queues: dict[str, deque[BrokerResultEnvelope]] = defaultdict(deque)
    _scheduled: list[_ScheduledResult] = []

    @classmethod
    def declare_queue(cls, queue_name: str) -> None:
        with cls._lock:
            _ = cls._result_queues[queue_name]

    @classmethod
    def schedule_result(cls, task_message: TaskMessage) -> None:
        queue_name = task_message.QUEUE_NAME
        cls.declare_queue(queue_name)

        # Keep deterministic-ish behavior for testing: some fail, mostly pass.
        seed = sum(ord(ch) for ch in task_message.request_id)
        is_pass = (seed % 8) != 0
        delay_seconds = 0.25 + (seed % 5) * 0.25

        payload = {
            "request_id": task_message.request_id,
            "result": ["PASS", "mock_label_ok"] if is_pass else ["FAIL", "mock_rule_ng"],
            "status": "DONE" if is_pass else "FAILED",
            "error": None if is_pass else "Mock validation failed",
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }

        envelope = BrokerResultEnvelope(
            payload=payload,
            message_id=task_message.request_id,
            correlation_id=task_message.request_id,
        )

        with cls._lock:
            cls._scheduled.append(
                _ScheduledResult(
                    available_at=time.monotonic() + delay_seconds,
                    queue_name=queue_name,
                    envelope=envelope,
                )
            )

    @classmethod
    def collect_results(cls, queue_name: str, max_messages: int) -> list[BrokerResultEnvelope]:
        now = time.monotonic()
        with cls._lock:
            due_items = [item for item in cls._scheduled if item.available_at <= now]
            cls._scheduled = [item for item in cls._scheduled if item.available_at > now]
            for item in due_items:
                cls._result_queues[item.queue_name].append(item.envelope)

            queue = cls._result_queues[queue_name]
            messages: list[BrokerResultEnvelope] = []
            for _ in range(min(max_messages, len(queue))):
                messages.append(queue.popleft())
        return messages

    @classmethod
    def requeue_front(cls, queue_name: str, envelope: BrokerResultEnvelope) -> None:
        """Requeue one envelope to the front of the named mock queue."""

        with cls._lock:
            cls._result_queues[queue_name].appendleft(envelope)


class MockBrokerClient(AbstractBrokerClient):
    """Mock broker implementing same behavior contract as RabbitMQ client."""

    def __init__(self) -> None:
        self._connected = False
        self._consumer_queue_name: str | None = None
        self._consumer_callback: BrokerConsumeCallback | None = None
        self._prefetch_count = 1

    def connect(self) -> None:
        self._connected = True

    def close(self) -> None:
        self._connected = False

    def declare_result_queue(self, queue_name: str) -> str:
        _MockBackend.declare_queue(queue_name)
        return queue_name

    def publish_task(self, task_message: TaskMessage) -> None:
        self._ensure_connected()
        _MockBackend.schedule_result(task_message)

    def start_result_consumer(
        self,
        queue_name: str,
        on_envelope: BrokerConsumeCallback,
        prefetch_count: int,
    ) -> None:
        self._ensure_connected()
        _MockBackend.declare_queue(queue_name)
        self._consumer_queue_name = queue_name
        self._consumer_callback = on_envelope
        self._prefetch_count = max(1, int(prefetch_count))

    def pump_events(self, time_limit_seconds: float) -> int:
        self._ensure_connected()
        if self._consumer_queue_name is None or self._consumer_callback is None:
            time.sleep(max(0.0, float(time_limit_seconds)))
            return 0

        time.sleep(max(0.0, float(time_limit_seconds)))
        envelopes = _MockBackend.collect_results(
            queue_name=self._consumer_queue_name,
            max_messages=self._prefetch_count,
        )
        for envelope in envelopes:
            try:
                decision = self._consumer_callback(envelope)
            except Exception:
                decision = BrokerConsumeDecision.REQUEUE_AND_PAUSE
            if not isinstance(decision, BrokerConsumeDecision):
                decision = BrokerConsumeDecision.ACK
            if decision == BrokerConsumeDecision.ACK:
                continue
            _MockBackend.requeue_front(self._consumer_queue_name, envelope)
            if decision == BrokerConsumeDecision.REQUEUE_AND_PAUSE:
                self.stop_result_consumer()
                break
        return len(envelopes)

    def stop_result_consumer(self) -> None:
        self._consumer_queue_name = None
        self._consumer_callback = None
        self._prefetch_count = 1

    def ping(self) -> bool:
        return self._connected

    def _ensure_connected(self) -> None:
        if not self._connected:
            raise RuntimeError("Mock broker 연결이 닫혀 있습니다.")
