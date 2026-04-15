"""Configuration loading and validation logic."""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path
from typing import Any, Mapping, TypeVar

import yaml

from config.models import (
    AppConfig,
    PublishConfig,
    QueueDeclareConfig,
    RabbitMQConfig,
    RecipeConfig,
    RecipeItem,
    UiConfig,
    default_recipe_items,
    default_request_queue_declare,
    default_result_queue_declare,
)


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
        raw = ConfigLoader._load_yaml_mapping(path, label="메인 설정 파일")

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

        ConfigLoader._ensure_legacy_recipe_keys_removed(publish_raw)
        ConfigLoader._ensure_inline_recipe_config_removed(raw)

        recipe_config_path = ConfigLoader._resolve_recipe_config_path(
            main_config_path=path,
            raw_recipe_config_path=raw.get("recipe_config_path"),
        )
        recipe_raw = ConfigLoader._load_yaml_mapping(recipe_config_path, label="recipe 설정 파일")

        rabbitmq_for_parse = dict(rabbitmq_raw)
        rabbitmq_for_parse["request_queue_declare"] = ConfigLoader._parse_queue_declare_config(
            raw_config=rabbitmq_raw.get("request_queue_declare"),
            default_config=default_request_queue_declare(),
            label="rabbitmq.request_queue_declare",
        )
        rabbitmq_for_parse["result_queue_declare"] = ConfigLoader._parse_queue_declare_config(
            raw_config=rabbitmq_raw.get("result_queue_declare"),
            default_config=default_result_queue_declare(),
            label="rabbitmq.result_queue_declare",
        )

        publish_for_parse = dict(publish_raw)

        rabbitmq = _build_dataclass(RabbitMQConfig, rabbitmq_for_parse)
        publish = _build_dataclass(PublishConfig, publish_for_parse)
        recipe_config = ConfigLoader._parse_recipe_config(recipe_raw)
        ui = _build_dataclass(UiConfig, ui_raw)

        config = AppConfig(
            rabbitmq=rabbitmq,
            recipe_config_path=str(recipe_config_path),
            recipe_config=recipe_config,
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
        if config.publish.initial_open_folders <= 0:
            raise ConfigError("initial_open_folders 는 1 이상이어야 합니다.")
        if config.publish.max_active_open_folders <= 0:
            raise ConfigError("max_active_open_folders 는 1 이상이어야 합니다.")
        if config.publish.initial_open_folders > config.publish.max_active_open_folders:
            raise ConfigError("initial_open_folders 는 max_active_open_folders 보다 클 수 없습니다.")

        normalized_ext = []
        for ext in config.publish.image_extensions:
            normalized = ext.lower().strip()
            if not normalized.startswith("."):
                normalized = f".{normalized}"
            normalized_ext.append(normalized)
        config.publish.image_extensions = sorted(set(normalized_ext))

        if config.publish.scan_mode not in {"direct", "recursive"}:
            raise ConfigError("scan_mode 는 direct 또는 recursive 여야 합니다.")

        recipes = config.recipe_config.recipes
        if not recipes:
            raise ConfigError("recipe_config.recipes 가 비어 있습니다. 최소 1개 이상의 레시피를 설정해주세요.")

        alias_set: set[str] = set()
        for recipe in recipes:
            alias = recipe.alias.strip()
            recipe_path = recipe.path.strip()
            if not alias:
                raise ConfigError("recipe_config.recipes.alias 는 빈 값일 수 없습니다.")
            if not recipe_path:
                raise ConfigError("recipe_config.recipes.path 는 빈 값일 수 없습니다.")
            key = alias.lower()
            if key in alias_set:
                raise ConfigError(f"recipe_config.recipes alias 중복: {alias}")
            alias_set.add(key)
            recipe.alias = alias
            recipe.path = recipe_path

        if config.recipe_config.default_alias:
            selected_alias = config.recipe_config.default_alias.strip()
            if selected_alias.lower() not in alias_set:
                raise ConfigError(
                    f"recipe_config.default_alias '{selected_alias}' 는 recipe_config.recipes 에 정의되어야 합니다."
                )
            config.recipe_config.default_alias = selected_alias
        else:
            config.recipe_config.default_alias = recipes[0].alias

    @staticmethod
    def _ensure_legacy_recipe_keys_removed(publish_raw: Mapping[str, Any]) -> None:
        """Reject legacy publish-level recipe keys to avoid silent misconfiguration."""

        legacy_keys = {"recipe_presets", "default_recipe_alias", "default_recipe_path"}
        found = sorted(key for key in legacy_keys if key in publish_raw)
        if not found:
            return
        joined = ", ".join(found)
        raise ConfigError(
            f"legacy recipe keys detected in publish: {joined}. "
            "레시피 설정은 최상위 recipe_config 섹션으로 이동해주세요."
        )

    @staticmethod
    def _ensure_inline_recipe_config_removed(raw: Mapping[str, Any]) -> None:
        """Reject inline recipe_config block in main config."""

        if "recipe_config" not in raw:
            return
        raise ConfigError(
            "메인 설정 파일의 inline recipe_config 는 더 이상 지원되지 않습니다. "
            "recipe_config_path 로 별도 YAML 파일을 지정해주세요."
        )

    @staticmethod
    def _resolve_recipe_config_path(main_config_path: Path, raw_recipe_config_path: Any) -> Path:
        """Resolve recipe config file path from main config."""

        recipe_config_path = str(raw_recipe_config_path or "").strip()
        if not recipe_config_path:
            raise ConfigError("recipe_config_path 가 누락되었습니다. 별도 recipe 설정 파일 경로를 지정해주세요.")

        candidate = Path(recipe_config_path)
        if candidate.is_absolute():
            return candidate

        return (main_config_path.parent / candidate).resolve()

    @staticmethod
    def _load_yaml_mapping(path: Path, label: str) -> dict[str, Any]:
        """Load one YAML file and ensure its top-level shape is a mapping."""

        if not path.exists():
            raise ConfigError(f"{label}을 찾을 수 없습니다: {path}")

        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"{label} 파싱 오류 ({path}): {exc}") from exc

        if not isinstance(raw, dict):
            raise ConfigError(f"{label} 최상위 구조는 key-value 형식이어야 합니다: {path}")

        return raw

    @staticmethod
    def _parse_recipe_config(recipe_raw: Mapping[str, Any]) -> RecipeConfig:
        """Parse top-level recipe configuration."""

        raw_recipes = recipe_raw.get("recipes")
        recipes: list[RecipeItem] = []

        if raw_recipes is None:
            recipes = default_recipe_items()
        elif isinstance(raw_recipes, list):
            for idx, item in enumerate(raw_recipes, start=1):
                if not isinstance(item, Mapping):
                    raise ConfigError(f"recipe_config.recipes[{idx}] 항목은 key-value 형식이어야 합니다.")
                alias = str(item.get("alias", "")).strip()
                path = str(item.get("path", "")).strip()
                if not alias or not path:
                    raise ConfigError(
                        f"recipe_config.recipes[{idx}] 항목은 alias/path 모두 필요합니다."
                    )
                recipes.append(RecipeItem(alias=alias, path=path))
        else:
            raise ConfigError("recipe_config.recipes 는 list 형식이어야 합니다.")

        default_alias = str(recipe_raw.get("default_alias", "")).strip() or None
        return RecipeConfig(default_alias=default_alias, recipes=recipes)

    @staticmethod
    def _parse_queue_declare_config(
        raw_config: Any,
        default_config: QueueDeclareConfig,
        label: str,
    ) -> QueueDeclareConfig:
        """Parse queue_declare settings from YAML with strict validation."""

        if raw_config is None:
            return default_config
        if not isinstance(raw_config, Mapping):
            raise ConfigError(f"{label} 설정은 key-value 형식이어야 합니다.")

        arguments = raw_config.get("arguments", default_config.arguments)
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, Mapping):
            raise ConfigError(f"{label}.arguments 는 key-value 형식이어야 합니다.")

        return QueueDeclareConfig(
            durable=ConfigLoader._read_bool(raw_config.get("durable", default_config.durable), f"{label}.durable"),
            exclusive=ConfigLoader._read_bool(
                raw_config.get("exclusive", default_config.exclusive), f"{label}.exclusive"
            ),
            auto_delete=ConfigLoader._read_bool(
                raw_config.get("auto_delete", default_config.auto_delete), f"{label}.auto_delete"
            ),
            arguments={str(key): value for key, value in arguments.items()},
        )

    @staticmethod
    def _read_bool(value: Any, label: str) -> bool:
        """Read a boolean config value with user-friendly errors."""

        if isinstance(value, bool):
            return value
        raise ConfigError(f"{label} 는 true/false 여야 합니다.")
