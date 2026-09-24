"""Load and save the editable worker node registry."""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import os
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import yaml


class WorkerNodeSettingsError(RuntimeError):
    """Raised when the worker node settings are invalid or unavailable."""


@dataclass(frozen=True, slots=True)
class WorkerNode:
    address: str
    name: str


@dataclass(frozen=True, slots=True)
class WorkerNodeSettings:
    management_api_url: str
    nodes: tuple[WorkerNode, ...]


def default_management_api_url(host: str) -> str:
    normalized_host = host.strip()
    if ":" in normalized_host and not normalized_host.startswith("["):
        normalized_host = f"[{normalized_host}]"
    return f"http://{normalized_host}:15672"


def load_worker_node_settings(path: str | Path) -> WorkerNodeSettings:
    settings_path = Path(path)
    try:
        raw = yaml.safe_load(settings_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise WorkerNodeSettingsError(f"worker_nodes.yaml을 읽지 못했습니다: {exc}") from exc
    if not isinstance(raw, dict):
        raise WorkerNodeSettingsError("worker_nodes.yaml의 최상위 값은 key-value 형식이어야 합니다.")

    management_api_url = str(raw.get("management_api_url", "")).strip().rstrip("/")
    parsed_url = urlparse(management_api_url) if management_api_url else None
    if parsed_url is not None and (parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc):
        raise WorkerNodeSettingsError("management_api_url은 http:// 또는 https:// 주소여야 합니다.")

    raw_nodes = raw.get("nodes", [])
    if not isinstance(raw_nodes, list):
        raise WorkerNodeSettingsError("nodes는 목록 형식이어야 합니다.")
    nodes = _normalize_nodes(raw_nodes)
    return WorkerNodeSettings(management_api_url, tuple(nodes))


def save_worker_node_settings(
    path: str | Path,
    management_api_url: str,
    nodes: list[WorkerNode],
) -> WorkerNodeSettings:
    normalized_url = str(management_api_url).strip().rstrip("/")
    parsed_url = urlparse(normalized_url) if normalized_url else None
    if parsed_url is not None and (parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc):
        raise WorkerNodeSettingsError("Management API 주소는 http:// 또는 https://로 시작해야 합니다.")
    normalized_nodes = _normalize_nodes(
        [{"ip": node.address, "name": node.name} for node in nodes]
    )

    settings_path = Path(path)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = settings_path.with_name(f".{settings_path.name}.{uuid4().hex}.tmp")
    payload = {
        "management_api_url": normalized_url,
        "nodes": [{"ip": node.address, "name": node.name} for node in normalized_nodes],
    }
    try:
        temp_path.write_text(
            yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        os.replace(temp_path, settings_path)
    except OSError as exc:
        raise WorkerNodeSettingsError(f"worker_nodes.yaml을 저장하지 못했습니다: {exc}") from exc
    finally:
        temp_path.unlink(missing_ok=True)
    return WorkerNodeSettings(normalized_url, tuple(normalized_nodes))


def _normalize_nodes(raw_nodes: list[object]) -> list[WorkerNode]:
    nodes: list[WorkerNode] = []
    seen_addresses: set[str] = set()
    for index, raw_node in enumerate(raw_nodes, start=1):
        if isinstance(raw_node, WorkerNode):
            address_value, name_value = raw_node.address, raw_node.name
        elif isinstance(raw_node, dict):
            address_value, name_value = raw_node.get("ip", ""), raw_node.get("name", "")
        else:
            raise WorkerNodeSettingsError(f"nodes {index}번 항목은 ip/name을 가진 객체여야 합니다.")

        try:
            address = str(ipaddress.ip_address(str(address_value).strip()))
        except ValueError as exc:
            raise WorkerNodeSettingsError(f"nodes {index}번 항목의 IP 주소가 올바르지 않습니다.") from exc
        name = str(name_value).strip()
        if not name:
            raise WorkerNodeSettingsError(f"{address}의 명칭을 입력해주세요.")
        if address in seen_addresses:
            raise WorkerNodeSettingsError(f"IP 주소가 중복 등록되었습니다: {address}")
        seen_addresses.add(address)
        nodes.append(WorkerNode(address, name))
    return nodes
