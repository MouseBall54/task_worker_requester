"""Lightweight controller behavior tests."""

from __future__ import annotations

import logging
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from config.models import AppConfig, PublishConfig, RabbitMQConfig, UiConfig
from services.broker import build_broker_provider
from state.task_store import TaskStore

try:
    from PySide6.QtCore import QCoreApplication, QObject, Signal
    from app.controller import TaskController

    PYSIDE_AVAILABLE = True
except ImportError:  # pragma: no cover
    QCoreApplication = None  # type: ignore[assignment]
    QObject = object  # type: ignore[assignment]
    Signal = None  # type: ignore[assignment]
    PYSIDE_AVAILABLE = False


class DummyView(QObject if PYSIDE_AVAILABLE else object):
    """Headless test double for MainWindow contract."""

    if PYSIDE_AVAILABLE:
        add_folder_requested = Signal(str)
        add_subfolders_requested = Signal(str)
        clear_requested = Signal()
        start_requested = Signal()
        stop_requested = Signal()
        reset_requested = Signal()
        folder_row_selected = Signal(str)

    def __init__(self) -> None:
        if PYSIDE_AVAILABLE:
            super().__init__()
        self.logs: list[str] = []
        self.running = False
        self.connection = (False, "")
        self.overall: dict[str, float | int] = {}

    def set_running_state(self, running: bool) -> None:
        self.running = running

    def set_connection_status(self, connected: bool, label: str) -> None:
        self.connection = (connected, label)

    def append_log(self, message: str) -> None:
        self.logs.append(message)

    def set_image_tasks(self, tasks) -> None:  # noqa: ANN001
        _ = tasks

    def update_image_task(self, task) -> None:  # noqa: ANN001
        _ = task

    def upsert_folder_row(self, row) -> None:  # noqa: ANN001
        _ = row

    def set_folder_rows(self, rows) -> None:  # noqa: ANN001
        _ = rows

    def confirm_reset(self) -> bool:
        return True

    def set_overall_stats(self, stats: dict[str, float | int]) -> None:
        self.overall = stats

    def current_runtime_settings(self) -> tuple[str, str, int]:
        return ("RUN_RECIPE", "recipe.json", 1)


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 is required for controller tests.")
class TaskControllerTest(unittest.TestCase):
    """Verify controller can register folders through scanner/store."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QCoreApplication.instance() or QCoreApplication([])

    def test_add_folder_registers_tasks(self) -> None:
        config = AppConfig(
            rabbitmq=RabbitMQConfig(host="127.0.0.1", port=5672, username="guest", password="guest"),
            publish=PublishConfig(image_extensions=[".jpg"]),
            ui=UiConfig(),
            mock_mode=True,
        )
        view = DummyView()
        store = TaskStore()
        logger = logging.getLogger("controller_test")
        logger.handlers.clear()
        logger.addHandler(logging.NullHandler())

        controller = TaskController(
            config=config,
            view=view,  # type: ignore[arg-type]
            store=store,
            broker_provider=build_broker_provider(config),
            logger=logger,
        )

        with TemporaryDirectory() as temp_dir:
            folder = Path(temp_dir)
            (folder / "img.jpg").write_text("x", encoding="utf-8")
            controller.on_add_folder_requested(str(folder))

        self.assertEqual(store.overall_stats()["total"], 1)
        self.assertTrue(len(view.logs) >= 1)


if __name__ == "__main__":
    unittest.main()
