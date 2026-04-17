"""Worker that polls result queue at configurable interval."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from PySide6.QtCore import QObject, Signal, Slot

from services.broker.base import (
    AbstractBrokerClient,
    BrokerConsumeDecision,
    BrokerResultEnvelope,
)
from services.result_parser import extract_request_id


class PollingWorker(QObject):
    """Periodic result polling worker executed in dedicated QThread."""

    result_received = Signal(object)
    poll_cycle = Signal(int)
    log = Signal(str)
    finished = Signal()

    def __init__(
        self,
        broker_provider: Callable[[], AbstractBrokerClient],
        queue_name: str,
        polling_interval_seconds: int,
        max_messages_per_poll: int,
        tracked_request_ids: set[str] | None = None,
    ) -> None:
        super().__init__()
        self._broker_provider = broker_provider
        self._queue_name = queue_name
        self._polling_interval_seconds = polling_interval_seconds
        self._max_messages_per_poll = max_messages_per_poll
        self._stop_requested = False
        self._tracked_request_ids = set(tracked_request_ids or set())
        self._tracked_ids_lock = threading.Lock()

    @Slot()
    def run(self) -> None:
        """Keep a consumer active while emitting periodic state-management ticks."""

        broker = self._broker_provider()
        try:
            broker.connect()
            broker.declare_result_queue(self._queue_name)
            self.log.emit(f"결과 consumer 등록 시작: {self._queue_name}")

            received_since_tick = 0
            interval_seconds = max(0.1, float(self._polling_interval_seconds))

            def _on_envelope(envelope: BrokerResultEnvelope) -> BrokerConsumeDecision:
                nonlocal received_since_tick
                matched_request_id = extract_request_id(
                    payload=envelope.payload,
                    correlation_id=envelope.correlation_id,
                    message_id=envelope.message_id,
                )
                if not matched_request_id:
                    self.log.emit("request_id mismatch - consumed and ignored: <missing>")
                    return BrokerConsumeDecision.ACK

                with self._tracked_ids_lock:
                    is_tracked = matched_request_id in self._tracked_request_ids

                if not is_tracked:
                    self.log.emit(
                        f"request_id mismatch - consumed and ignored: {matched_request_id}"
                    )
                    return BrokerConsumeDecision.ACK

                received_since_tick += 1
                self.result_received.emit(envelope)
                return BrokerConsumeDecision.ACK

            broker.start_result_consumer(
                queue_name=self._queue_name,
                on_envelope=_on_envelope,
                prefetch_count=self._max_messages_per_poll,
            )
            self.log.emit(f"결과 consumer active: {self._queue_name}")

            next_tick_at = time.monotonic() + interval_seconds
            while not self._stop_requested:
                try:
                    broker.pump_events(time_limit_seconds=0.2)
                except Exception as exc:  # pylint: disable=broad-except
                    self.log.emit(f"consumer 이벤트 처리 중 오류: {exc}")

                now = time.monotonic()
                if now < next_tick_at:
                    continue

                self.poll_cycle.emit(received_since_tick)
                self.log.emit(f"tick 수행 - 수신 {received_since_tick}건")
                received_since_tick = 0
                next_tick_at = now + interval_seconds

            self.log.emit("결과 consumer 워커가 종료되었습니다.")
        except Exception as exc:  # pylint: disable=broad-except
            self.log.emit(f"결과 consumer 워커 초기화 실패: {exc}")
        finally:
            try:
                broker.stop_result_consumer()
                self.log.emit("consumer stop/cancel")
            except Exception:  # pragma: no cover
                pass
            try:
                broker.close()
            except Exception:  # pragma: no cover
                pass
            self.finished.emit()

    @Slot()
    def stop(self) -> None:
        """Request graceful stop for polling loop."""

        self._stop_requested = True

    def add_tracked_request_ids(self, request_ids: list[str] | set[str]) -> None:
        """Add request IDs that should be considered safe to consume."""

        with self._tracked_ids_lock:
            self._tracked_request_ids.update(str(request_id) for request_id in request_ids if request_id)

    def remove_tracked_request_ids(self, request_ids: list[str] | set[str]) -> None:
        """Remove request IDs from tracked consume targets."""

        with self._tracked_ids_lock:
            for request_id in request_ids:
                normalized = str(request_id).strip()
                if not normalized:
                    continue
                self._tracked_request_ids.discard(normalized)
