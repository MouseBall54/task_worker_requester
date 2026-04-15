"""Broker abstraction for RabbitMQ and mock adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from models.task_models import TaskMessage


@dataclass(slots=True)
class BrokerResultEnvelope:
    """Normalized result message returned by broker polling."""

    payload: dict[str, Any]
    message_id: str | None = None
    correlation_id: str | None = None


BrokerConsumeCallback = Callable[[BrokerResultEnvelope], None]


class AbstractBrokerClient(ABC):
    """Common interface used by publish/polling workers."""

    @abstractmethod
    def connect(self) -> None:
        """Open connection/channel resources."""

    @abstractmethod
    def close(self) -> None:
        """Close opened resources safely."""

    @abstractmethod
    def declare_result_queue(self, queue_name: str) -> str:
        """Create or ensure existence of the configured result queue."""

    @abstractmethod
    def publish_task(self, task_message: TaskMessage) -> None:
        """Publish one image task message to RabbitMQ."""

    @abstractmethod
    def start_result_consumer(
        self,
        queue_name: str,
        on_envelope: BrokerConsumeCallback,
        prefetch_count: int,
    ) -> None:
        """Register a result consumer callback for the queue."""

    @abstractmethod
    def pump_events(self, time_limit_seconds: float) -> int:
        """Process broker events for up to time_limit_seconds and return delivered count."""

    @abstractmethod
    def stop_result_consumer(self) -> None:
        """Cancel any registered result consumer safely."""

    @abstractmethod
    def ping(self) -> bool:
        """Return broker liveness state."""
