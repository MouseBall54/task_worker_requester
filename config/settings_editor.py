"""Validated, atomic updates for user-editable runtime YAML settings."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping, Sequence
from uuid import uuid4

import yaml

from config.config_loader import ConfigError, ConfigLoader


class SettingsEditorError(RuntimeError):
    """Raised when runtime settings cannot be loaded, validated, or saved."""


def update_app_config(
    config_path: str | Path,
    rabbitmq_values: Mapping[str, Any],
    publish_values: Mapping[str, Any],
) -> None:
    """Update supported app settings while preserving unrelated YAML sections."""

    path = Path(config_path)
    raw = _load_mapping(path, "app_config.yaml")
    rabbitmq = raw.get("rabbitmq")
    publish = raw.get("publish")
    if not isinstance(rabbitmq, dict) or not isinstance(publish, dict):
        raise SettingsEditorError("rabbitmq 또는 publish 설정 구조가 올바르지 않습니다.")
    rabbitmq.update(dict(rabbitmq_values))
    publish.update(dict(publish_values))
    _require_non_empty(
        rabbitmq,
        ("host", "username", "virtual_host", "request_routing_key", "request_queue", "result_queue_base"),
        "rabbitmq",
    )
    _require_non_empty(publish, ("default_action",), "publish")
    temp_path = _write_temp_yaml(path, raw)
    try:
        ConfigLoader.load(temp_path)
        os.replace(temp_path, path)
    except (ConfigError, OSError, yaml.YAMLError) as exc:
        raise SettingsEditorError(str(exc)) from exc
    finally:
        temp_path.unlink(missing_ok=True)


def update_recipe_config(
    app_config_path: str | Path,
    recipe_config_path: str | Path,
    default_alias: str,
    recipes: Sequence[tuple[str, str]],
) -> None:
    """Update recipe aliases/paths and validate them through the full app loader."""

    app_path = Path(app_config_path)
    recipe_path = Path(recipe_config_path)
    recipe_raw = _load_mapping(recipe_path, "recipe_config.yaml")
    recipe_raw["default_alias"] = str(default_alias).strip()
    recipe_raw["recipes"] = [
        {"alias": str(alias).strip(), "path": str(path).strip()}
        for alias, path in recipes
    ]
    temp_recipe_path = _write_temp_yaml(recipe_path, recipe_raw)
    temp_app_path: Path | None = None
    try:
        app_raw = _load_mapping(app_path, "app_config.yaml")
        app_raw["recipe_config_path"] = str(temp_recipe_path.resolve())
        temp_app_path = _write_temp_yaml(app_path, app_raw)
        ConfigLoader.load(temp_app_path)
        os.replace(temp_recipe_path, recipe_path)
    except (ConfigError, OSError, yaml.YAMLError) as exc:
        raise SettingsEditorError(str(exc)) from exc
    finally:
        temp_recipe_path.unlink(missing_ok=True)
        if temp_app_path is not None:
            temp_app_path.unlink(missing_ok=True)


def _load_mapping(path: Path, label: str) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise SettingsEditorError(f"{label}을 읽지 못했습니다: {exc}") from exc
    if not isinstance(raw, dict):
        raise SettingsEditorError(f"{label} 최상위 구조는 key-value 형식이어야 합니다.")
    return dict(raw)


def _write_temp_yaml(target_path: Path, raw: Mapping[str, Any]) -> Path:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target_path.with_name(f".{target_path.name}.{uuid4().hex}.tmp")
    try:
        temp_path.write_text(
            yaml.safe_dump(dict(raw), allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
    except OSError as exc:
        raise SettingsEditorError(f"설정 임시 파일을 쓰지 못했습니다: {exc}") from exc
    return temp_path


def _require_non_empty(values: Mapping[str, Any], keys: Sequence[str], section: str) -> None:
    missing = [key for key in keys if not str(values.get(key, "")).strip()]
    if missing:
        raise SettingsEditorError(
            f"{section} 필수 값이 비어 있습니다: {', '.join(missing)}"
        )
