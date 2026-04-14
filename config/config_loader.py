"""Configuration loading and validation logic."""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path
from typing import Any, Mapping, TypeVar

import yaml

from config.models import AppConfig, PublishConfig, RabbitMQConfig, RecipePreset, UiConfig


class ConfigError(RuntimeError):
    """Raised when config file is missing or invalid."""


T = TypeVar("T")


def _build_dataclass(cls: type[T], values: Mapping[str, Any]) -> T:
    """Create a dataclass instance by filtering unknown keys.

    Parameters
    ----------
    cls:
        Dataclass type to instantiate.
    values:
        Source mapping loaded from config.
    """

    allowed = {field.name for field in fields(cls)}
    kwargs = {name: values[name] for name in values if name in allowed}
    return cls(**kwargs)  # type: ignore[arg-type]


class ConfigLoader:
    """Loads and validates application configuration."""

    @staticmethod
    def load(config_path: str | Path) -> AppConfig:
        """Load configuration from a YAML file.

        Raises
        ------
        ConfigError
            If the file cannot be parsed or required keys are missing.
        """

        path = Path(config_path)
        if not path.exists():
            raise ConfigError(f"설정 파일을 찾을 수 없습니다: {path}")

        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"설정 파일 파싱 오류: {exc}") from exc

        if not isinstance(raw, dict):
            raise ConfigError("설정 파일 최상위 구조는 key-value 형식이어야 합니다.")

        rabbitmq_raw = raw.get("rabbitmq")
        if not isinstance(rabbitmq_raw, dict):
            raise ConfigError("rabbitmq 설정이 누락되었거나 형식이 올바르지 않습니다.")

        required_mq = ["host", "port", "username", "password"]
        missing = [key for key in required_mq if key not in rabbitmq_raw]
        if missing:
            missing_joined = ", ".join(missing)
            raise ConfigError(f"rabbitmq 필수 항목 누락: {missing_joined}")

        publish_raw = raw.get("publish", {})
        ui_raw = raw.get("ui", {})

        if not isinstance(publish_raw, dict) or not isinstance(ui_raw, dict):
            raise ConfigError("publish/ui 설정은 key-value 형식이어야 합니다.")

        rabbitmq = _build_dataclass(RabbitMQConfig, rabbitmq_raw)
        publish_for_parse = dict(publish_raw)
        publish_for_parse["recipe_presets"] = ConfigLoader._parse_recipe_presets(
            publish_raw=publish_raw,
            fallback_default_path=str(publish_raw.get("default_recipe_path", PublishConfig.default_recipe_path)),
        )
        publish = _build_dataclass(PublishConfig, publish_for_parse)
        ui = _build_dataclass(UiConfig, ui_raw)

        config = AppConfig(
            rabbitmq=rabbitmq,
            publish=publish,
            ui=ui,
            mock_mode=bool(raw.get("mock_mode", False)),
            log_level=str(raw.get("log_level", "INFO")),
        )

        ConfigLoader._validate(config)
        return config

    @staticmethod
    def _validate(config: AppConfig) -> None:
        """Perform semantic checks after dataclass parsing."""

        if config.publish.polling_interval_seconds <= 0:
            raise ConfigError("polling_interval_seconds 는 1 이상이어야 합니다.")
        if config.publish.timeout_seconds <= 0:
            raise ConfigError("timeout_seconds 는 1 이상이어야 합니다.")
        if config.publish.max_messages_per_poll <= 0:
            raise ConfigError("max_messages_per_poll 는 1 이상이어야 합니다.")
        if config.publish.max_publish_retries <= 0:
            raise ConfigError("max_publish_retries 는 1 이상이어야 합니다.")

        normalized_ext = []
        for ext in config.publish.image_extensions:
            normalized = ext.lower().strip()
            if not normalized.startswith("."):
                normalized = f".{normalized}"
            normalized_ext.append(normalized)
        config.publish.image_extensions = sorted(set(normalized_ext))

        if config.publish.scan_mode not in {"direct", "recursive"}:
            raise ConfigError("scan_mode 는 direct 또는 recursive 여야 합니다.")

        presets = config.publish.recipe_presets
        if not presets:
            raise ConfigError("recipe_presets 가 비어 있습니다. 최소 1개 이상의 레시피를 설정해주세요.")

        alias_set: set[str] = set()
        for preset in presets:
            alias = preset.alias.strip()
            recipe_path = preset.path.strip()
            if not alias:
                raise ConfigError("recipe_presets.alias 는 빈 값일 수 없습니다.")
            if not recipe_path:
                raise ConfigError("recipe_presets.path 는 빈 값일 수 없습니다.")
            key = alias.lower()
            if key in alias_set:
                raise ConfigError(f"recipe_presets alias 중복: {alias}")
            alias_set.add(key)
            preset.alias = alias
            preset.path = recipe_path

        if config.publish.default_recipe_alias:
            selected_alias = config.publish.default_recipe_alias.strip()
            if selected_alias.lower() not in alias_set:
                raise ConfigError(
                    f"default_recipe_alias '{selected_alias}' 는 recipe_presets 에 정의되어야 합니다."
                )
            config.publish.default_recipe_alias = selected_alias
        else:
            config.publish.default_recipe_alias = presets[0].alias

        # Keep backward-compatible path field aligned with selected default alias.
        selected_preset = next(
            preset
            for preset in presets
            if preset.alias.lower() == config.publish.default_recipe_alias.lower()
        )
        config.publish.default_recipe_path = selected_preset.path

    @staticmethod
    def _parse_recipe_presets(publish_raw: Mapping[str, Any], fallback_default_path: str) -> list[RecipePreset]:
        """Parse recipe presets from config with backward compatibility."""

        raw_presets = publish_raw.get("recipe_presets")
        presets: list[RecipePreset] = []

        if isinstance(raw_presets, list):
            for idx, item in enumerate(raw_presets, start=1):
                if not isinstance(item, Mapping):
                    raise ConfigError(f"recipe_presets[{idx}] 항목은 key-value 형식이어야 합니다.")
                alias = str(item.get("alias", "")).strip()
                path = str(item.get("path", "")).strip()
                if not alias or not path:
                    raise ConfigError(
                        f"recipe_presets[{idx}] 항목은 alias/path 모두 필요합니다."
                    )
                presets.append(RecipePreset(alias=alias, path=path))

        if not presets:
            default_path = str(publish_raw.get("default_recipe_path", fallback_default_path)).strip()
            if not default_path:
                default_path = PublishConfig.default_recipe_path
            presets.append(RecipePreset(alias="Default Recipe", path=default_path))

        return presets
