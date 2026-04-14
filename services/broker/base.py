"""Broker abstraction for RabbitMQ and mock adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from models.task_models import TaskMessage


@dataclass(slots=True)
class BrokerResultEnvelope:
    """Normalized result message returned by broker polling."""

    payload: dict[str, Any]
    message_id: str | None = None
    correlation_id: str | None = None


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
    def poll_results(self, queue_name: str, max_messages: int) -> list[BrokerResultEnvelope]:
        """Pull messages from result queue and acknowledge internally."""

    @abstractmethod
    def ping(self) -> bool:
        """Return broker liveness state."""
