"""Application bootstrap and dependency wiring."""

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication, QMessageBox

from app.controller import TaskController
from app.runtime_paths import RuntimePathError, resolve_default_config_path, resolve_stylesheet_path
from config.config_loader import ConfigError, ConfigLoader
from services.broker import build_broker_provider
from state.task_store import TaskStore
from ui.main_window import MainWindow
from utils.logging_setup import setup_logging


def run_app(config_path: str | None = None) -> int:
    """Create app dependencies and start Qt event loop."""

    try:
        resolved_config_path = resolve_default_config_path(config_path)
        app_config = ConfigLoader.load(resolved_config_path)
    except (ConfigError, RuntimePathError) as exc:
        message = f"설정 파일을 불러오지 못했습니다.\n\n{exc}"
        print(f"[ConfigError] {exc}")
        _show_config_error_dialog(message)
        return 1

    logger = setup_logging(app_config.log_level)
    logger.info("사용 설정 파일: %s", resolved_config_path)

    app = QApplication(sys.argv)
    app.setApplicationName(app_config.ui.app_name)

    styles_path = resolve_stylesheet_path()
    if styles_path and styles_path.exists():
        app.setStyleSheet(styles_path.read_text(encoding="utf-8"))
    else:
        logger.warning("스타일 파일을 찾지 못했습니다.")

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


def _show_config_error_dialog(message: str) -> None:
    """Try to show user-friendly config error popup in GUI environments."""

    app = QApplication.instance()
    created_app = False

    if app is None:
        try:
            app = QApplication([])
            created_app = True
        except Exception:
            return

    try:
        QMessageBox.critical(None, "설정 오류", message)
    except Exception:
        return
    finally:
        if created_app and app is not None:
            app.quit()
