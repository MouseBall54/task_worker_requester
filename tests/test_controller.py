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
        add_folder_requested = Signal(list)
        add_subfolders_requested = Signal(list)
        delete_folders_requested = Signal(list)
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
            controller.on_add_folder_requested([str(folder)])

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

    def test_dispatch_backfills_slots_when_active_folder_finishes_without_threshold(self) -> None:
        config = AppConfig(
            rabbitmq=RabbitMQConfig(host="127.0.0.1", port=5672, username="guest", password="guest"),
            publish=PublishConfig(image_extensions=[".jpg"], initial_open_folders=2, max_active_open_folders=3),
            ui=UiConfig(),
            mock_mode=True,
        )
        view = DummyView()
        store = TaskStore()
        logger = logging.getLogger("controller_dispatch_refill_test")
        logger.handlers.clear()
        logger.addHandler(logging.NullHandler())

        store.register_folder_map(
            {
                "f1": ["f1/a.jpg", "f1/b.jpg"],
                "f2": ["f2/a.jpg", "f2/b.jpg", "f2/c.jpg", "f2/d.jpg"],
                "f3": ["f3/a.jpg"],
                "f4": ["f4/a.jpg"],
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

        for task in store.get_image_tasks(opened[0]):
            task.status = TaskStatus.FAIL

        dispatched: list = []

        def _fake_start_publish_worker(messages, publish_exchange, publish_routing_key):  # noqa: ANN001
            _ = publish_exchange
            _ = publish_routing_key
            dispatched.extend(messages)

        controller._start_publish_worker = _fake_start_publish_worker  # type: ignore[method-assign]
        controller._maybe_dispatch_next_folder_batch()

        self.assertEqual(controller._next_folder_batch_index, 4)
        self.assertEqual(len(controller._active_folder_paths), 3)
        self.assertEqual(len(dispatched), 2)
        self.assertTrue(any("slot refill" in log for log in view.logs))

    def test_dispatch_backfills_multiple_vacant_slots_in_single_cycle(self) -> None:
        config = AppConfig(
            rabbitmq=RabbitMQConfig(host="127.0.0.1", port=5672, username="guest", password="guest"),
            publish=PublishConfig(image_extensions=[".jpg"], initial_open_folders=2, max_active_open_folders=4),
            ui=UiConfig(),
            mock_mode=True,
        )
        view = DummyView()
        store = TaskStore()
        logger = logging.getLogger("controller_dispatch_multi_refill_test")
        logger.handlers.clear()
        logger.addHandler(logging.NullHandler())

        store.register_folder_map(
            {
                "f1": ["f1/a.jpg"],
                "f2": ["f2/a.jpg"],
                "f3": ["f3/a.jpg"],
                "f4": ["f4/a.jpg"],
                "f5": ["f5/a.jpg"],
                "f6": ["f6/a.jpg"],
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
        _, opened = controller._take_next_folder_batches(4)

        for task in store.get_image_tasks(opened[0]):
            task.status = TaskStatus.SUCCESS
        for task in store.get_image_tasks(opened[1]):
            task.status = TaskStatus.FAIL

        dispatched: list = []

        def _fake_start_publish_worker(messages, publish_exchange, publish_routing_key):  # noqa: ANN001
            _ = publish_exchange
            _ = publish_routing_key
            dispatched.extend(messages)

        controller._start_publish_worker = _fake_start_publish_worker  # type: ignore[method-assign]
        controller._maybe_dispatch_next_folder_batch()

        self.assertEqual(controller._next_folder_batch_index, 6)
        self.assertEqual(len(controller._active_folder_paths), 4)
        self.assertEqual(len(dispatched), 2)

    def test_dispatch_does_not_expand_without_threshold_when_no_slots_were_freed(self) -> None:
        config = AppConfig(
            rabbitmq=RabbitMQConfig(host="127.0.0.1", port=5672, username="guest", password="guest"),
            publish=PublishConfig(image_extensions=[".jpg"], initial_open_folders=2, max_active_open_folders=3),
            ui=UiConfig(),
            mock_mode=True,
        )
        view = DummyView()
        store = TaskStore()
        logger = logging.getLogger("controller_dispatch_threshold_guard_test")
        logger.handlers.clear()
        logger.addHandler(logging.NullHandler())

        store.register_folder_map(
            {
                "f1": ["f1/a.jpg", "f1/b.jpg", "f1/c.jpg"],
                "f2": ["f2/a.jpg", "f2/b.jpg", "f2/c.jpg"],
                "f3": ["f3/a.jpg"],
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
        controller._take_next_folder_batches(2)

        dispatched: list = []

        def _fake_start_publish_worker(messages, publish_exchange, publish_routing_key):  # noqa: ANN001
            _ = publish_exchange
            _ = publish_routing_key
            dispatched.extend(messages)

        controller._start_publish_worker = _fake_start_publish_worker  # type: ignore[method-assign]
        before_index = controller._next_folder_batch_index
        controller._maybe_dispatch_next_folder_batch()

        self.assertEqual(controller._next_folder_batch_index, before_index)
        self.assertEqual(len(dispatched), 0)
        self.assertTrue(any("threshold unmet" in log for log in view.logs))

    def test_dispatch_uses_only_refill_when_initial_equals_active_cap(self) -> None:
        config = AppConfig(
            rabbitmq=RabbitMQConfig(host="127.0.0.1", port=5672, username="guest", password="guest"),
            publish=PublishConfig(image_extensions=[".jpg"], initial_open_folders=3, max_active_open_folders=3),
            ui=UiConfig(),
            mock_mode=True,
        )
        view = DummyView()
        store = TaskStore()
        logger = logging.getLogger("controller_dispatch_equal_cap_test")
        logger.handlers.clear()
        logger.addHandler(logging.NullHandler())

        store.register_folder_map(
            {
                "f1": ["f1/a.jpg"],
                "f2": ["f2/a.jpg"],
                "f3": ["f3/a.jpg"],
                "f4": ["f4/a.jpg"],
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
        opened_messages, opened_folders = controller._take_next_folder_batches(3)
        self.assertEqual(len(opened_messages), 3)
        self.assertEqual(len(opened_folders), 3)

        dispatched: list = []

        def _fake_start_publish_worker(messages, publish_exchange, publish_routing_key):  # noqa: ANN001
            _ = publish_exchange
            _ = publish_routing_key
            dispatched.extend(messages)

        controller._start_publish_worker = _fake_start_publish_worker  # type: ignore[method-assign]
        controller._maybe_dispatch_next_folder_batch()
        self.assertEqual(len(dispatched), 0)

        for task in store.get_image_tasks(opened_folders[0]):
            task.status = TaskStatus.TIMEOUT

        controller._maybe_dispatch_next_folder_batch()
        self.assertEqual(len(dispatched), 1)
        self.assertEqual(controller._next_folder_batch_index, 4)

    def test_running_addition_is_appended_to_current_session_schedule(self) -> None:
        config = AppConfig(
            rabbitmq=RabbitMQConfig(host="127.0.0.1", port=5672, username="guest", password="guest"),
            publish=PublishConfig(image_extensions=[".jpg"]),
            ui=UiConfig(),
            mock_mode=True,
        )
        view = DummyView()
        store = TaskStore()
        logger = logging.getLogger("controller_running_add_test")
        logger.handlers.clear()
        logger.addHandler(logging.NullHandler())

        controller = TaskController(
            config=config,
            view=view,  # type: ignore[arg-type]
            store=store,
            broker_provider=build_broker_provider(config),
            logger=logger,
        )

        controller._active = True
        controller._active_result_queue = config.rabbitmq.result_queue_base
        controller._publish_exchange = ""
        controller._publish_routing_key = config.rabbitmq.request_queue
        controller._maybe_dispatch_next_folder_batch = lambda: None  # type: ignore[method-assign]

        with TemporaryDirectory() as temp_dir:
            folder = Path(temp_dir) / "folder_new"
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "a.jpg").write_text("x", encoding="utf-8")
            (folder / "b.jpg").write_text("x", encoding="utf-8")

            controller.on_add_folder_requested([str(folder)])

        self.assertEqual(len(controller._folder_message_batches), 1)
        _, messages = controller._folder_message_batches[0]
        self.assertEqual(len(messages), 2)
        self.assertEqual(len(controller._scheduled_request_ids), 2)
        for message in messages:
            task = store.get_task(message.request_id)
            self.assertIsNotNone(task)
            assert task is not None
            self.assertIsNotNone(task.expected_message)

    def test_delete_folders_requested_removes_pending_only(self) -> None:
        config = AppConfig(
            rabbitmq=RabbitMQConfig(host="127.0.0.1", port=5672, username="guest", password="guest"),
            publish=PublishConfig(image_extensions=[".jpg"]),
            ui=UiConfig(),
            mock_mode=True,
        )
        view = DummyView()
        store = TaskStore()
        logger = logging.getLogger("controller_delete_pending_test")
        logger.handlers.clear()
        logger.addHandler(logging.NullHandler())

        store.register_folder_map(
            {
                "f_pending": ["f_pending/a.jpg", "f_pending/b.jpg"],
                "f_running": ["f_running/a.jpg", "f_running/b.jpg"],
            }
        )
        sent_request_id = store.get_image_tasks("f_running")[0].request_id
        store.mark_task_sent(sent_request_id)

        controller = TaskController(
            config=config,
            view=view,  # type: ignore[arg-type]
            store=store,
            broker_provider=build_broker_provider(config),
            logger=logger,
        )

        controller._folder_message_batches = store.build_pending_messages_by_folder("RUN", "result.q", "recipe.json")
        controller._next_folder_batch_index = 0
        controller._scheduled_request_ids = {
            message.request_id
            for _, messages in controller._folder_message_batches
            for message in messages
        }

        controller.on_delete_folders_requested(["f_pending", "f_running"])

        self.assertIsNone(store.get_folder_summary("f_pending"))
        self.assertIsNotNone(store.get_folder_summary("f_running"))
        remaining_batch_paths = [folder_path for folder_path, _ in controller._folder_message_batches]
        self.assertNotIn("f_pending", remaining_batch_paths)
        self.assertIn("f_running", remaining_batch_paths)
        self.assertTrue(any("삭제 차단" in log for log in view.logs))

    def test_start_without_pending_but_with_inflight_resumes_polling_only(self) -> None:
        config = AppConfig(
            rabbitmq=RabbitMQConfig(host="127.0.0.1", port=5672, username="guest", password="guest"),
            publish=PublishConfig(image_extensions=[".jpg"], polling_interval_seconds=5),
            ui=UiConfig(),
            mock_mode=True,
        )
        view = DummyView()
        store = TaskStore()
        logger = logging.getLogger("controller_polling_resume_test")
        logger.handlers.clear()
        logger.addHandler(logging.NullHandler())

        store.register_folder_map({"f1": ["f1/a.jpg"]})
        task = store.get_image_tasks("f1")[0]
        store.mark_task_sent(task.request_id)

        controller = TaskController(
            config=config,
            view=view,  # type: ignore[arg-type]
            store=store,
            broker_provider=build_broker_provider(config),
            logger=logger,
        )

        captured: dict[str, object] = {}

        def _fake_start_polling_worker(queue_name: str, polling_interval: int) -> None:
            captured["queue_name"] = queue_name
            captured["polling_interval"] = polling_interval

        controller._start_polling_worker = _fake_start_polling_worker  # type: ignore[method-assign]
        controller._ensure_resolved_result_queue = (  # type: ignore[method-assign]
            lambda: "task.result.client_192.168.0.10"
        )
        controller._resolved_local_ipv4 = "192.168.0.10"
        controller.on_start_requested()

        self.assertTrue(controller._active)
        self.assertTrue(controller._publish_finished)
        self.assertEqual(captured.get("queue_name"), "task.result.client_192.168.0.10")
        self.assertEqual(captured.get("polling_interval"), 1)
        self.assertTrue(any("결과 모니터링만 재개" in log for log in view.logs))

    def test_dispatch_force_open_when_active_zero_and_remaining_batches_exist(self) -> None:
        config = AppConfig(
            rabbitmq=RabbitMQConfig(host="127.0.0.1", port=5672, username="guest", password="guest"),
            publish=PublishConfig(image_extensions=[".jpg"], initial_open_folders=2, max_active_open_folders=3),
            ui=UiConfig(),
            mock_mode=True,
        )
        view = DummyView()
        store = TaskStore()
        logger = logging.getLogger("controller_dispatch_force_open_test")
        logger.handlers.clear()
        logger.addHandler(logging.NullHandler())

        store.register_folder_map(
            {
                "f1": ["f1/a.jpg"],
                "f2": ["f2/a.jpg"],
                "f3": ["f3/a.jpg"],
                "f4": ["f4/a.jpg"],
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
        controller._next_folder_batch_index = 2

        for task in store.get_image_tasks("f1"):
            task.status = TaskStatus.SUCCESS
        for task in store.get_image_tasks("f2"):
            task.status = TaskStatus.FAIL

        dispatched: list = []

        def _fake_start_publish_worker(messages, publish_exchange, publish_routing_key):  # noqa: ANN001
            _ = publish_exchange
            _ = publish_routing_key
            dispatched.extend(messages)

        controller._start_publish_worker = _fake_start_publish_worker  # type: ignore[method-assign]
        controller._maybe_dispatch_next_folder_batch()

        self.assertEqual(len(dispatched), 2)
        self.assertEqual(controller._next_folder_batch_index, 4)
        self.assertTrue(any("force-open (anti-stall)" in log for log in view.logs))

    def test_synchronize_dispatch_state_recovers_running_folder_from_store(self) -> None:
        config = AppConfig(
            rabbitmq=RabbitMQConfig(host="127.0.0.1", port=5672, username="guest", password="guest"),
            publish=PublishConfig(image_extensions=[".jpg"]),
            ui=UiConfig(),
            mock_mode=True,
        )
        view = DummyView()
        store = TaskStore()
        logger = logging.getLogger("controller_sync_state_test")
        logger.handlers.clear()
        logger.addHandler(logging.NullHandler())

        store.register_folder_map({"f1": ["f1/a.jpg"], "f2": ["f2/a.jpg"]})
        grouped = store.build_pending_messages_by_folder("RUN", "result.q", "recipe.json")

        controller = TaskController(
            config=config,
            view=view,  # type: ignore[arg-type]
            store=store,
            broker_provider=build_broker_provider(config),
            logger=logger,
        )

        controller._reset_publish_schedule_state()
        controller._folder_message_batches = grouped
        controller._next_folder_batch_index = 0
        controller._opened_folder_paths.clear()
        controller._active_folder_paths.clear()
        running_task = store.get_image_tasks("f1")[0]
        running_task.status = TaskStatus.RUNNING

        remaining = controller._synchronize_dispatch_state()
        self.assertEqual(remaining, 2)
        self.assertIn("f1", controller._opened_folder_paths)
        self.assertIn("f1", controller._active_folder_paths)

    def test_is_publish_worker_running_clears_stale_runtime_error_reference(self) -> None:
        class _BrokenThread:
            def isRunning(self) -> bool:  # noqa: N802
                raise RuntimeError("deleted")

        config = AppConfig(
            rabbitmq=RabbitMQConfig(host="127.0.0.1", port=5672, username="guest", password="guest"),
            publish=PublishConfig(image_extensions=[".jpg"]),
            ui=UiConfig(),
            mock_mode=True,
        )
        view = DummyView()
        store = TaskStore()
        logger = logging.getLogger("controller_stale_publish_ref_test")
        logger.handlers.clear()
        logger.addHandler(logging.NullHandler())

        controller = TaskController(
            config=config,
            view=view,  # type: ignore[arg-type]
            store=store,
            broker_provider=build_broker_provider(config),
            logger=logger,
        )
        controller._publish_thread = _BrokenThread()  # type: ignore[assignment]
        controller._publish_worker = object()  # type: ignore[assignment]

        self.assertFalse(controller._is_publish_worker_running())
        self.assertIsNone(controller._publish_thread)
        self.assertIsNone(controller._publish_worker)


if __name__ == "__main__":
    unittest.main()
