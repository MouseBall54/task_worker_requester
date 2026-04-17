"""Broker client factory exports."""

from __future__ import annotations

from collections.abc import Callable

from config.models import AppConfig
from services.broker.base import (
    AbstractBrokerClient,
    BrokerConsumeDecision,
    BrokerResultEnvelope,
)
from services.broker.mock_broker import MockBrokerClient
from services.broker.rabbitmq_client import RabbitMQClient


def build_broker_provider(config: AppConfig) -> Callable[[], AbstractBrokerClient]:
    """Return lazy client factory used by each worker thread."""

    if config.mock_mode:
        return lambda: MockBrokerClient()
    return lambda: RabbitMQClient(config.rabbitmq)


__all__ = [
    "AbstractBrokerClient",
    "BrokerConsumeDecision",
    "BrokerResultEnvelope",
    "MockBrokerClient",
    "RabbitMQClient",
    "build_broker_provider",
]
