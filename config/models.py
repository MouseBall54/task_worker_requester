"""Configuration models for the task worker requester application."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


ScanMode = Literal["direct", "recursive"]


@dataclass(slots=True)
class RecipePreset:
    """Alias/path pair shown in UI recipe selector."""

    alias: str
    path: str


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
    heartbeat: int = 30
    blocked_connection_timeout: int = 30
    connection_attempts: int = 3
    retry_delay_seconds: float = 2.0


@dataclass(slots=True)
class PublishConfig:
    """Job publish and polling behavior settings."""

    default_action: str = "RUN_RECIPE"
    default_recipe_path: str = "recipes/default_recipe.json"
    default_recipe_alias: str | None = None
    recipe_presets: list[RecipePreset] = field(default_factory=list)
    polling_interval_seconds: int = 5
    timeout_seconds: int = 300
    max_messages_per_poll: int = 50
    max_publish_retries: int = 3
    publish_retry_backoff_seconds: float = 1.5
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
    publish: PublishConfig = field(default_factory=PublishConfig)
    ui: UiConfig = field(default_factory=UiConfig)
    mock_mode: bool = False
    log_level: str = "INFO"

    @property
    def styles_path(self) -> Path:
        """Return path to bundled QSS stylesheet."""

        return Path("ui") / "styles.qss"
