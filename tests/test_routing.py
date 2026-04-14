"""Unit tests for publish routing resolution helpers."""

from __future__ import annotations

import unittest

from config.models import RabbitMQConfig
from services.broker.routing import resolve_publish_route


class RoutingHelperTest(unittest.TestCase):
    """Validate exchange/routing key derivation rules."""

    def test_default_exchange_uses_request_queue(self) -> None:
        config = RabbitMQConfig(
            host="127.0.0.1",
            port=5672,
            username="guest",
            password="guest",
            request_exchange="",
            request_routing_key="custom.route",
            request_queue="task.request.queue",
        )

        exchange, routing_key = resolve_publish_route(config)
        self.assertEqual(exchange, "")
        self.assertEqual(routing_key, "task.request.queue")

    def test_custom_exchange_prefers_request_routing_key(self) -> None:
        config = RabbitMQConfig(
            host="127.0.0.1",
            port=5672,
            username="guest",
            password="guest",
            request_exchange="task.exchange",
            request_routing_key="task.route",
            request_queue="task.request.queue",
        )

        exchange, routing_key = resolve_publish_route(config)
        self.assertEqual(exchange, "task.exchange")
        self.assertEqual(routing_key, "task.route")

    def test_custom_exchange_falls_back_to_request_queue(self) -> None:
        config = RabbitMQConfig(
            host="127.0.0.1",
            port=5672,
            username="guest",
            password="guest",
            request_exchange="task.exchange",
            request_routing_key="",
            request_queue="task.request.queue",
        )

        exchange, routing_key = resolve_publish_route(config)
        self.assertEqual(exchange, "task.exchange")
        self.assertEqual(routing_key, "task.request.queue")


if __name__ == "__main__":
    unittest.main()

