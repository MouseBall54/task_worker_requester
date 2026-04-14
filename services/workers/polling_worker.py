"""Worker that polls result queue at configurable interval."""

from __future__ import annotations

import time
from collections.abc import Callable

from PySide6.QtCore import QObject, Signal, Slot

from services.broker.base import AbstractBrokerClient, BrokerResultEnvelope


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
    ) -> None:
        super().__init__()
        self._broker_provider = broker_provider
        self._queue_name = queue_name
        self._polling_interval_seconds = polling_interval_seconds
        self._max_messages_per_poll = max_messages_per_poll
        self._stop_requested = False

    @Slot()
    def run(self) -> None:
        """Poll queue repeatedly and emit every received result envelope."""

        broker = self._broker_provider()
        try:
            broker.connect()
            self.log.emit(f"결과 polling 시작: {self._queue_name}")
            while not self._stop_requested:
                try:
                    envelopes = broker.poll_results(
                        queue_name=self._queue_name,
                        max_messages=self._max_messages_per_poll,
                    )
                    for envelope in envelopes:
                        self.result_received.emit(envelope)
                    self.poll_cycle.emit(len(envelopes))
                except Exception as exc:  # pylint: disable=broad-except
                    self.log.emit(f"polling 중 오류: {exc}")

                sleep_left = float(self._polling_interval_seconds)
                while sleep_left > 0 and not self._stop_requested:
                    step = min(0.2, sleep_left)
                    time.sleep(step)
                    sleep_left -= step

            self.log.emit("결과 polling 워커가 종료되었습니다.")
        except Exception as exc:  # pylint: disable=broad-except
            self.log.emit(f"결과 polling 워커 초기화 실패: {exc}")
        finally:
            try:
                broker.close()
            except Exception:  # pragma: no cover
                pass
            self.finished.emit()

    @Slot()
    def stop(self) -> None:
        """Request graceful stop for polling loop."""

        self._stop_requested = True
