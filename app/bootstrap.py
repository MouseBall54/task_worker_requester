"""Application bootstrap and dependency wiring."""

from __future__ import annotations

import sys

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from app.controller import TaskController
from app.runtime_paths import (
    RuntimePathError,
    resolve_app_icon_path,
    resolve_default_config_path,
    resolve_logs_dir,
    resolve_stylesheet_path,
)
from app.single_instance import SingleInstanceGuard, ensure_single_instance
from config.config_loader import ConfigError, ConfigLoader
from services.broker import build_broker_provider
from state.task_store import TaskStore
from ui.main_window import MainWindow
from utils.logging_setup import setup_logging


APP_DISPLAY_NAME = "IPDK_plus"
SINGLE_INSTANCE_KEY = "IPDK_plus_single_instance"


def run_app(config_path: str | None = None) -> int:
    """Create app dependencies and start Qt event loop."""

    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(APP_DISPLAY_NAME)

    guard = ensure_single_instance(SINGLE_INSTANCE_KEY)
    if guard is None:
        _show_duplicate_instance_dialog()
        return 0

    try:
        resolved_config_path = resolve_default_config_path(config_path)
        app_config = ConfigLoader.load(resolved_config_path)
    except (ConfigError, RuntimePathError) as exc:
        message = f"설정 파일을 불러오지 못했습니다.\n\n{exc}"
        print(f"[ConfigError] {exc}")
        _release_guard(guard)
        _show_config_error_dialog(message)
        return 1

    logger = setup_logging(app_config.log_level, logs_dir=resolve_logs_dir())
    logger.info("사용 설정 파일: %s", resolved_config_path)
    app.setApplicationName(app_config.ui.app_name)

    icon = _resolve_runtime_window_icon(app)
    if icon is not None:
        app.setWindowIcon(icon)

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
    app.aboutToQuit.connect(guard.release)

    if icon is not None:
        window.setWindowIcon(icon)

    window.show()
    exit_code = app.exec()
    _release_guard(guard)
    return exit_code


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


def _show_duplicate_instance_dialog() -> None:
    """Explain why a second IPDK_plus instance is blocked."""

    message = (
        "IPDK_plus 는 같은 PC에서 하나의 인스턴스만 실행할 수 있습니다.\n\n"
        "중복 실행 시 결과 queue 처리와 request_id 매칭이 꼬일 수 있으므로 "
        "이미 실행 중인 프로그램만 유지하고 새로 실행한 프로그램은 종료합니다."
    )
    QMessageBox.warning(None, "중복 실행 감지", message)


def _release_guard(guard: SingleInstanceGuard | None) -> None:
    """Release the single-instance guard if it is still held."""

    if guard is None:
        return
    try:
        guard.release()
    except Exception:
        return


def _resolve_runtime_window_icon(app: QApplication) -> QIcon | None:
    """Resolve the best runtime window icon available for source and packaged runs."""

    icon_path = resolve_app_icon_path()
    if icon_path and icon_path.exists():
        icon = QIcon(str(icon_path))
        if not icon.isNull():
            return icon

    existing_icon = app.windowIcon()
    if existing_icon is not None and not existing_icon.isNull():
        return existing_icon
    return None
