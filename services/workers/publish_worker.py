"""Worker that publishes queued task messages in a background thread."""

from __future__ import annotations

import time
from collections.abc import Callable

from PySide6.QtCore import QObject, Signal, Slot

from models.task_models import TaskMessage
from services.broker.base import AbstractBrokerClient


class PublishWorker(QObject):
    """Publish one-message-per-image requests without blocking GUI."""

    queue_ready = Signal(str)
    message_published = Signal(str, int, int, object, object)
    message_failed = Signal(str, str)
    log = Signal(str)
    finished = Signal()

    def __init__(
        self,
        broker_provider: Callable[[], AbstractBrokerClient],
        messages: list[TaskMessage],
        result_queue_base: str,
        publish_exchange: str,
        publish_routing_key: str,
        max_retries: int,
        retry_backoff_seconds: float,
    ) -> None:
        super().__init__()
        self._broker_provider = broker_provider
        self._messages = messages
        self._result_queue_base = result_queue_base
        self._publish_exchange = publish_exchange
        self._publish_routing_key = publish_routing_key
        self._max_retries = max_retries
        self._retry_backoff_seconds = retry_backoff_seconds
        self._stop_requested = False

    @Slot()
    def run(self) -> None:
        """Execute publishing lifecycle in worker thread."""

        broker = self._broker_provider()
        total = len(self._messages)

        try:
            broker.connect()
            queue_name = broker.declare_result_queue(self._result_queue_base)
            self.queue_ready.emit(queue_name)
            self.log.emit(f"브로커 연결 성공, 결과 큐 준비: {queue_name}")

            for index, message in enumerate(self._messages, start=1):
                if self._stop_requested:
                    self.log.emit("전송 중지 요청으로 게시를 종료합니다.")
                    break

                published = False
                last_error = ""
                for attempt in range(1, self._max_retries + 1):
                    try:
                        broker.publish_task(message)
                        published_payload = message.to_dict()
                        publish_meta = {
                            "exchange": self._publish_exchange,
                            "routing_key": self._publish_routing_key,
                            "reply_to": message.QUEUE_NAME,
                            "message_id": message.request_id,
                            "correlation_id": message.request_id,
                            "content_type": "application/json",
                        }
                        self.message_published.emit(
                            message.request_id,
                            index,
                            total,
                            published_payload,
                            publish_meta,
                        )
                        published = True
                        break
                    except Exception as exc:  # pylint: disable=broad-except
                        last_error = str(exc)
                        self.log.emit(
                            f"전송 재시도 {attempt}/{self._max_retries} 실패 - {message.request_id}: {last_error}"
                        )
                        if attempt < self._max_retries:
                            time.sleep(self._retry_backoff_seconds * attempt)

                if not published:
                    self.message_failed.emit(message.request_id, last_error or "전송 실패")

            self.log.emit("메시지 전송 워커가 종료되었습니다.")
        except Exception as exc:  # pylint: disable=broad-except
            self.log.emit(f"메시지 전송 워커 오류: {exc}")
        finally:
            try:
                broker.close()
            except Exception:  # pragma: no cover
                pass
            self.finished.emit()

    @Slot()
    def stop(self) -> None:
        """Request graceful stop at next safe boundary."""

        self._stop_requested = True
