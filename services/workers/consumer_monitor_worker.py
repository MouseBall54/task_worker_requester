"""Poll RabbitMQ Management API for request queue consumers by peer IP."""

from __future__ import annotations

import base64
import ipaddress
import json
import threading
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from PySide6.QtCore import QObject, Signal, Slot

from config.models import RabbitMQConfig
from config.worker_node_settings import default_management_api_url, load_worker_node_settings


class ConsumerMonitorWorker(QObject):
    """Poll consumer IP counts off the GUI thread until stopped."""

    snapshot_ready = Signal(object)
    finished = Signal()

    def __init__(
        self,
        settings_path: str,
        rabbitmq: RabbitMQConfig,
        request_queue: str,
        interval_seconds: int = 5,
    ) -> None:
        super().__init__()
        self._settings_path = settings_path
        self._rabbitmq = rabbitmq
        self._request_queue = request_queue
        self._interval_seconds = max(1, int(interval_seconds))
        self._stop_event = threading.Event()
        self._refresh_event = threading.Event()

    @Slot()
    def run(self) -> None:
        try:
            while not self._stop_event.is_set():
                self._refresh_event.clear()
                try:
                    settings = load_worker_node_settings(self._settings_path)
                    counts, unregistered = self._fetch_counts(
                        settings.management_api_url
                        or default_management_api_url(self._rabbitmq.host),
                        {node.address for node in settings.nodes},
                    )
                    self.snapshot_ready.emit(
                        {"counts": counts, "unregistered": unregistered, "error": ""}
                    )
                except Exception as exc:  # pragma: no cover - network-dependent
                    self.snapshot_ready.emit(
                        {"counts": {}, "unregistered": {}, "error": _safe_monitor_error(exc)}
                    )
                self._refresh_event.wait(self._interval_seconds)
        finally:
            self.finished.emit()

    @Slot()
    def stop(self) -> None:
        self._stop_event.set()
        self._refresh_event.set()

    @Slot()
    def request_refresh(self) -> None:
        self._refresh_event.set()

    def _fetch_counts(
        self,
        management_api_url: str,
        registered_addresses: set[str],
    ) -> tuple[dict[str, int], dict[str, int]]:
        vhost = quote(self._rabbitmq.virtual_host, safe="")
        queue = quote(self._request_queue, safe="")
        endpoint = f"{management_api_url}/api/queues/{vhost}/{queue}"
        token = base64.b64encode(
            f"{self._rabbitmq.username}:{self._rabbitmq.password}".encode("utf-8")
        ).decode("ascii")
        request = Request(
            endpoint,
            headers={"Accept": "application/json", "Authorization": f"Basic {token}"},
        )
        try:
            with urlopen(request, timeout=4) as response:
                queue_details = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise RuntimeError(f"Management API HTTP {exc.code}") from exc
        except URLError as exc:
            raise RuntimeError("Management API에 연결할 수 없습니다") from exc
        if not isinstance(queue_details, dict):
            raise RuntimeError("Management API가 예상한 queue 정보를 반환하지 않았습니다")
        consumers = queue_details.get("consumer_details", [])
        if not isinstance(consumers, list):
            raise RuntimeError("Management API가 예상한 consumer 목록을 반환하지 않았습니다")

        registered = {address: 0 for address in registered_addresses}
        unregistered: dict[str, int] = {}
        for consumer in consumers:
            if not isinstance(consumer, dict):
                continue
            channel = consumer.get("channel_details") or {}
            peer_host = str(channel.get("peer_host", "")).strip()
            if not peer_host:
                unregistered["알 수 없는 IP"] = unregistered.get("알 수 없는 IP", 0) + 1
                continue
            normalized_host = _normalize_peer_host(peer_host)
            if normalized_host in registered:
                registered[normalized_host] += 1
            else:
                unregistered[normalized_host] = unregistered.get(normalized_host, 0) + 1
        return registered, unregistered


def _normalize_peer_host(value: str) -> str:
    normalized = value.strip().strip("[]")
    try:
        return str(ipaddress.ip_address(normalized))
    except ValueError:
        return normalized


def _safe_monitor_error(exc: Exception) -> str:
    message = str(exc).strip()
    return message or "Consumer 현황을 조회하지 못했습니다"
