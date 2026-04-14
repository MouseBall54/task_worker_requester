"""Application bootstrap and dependency wiring."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

from app.controller import TaskController
from config.config_loader import ConfigError, ConfigLoader
from services.broker import build_broker_provider
from state.task_store import TaskStore
from ui.main_window import MainWindow
from utils.logging_setup import setup_logging


def run_app(config_path: str = "config/app_config.yaml") -> int:
    """Create app dependencies and start Qt event loop."""

    try:
        app_config = ConfigLoader.load(config_path)
    except ConfigError as exc:
        print(f"[ConfigError] {exc}")
        return 1

    logger = setup_logging(app_config.log_level)

    app = QApplication(sys.argv)
    app.setApplicationName(app_config.ui.app_name)

    styles_path = Path(app_config.styles_path)
    if styles_path.exists():
        app.setStyleSheet(styles_path.read_text(encoding="utf-8"))
    else:
        logger.warning("스타일 파일을 찾지 못했습니다: %s", styles_path)

    store = TaskStore()
    broker_provider = build_broker_provider(app_config)
    window = MainWindow(config=app_config)
    controller = TaskController(
        config=app_config,
        view=window,
        store=store,
        broker_provider=broker_provider,
        logger=logger,
    )

    app.aboutToQuit.connect(controller.shutdown)

    window.show()
    return app.exec()
