"""Lightweight controller behavior tests."""

from __future__ import annotations

import logging
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from config.models import AppConfig, PublishConfig, RabbitMQConfig, UiConfig
from models.task_models import TaskStatus
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
        mq_preview_requested = Signal(str)

    def __init__(self) -> None:
        if PYSIDE_AVAILABLE:
            super().__init__()
        self.logs: list[str] = []
        self.running = False
        self.connection = (False, "")
        self.overall: dict[str, float | int | None] = {}
        self.active_result_queue: str | None = None

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

    def set_overall_stats(self, stats: dict[str, float | int | None]) -> None:
        self.overall = stats

    def set_active_result_queue(self, queue_name: str | None) -> None:
        self.active_result_queue = queue_name

    def clear_progress_views(self) -> None:
        return

    def show_mq_preview(self, _preview) -> None:  # noqa: ANN001
        return

    def current_runtime_settings(self) -> tuple[str, str, int, int]:
        return ("RUN_RECIPE", "recipe.json", 1, 0)


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

    def test_controller_uses_configured_folder_open_limits(self) -> None:
        config = AppConfig(
            rabbitmq=RabbitMQConfig(host="127.0.0.1", port=5672, username="guest", password="guest"),
            publish=PublishConfig(
                image_extensions=[".jpg"],
                initial_open_folders=1,
                max_active_open_folders=4,
            ),
            ui=UiConfig(),
            mock_mode=True,
        )
        view = DummyView()
        store = TaskStore()
        logger = logging.getLogger("controller_limits_test")
        logger.handlers.clear()
        logger.addHandler(logging.NullHandler())

        controller = TaskController(
            config=config,
            view=view,  # type: ignore[arg-type]
            store=store,
            broker_provider=build_broker_provider(config),
            logger=logger,
        )

        self.assertEqual(controller._max_initial_open_folders, 1)
        self.assertEqual(controller._max_active_open_folders, 4)

    def test_dispatch_opens_only_one_more_when_threshold_met_and_respects_active_cap(self) -> None:
        config = AppConfig(
            rabbitmq=RabbitMQConfig(host="127.0.0.1", port=5672, username="guest", password="guest"),
            publish=PublishConfig(image_extensions=[".jpg"]),
            ui=UiConfig(),
            mock_mode=True,
        )
        view = DummyView()
        store = TaskStore()
        logger = logging.getLogger("controller_dispatch_cap_test")
        logger.handlers.clear()
        logger.addHandler(logging.NullHandler())

        store.register_folder_map(
            {
                "f1": ["f1/a.jpg", "f1/b.jpg"],
                "f2": ["f2/a.jpg", "f2/b.jpg"],
                "f3": ["f3/a.jpg", "f3/b.jpg"],
                "f4": ["f4/a.jpg", "f4/b.jpg"],
            }
        )

        controller = TaskController(
            config=config,
            view=view,  # type: ignore[arg-type]
            store=store,
            broker_provider=build_broker_provider(config),
            logger=logger,
        )

        controller._reset_publish_schedule_state()
        controller._folder_message_batches = store.build_pending_messages_by_folder("RUN", "result.q", "recipe.json")
        controller._publish_exchange = ""
        controller._publish_routing_key = "task.request"

        first_messages, opened = controller._take_next_folder_batches(2)
        self.assertEqual(len(opened), 2)
        self.assertEqual(len(controller._active_folder_paths), 2)

        # Make one active folder meet 50% progress.
        first_folder = opened[0]
        first_folder_tasks = store.get_image_tasks(first_folder)
        self.assertGreaterEqual(len(first_folder_tasks), 2)
        first_folder_tasks[0].status = TaskStatus.SUCCESS

        dispatched: list = []

        def _fake_start_publish_worker(messages, publish_exchange, publish_routing_key):  # noqa: ANN001
            _ = publish_exchange
            _ = publish_routing_key
            dispatched.extend(messages)

        controller._start_publish_worker = _fake_start_publish_worker  # type: ignore[method-assign]
        controller._maybe_dispatch_next_folder_batch()
        self.assertGreater(len(dispatched), 0)
        self.assertEqual(len(controller._active_folder_paths), 3)
        opened_after_first_dispatch = controller._next_folder_batch_index

        # Threshold still true, but active cap=3 should block additional opening.
        dispatched.clear()
        controller._maybe_dispatch_next_folder_batch()
        self.assertEqual(len(dispatched), 0)
        self.assertEqual(controller._next_folder_batch_index, opened_after_first_dispatch)

    def test_dispatch_can_resume_after_active_folder_completes(self) -> None:
        config = AppConfig(
            rabbitmq=RabbitMQConfig(host="127.0.0.1", port=5672, username="guest", password="guest"),
            publish=PublishConfig(image_extensions=[".jpg"]),
            ui=UiConfig(),
            mock_mode=True,
        )
        view = DummyView()
        store = TaskStore()
        logger = logging.getLogger("controller_dispatch_resume_test")
        logger.handlers.clear()
        logger.addHandler(logging.NullHandler())

        store.register_folder_map(
            {
                "f1": ["f1/a.jpg", "f1/b.jpg"],
                "f2": ["f2/a.jpg", "f2/b.jpg"],
                "f3": ["f3/a.jpg", "f3/b.jpg"],
                "f4": ["f4/a.jpg", "f4/b.jpg"],
                "f5": ["f5/a.jpg", "f5/b.jpg"],
            }
        )

        controller = TaskController(
            config=config,
            view=view,  # type: ignore[arg-type]
            store=store,
            broker_provider=build_broker_provider(config),
            logger=logger,
        )

        controller._reset_publish_schedule_state()
        controller._folder_message_batches = store.build_pending_messages_by_folder("RUN", "result.q", "recipe.json")
        controller._publish_exchange = ""
        controller._publish_routing_key = "task.request"
        _, opened = controller._take_next_folder_batches(2)

        first_folder = opened[0]
        first_folder_tasks = store.get_image_tasks(first_folder)
        first_folder_tasks[0].status = TaskStatus.SUCCESS

        def _fake_start_publish_worker(messages, publish_exchange, publish_routing_key):  # noqa: ANN001
            _ = messages
            _ = publish_exchange
            _ = publish_routing_key

        controller._start_publish_worker = _fake_start_publish_worker  # type: ignore[method-assign]

        # Open third folder.
        controller._maybe_dispatch_next_folder_batch()
        self.assertEqual(len(controller._active_folder_paths), 3)

        # Complete one active folder entirely, then ensure one more can be opened.
        completed_folder = next(path for path in controller._active_folder_paths if path != first_folder)
        for task in store.get_image_tasks(completed_folder):
            task.status = TaskStatus.SUCCESS

        before_index = controller._next_folder_batch_index
        controller._maybe_dispatch_next_folder_batch()
        self.assertGreater(controller._next_folder_batch_index, before_index)
        self.assertLessEqual(len(controller._active_folder_paths), 3)


if __name__ == "__main__":
    unittest.main()
