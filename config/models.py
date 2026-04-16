"""Configuration models for the task worker requester application."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal


ScanMode = Literal["direct", "recursive"]


@dataclass(slots=True)
class RecipeItem:
    """Alias/path pair shown in UI recipe selector."""

    alias: str
    path: str


@dataclass(slots=True)
class QueueDeclareConfig:
    """Queue declaration options passed to RabbitMQ."""

    durable: bool = True
    exclusive: bool = False
    auto_delete: bool = False
    arguments: dict[str, Any] = field(default_factory=dict)


def default_request_queue_declare() -> QueueDeclareConfig:
    """Return default request queue declaration options."""

    return QueueDeclareConfig(
        durable=True,
        exclusive=False,
        auto_delete=False,
        arguments={"x-max-priority": 5, "module_group": "IPDK_WORKER"},
    )


def default_result_queue_declare() -> QueueDeclareConfig:
    """Return default result queue declaration options."""

    return QueueDeclareConfig(
        durable=True,
        exclusive=False,
        auto_delete=False,
        arguments={"x-max-priority": 5, "module_group": "default"},
    )


def default_recipe_items() -> list[RecipeItem]:
    """Return default recipe list used by code-first configs/tests."""

    return [RecipeItem(alias="Default Recipe", path="recipes/default_recipe.json")]


@dataclass(slots=True)
class RecipeConfig:
    """Top-level recipe alias/path configuration."""

    default_alias: str | None = None
    recipes: list[RecipeItem] = field(default_factory=default_recipe_items)

    @property
    def default_recipe(self) -> RecipeItem:
        """Return the selected default recipe entry."""

        normalized_default = (self.default_alias or "").strip().lower()
        if normalized_default:
            for recipe in self.recipes:
                if recipe.alias.strip().lower() == normalized_default:
                    return recipe
        return self.recipes[0]

    @property
    def default_path(self) -> str:
        """Return default recipe path for runtime fallback usage."""

        return self.default_recipe.path


@dataclass(slots=True)
class RabbitMQConfig:
    """RabbitMQ connection and queue settings."""

    host: str
    port: int
    username: str
    password: str
    virtual_host: str = "/"
    request_exchange: str = ""
    request_routing_key: str = "task.request"
    request_queue: str = "task.request"
    result_queue_base: str = "task.result.client"
    request_queue_declare: QueueDeclareConfig = field(default_factory=default_request_queue_declare)
    result_queue_declare: QueueDeclareConfig = field(default_factory=default_result_queue_declare)
    heartbeat: int = 30
    blocked_connection_timeout: int = 30
    connection_attempts: int = 3
    retry_delay_seconds: float = 2.0

    @property
    def request_queue_max_priority(self) -> int | None:
        """Return request queue max priority declared in RabbitMQ, if configured."""

        raw_priority = self.request_queue_declare.arguments.get("x-max-priority")
        if not isinstance(raw_priority, int) or isinstance(raw_priority, bool):
            return None
        return raw_priority if raw_priority >= 1 else None


@dataclass(slots=True)
class PublishConfig:
    """Job publish and polling behavior settings."""

    default_action: str = "RUN_RECIPE"
    polling_interval_seconds: int = 5
    timeout_seconds: int = 300
    max_messages_per_poll: int = 50
    max_publish_retries: int = 3
    publish_retry_backoff_seconds: float = 1.5
    default_priority: int = 0
    initial_open_folders: int = 2
    max_active_open_folders: int = 3
    image_extensions: list[str] = field(
        default_factory=lambda: [".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff"]
    )
    scan_mode: ScanMode = "direct"


@dataclass(slots=True)
class UiConfig:
    """UI-specific settings."""

    app_name: str = "RabbitMQ Task Tracker"
    window_width: int = 1480
    window_height: int = 900
    theme: str = "dark"
    font_family: str = "Segoe UI"


@dataclass(slots=True)
class AppConfig:
    """Root configuration object."""

    rabbitmq: RabbitMQConfig
    recipe_config_path: str | None = None
    recipe_config: RecipeConfig = field(default_factory=RecipeConfig)
    publish: PublishConfig = field(default_factory=PublishConfig)
    ui: UiConfig = field(default_factory=UiConfig)
    mock_mode: bool = False
    log_level: str = "INFO"

    @property
    def styles_path(self) -> Path:
        """Return path to bundled QSS stylesheet."""

        return Path("ui") / "styles.qss"
