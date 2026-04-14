"""RabbitMQ client implementation based on pika blocking connection."""

from __future__ import annotations

import json
import time
from typing import Any

from config.models import RabbitMQConfig
from models.task_models import TaskMessage
from services.broker.base import AbstractBrokerClient, BrokerResultEnvelope
from services.broker.routing import resolve_publish_route

try:
    import pika
    from pika.adapters.blocking_connection import BlockingChannel
except ImportError:  # pragma: no cover - depends on runtime package install
    pika = None
    BlockingChannel = Any


class RabbitMQClient(AbstractBrokerClient):
    """RabbitMQ adapter for publish and result polling operations."""

    def __init__(self, config: RabbitMQConfig) -> None:
        self._config = config
        self._connection: pika.BlockingConnection | None = None
        self._channel: BlockingChannel | None = None

    def connect(self) -> None:
        """Connect to RabbitMQ and prepare base request queue."""

        if pika is None:
            raise RuntimeError("pika 패키지가 설치되지 않았습니다. requirements.txt를 설치해주세요.")

        if self._connection and self._connection.is_open:
            return

        credentials = pika.PlainCredentials(self._config.username, self._config.password)
        parameters = pika.ConnectionParameters(
            host=self._config.host,
            port=self._config.port,
            virtual_host=self._config.virtual_host,
            heartbeat=self._config.heartbeat,
            blocked_connection_timeout=self._config.blocked_connection_timeout,
            connection_attempts=self._config.connection_attempts,
            retry_delay=self._config.retry_delay_seconds,
            credentials=credentials,
        )

        self._connection = pika.BlockingConnection(parameters=parameters)
        self._channel = self._connection.channel()

        # Ensure request queue exists when using default exchange.
        self._channel.queue_declare(queue=self._config.request_queue, durable=True)

    def close(self) -> None:
        """Close channel and connection safely."""

        if self._channel and self._channel.is_open:
            self._channel.close()
        if self._connection and self._connection.is_open:
            self._connection.close()
        self._channel = None
        self._connection = None

    def declare_result_queue(self, queue_name: str) -> str:
        """Declare the configured result queue used by this GUI."""

        self._ensure_connected()
        assert self._channel is not None
        self._channel.queue_declare(queue=queue_name, durable=True)
        return queue_name

    def publish_task(self, task_message: TaskMessage) -> None:
        """Publish task as JSON with request/correlation metadata."""

        self._ensure_connected()
        assert self._channel is not None

        body = json.dumps(task_message.to_dict(), ensure_ascii=False)
        exchange, routing_key = resolve_publish_route(self._config)

        properties = pika.BasicProperties(
            message_id=task_message.request_id,
            correlation_id=task_message.request_id,
            reply_to=task_message.QUEU_NAME,
            content_type="application/json",
            delivery_mode=2,
            timestamp=int(time.time()),
        )

        self._channel.basic_publish(
            exchange=exchange,
            routing_key=routing_key,
            body=body,
            properties=properties,
            mandatory=False,
        )

    def poll_results(self, queue_name: str, max_messages: int) -> list[BrokerResultEnvelope]:
        """Fetch up to max_messages from queue via basic_get and ack each."""

        self._ensure_connected()
        assert self._channel is not None

        envelopes: list[BrokerResultEnvelope] = []
        for _ in range(max_messages):
            method_frame, properties, body = self._channel.basic_get(queue=queue_name, auto_ack=False)
            if method_frame is None:
                break

            payload: dict[str, Any]
            try:
                decoded = body.decode("utf-8")
                loaded = json.loads(decoded)
                payload = loaded if isinstance(loaded, dict) else {"raw": loaded}
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                payload = {"error": f"결과 메시지 파싱 실패: {exc}", "raw": str(body)}

            envelopes.append(
                BrokerResultEnvelope(
                    payload=payload,
                    message_id=getattr(properties, "message_id", None),
                    correlation_id=getattr(properties, "correlation_id", None),
                )
            )

            self._channel.basic_ack(delivery_tag=method_frame.delivery_tag)

        return envelopes

    def ping(self) -> bool:
        """Return whether current connection appears healthy."""

        return bool(self._connection and self._connection.is_open and self._channel and self._channel.is_open)

    def _ensure_connected(self) -> None:
        """Ensure a channel is available before broker operations."""

        if self._connection is None or self._channel is None or not self._connection.is_open:
            self.connect()
