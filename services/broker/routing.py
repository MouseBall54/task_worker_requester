"""Shared publish routing resolution helpers."""

from __future__ import annotations

from config.models import RabbitMQConfig


def resolve_publish_route(config: RabbitMQConfig) -> tuple[str, str]:
    """Resolve exchange/routing_key pair from RabbitMQ config.

    Rules
    -----
    - Default exchange(`""`): routing_key must be the target queue name.
    - Custom exchange: use request_routing_key first, then fallback to request_queue.
    """

    exchange = config.request_exchange.strip()
    request_queue = config.request_queue.strip()
    request_routing_key = config.request_routing_key.strip()

    if not exchange:
        return exchange, request_queue

    return exchange, (request_routing_key or request_queue)

