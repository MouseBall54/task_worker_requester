"""RabbitMQ client implementation based on pika blocking connection."""

from __future__ import annotations

import json
import time
from typing import Any

from config.models import RabbitMQConfig
from models.task_models import TaskMessage
from services.broker.base import (
    AbstractBrokerClient,
    BrokerConsumeCallback,
    BrokerConsumeDecision,
    BrokerResultEnvelope,
)
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
        self._consumer_tag: str | None = None
        self._consumer_callback: BrokerConsumeCallback | None = None
        self._delivered_since_pump = 0
        self._cancel_requested_after_delivery = False

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
        self._channel.queue_declare(
            queue=self._config.request_queue,
            **self._build_queue_declare_kwargs(self._config.request_queue_declare),
        )

    def close(self) -> None:
        """Close channel and connection safely."""

        self.stop_result_consumer()
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
        self._channel.queue_declare(
            queue=queue_name,
            **self._build_queue_declare_kwargs(self._config.result_queue_declare),
        )
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
            reply_to=task_message.QUEUE_NAME,
            content_type="application/json",
            priority=max(0, int(task_message.priority)),
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

    def start_result_consumer(
        self,
        queue_name: str,
        on_envelope: BrokerConsumeCallback,
        prefetch_count: int,
    ) -> None:
        """Register a long-lived consumer so RabbitMQ shows active consumers."""

        self._ensure_connected()
        assert self._channel is not None
        if self._consumer_tag is not None:
            self.stop_result_consumer()

        self._channel.basic_qos(prefetch_count=max(1, int(prefetch_count)))
        self._consumer_callback = on_envelope
        self._delivered_since_pump = 0
        self._cancel_requested_after_delivery = False

        def _handle_message(channel, method_frame, properties, body: bytes) -> None:  # type: ignore[no-untyped-def]
            payload = self._decode_payload(body)
            envelope = BrokerResultEnvelope(
                payload=payload,
                message_id=getattr(properties, "message_id", None),
                correlation_id=getattr(properties, "correlation_id", None),
            )
            decision = BrokerConsumeDecision.ACK
            if self._consumer_callback is not None:
                try:
                    callback_result = self._consumer_callback(envelope)
                except Exception:
                    callback_result = BrokerConsumeDecision.REQUEUE_AND_PAUSE
                if isinstance(callback_result, BrokerConsumeDecision):
                    decision = callback_result

            if decision == BrokerConsumeDecision.ACK:
                channel.basic_ack(delivery_tag=method_frame.delivery_tag)
                self._delivered_since_pump += 1
                return

            channel.basic_nack(delivery_tag=method_frame.delivery_tag, requeue=True)
            self._delivered_since_pump += 1
            if decision == BrokerConsumeDecision.REQUEUE_AND_PAUSE:
                self._cancel_requested_after_delivery = True

        self._consumer_tag = self._channel.basic_consume(
            queue=queue_name,
            on_message_callback=_handle_message,
            auto_ack=False,
        )

    def pump_events(self, time_limit_seconds: float) -> int:
        """Pump pika events and return how many envelopes were delivered."""

        self._ensure_connected()
        assert self._connection is not None

        self._delivered_since_pump = 0
        self._connection.process_data_events(time_limit=max(0.0, float(time_limit_seconds)))
        if self._cancel_requested_after_delivery and self._consumer_tag is not None:
            self.stop_result_consumer()
        delivered = self._delivered_since_pump
        self._delivered_since_pump = 0
        return delivered

    def stop_result_consumer(self) -> None:
        """Cancel registered result consumer if it exists."""

        if self._consumer_tag is None:
            return

        if self._channel and self._channel.is_open:
            try:
                self._channel.basic_cancel(self._consumer_tag)
            except Exception:  # pragma: no cover - best effort cleanup
                pass

        self._consumer_tag = None
        self._consumer_callback = None
        self._delivered_since_pump = 0
        self._cancel_requested_after_delivery = False

    def ping(self) -> bool:
        """Return whether current connection appears healthy."""

        return bool(self._connection and self._connection.is_open and self._channel and self._channel.is_open)

    def _ensure_connected(self) -> None:
        """Ensure a channel is available before broker operations."""

        if self._connection is None or self._channel is None or not self._connection.is_open:
            self.connect()

    @staticmethod
    def _build_queue_declare_kwargs(queue_config) -> dict[str, Any]:  # noqa: ANN001
        """Translate queue config dataclass into pika queue_declare kwargs."""

        kwargs: dict[str, Any] = {
            "durable": bool(queue_config.durable),
            "exclusive": bool(queue_config.exclusive),
            "auto_delete": bool(queue_config.auto_delete),
        }
        if queue_config.arguments:
            kwargs["arguments"] = dict(queue_config.arguments)
        return kwargs

    @staticmethod
    def _decode_payload(body: bytes) -> dict[str, Any]:
        """Decode one broker body into a normalized dict payload."""

        try:
            decoded = body.decode("utf-8")
            loaded = json.loads(decoded)
            return loaded if isinstance(loaded, dict) else {"raw": loaded}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return {"error": f"결과 메시지 파싱 실패: {exc}", "raw": str(body)}
