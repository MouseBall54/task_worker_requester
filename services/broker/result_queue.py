"""Helpers for resolving local IPv4 and final result queue names."""

from __future__ import annotations

from ipaddress import ip_address
import socket

from config.models import RabbitMQConfig


def resolve_local_ipv4(config: RabbitMQConfig) -> str:
    """Resolve the representative local IPv4 for this client machine.

    Preference order:
    1. The local IPv4 selected by the OS when routing toward RabbitMQ host/port.
    2. The first non-loopback IPv4 found from local hostname lookup.
    """

    routed_ipv4 = _resolve_routed_ipv4(config.host, config.port)
    if routed_ipv4:
        return routed_ipv4

    fallback_ipv4 = _resolve_first_non_loopback_ipv4()
    if fallback_ipv4:
        return fallback_ipv4

    raise RuntimeError(
        "실행 PC의 usable IPv4 주소를 찾지 못했습니다. 네트워크 연결 상태를 확인해주세요."
    )


def resolve_result_queue_name(result_queue_base: str, local_ipv4: str) -> str:
    """Return the final result queue name using base queue prefix and IPv4 suffix."""

    base = str(result_queue_base or "").strip()
    ipv4 = str(local_ipv4 or "").strip()
    if not base:
        raise RuntimeError("result_queue_base 가 비어 있어 결과 queue 이름을 만들 수 없습니다.")
    if not _is_valid_ipv4(ipv4):
        raise RuntimeError(f"유효한 IPv4 형식이 아닙니다: {ipv4}")
    return f"{base}_{ipv4}"


def _resolve_routed_ipv4(host: str, port: int) -> str | None:
    """Ask the OS which local IPv4 would be used for outbound RabbitMQ traffic."""

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect((host, int(port)))
            candidate = sock.getsockname()[0]
    except OSError:
        return None

    return candidate if _is_usable_ipv4(candidate) else None


def _resolve_first_non_loopback_ipv4() -> str | None:
    """Return the first non-loopback IPv4 discovered from hostname lookup."""

    host_candidates = [socket.gethostname(), socket.getfqdn()]
    seen: set[str] = set()
    for host in host_candidates:
        normalized = str(host or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)

        try:
            _, _, addresses = socket.gethostbyname_ex(normalized)
        except OSError:
            continue

        for candidate in addresses:
            if _is_usable_ipv4(candidate):
                return candidate

    return None


def _is_usable_ipv4(value: str) -> bool:
    """Return whether the string is a non-loopback IPv4 address."""

    try:
        parsed = ip_address(value)
    except ValueError:
        return False

    return parsed.version == 4 and not parsed.is_loopback


def _is_valid_ipv4(value: str) -> bool:
    """Return whether the string is a syntactically valid IPv4 address."""

    try:
        parsed = ip_address(value)
    except ValueError:
        return False

    return parsed.version == 4
