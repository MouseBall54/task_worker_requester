"""Lightweight UI tests for main window tab behavior."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import Mock, patch

from config.models import AppConfig, PublishConfig, RabbitMQConfig, RecipeConfig, RecipeItem, UiConfig
from models.task_models import FolderSummary, RunHistorySummary, TaskStatus
from state.folder_index_repository import SCOPE_DEPTH, SCOPE_EXCLUDED
from state.folder_index_repository import FolderIndexRepository, INDEX_READY

try:
    from PySide6.QtCore import QItemSelectionModel, Qt
    from PySide6.QtGui import QStandardItem, QStandardItemModel
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import (
        QAbstractItemView,
        QApplication,
        QComboBox,
        QHeaderView,
        QLabel,
        QListWidgetItem,
        QSizePolicy,
        QToolButton,
    )
    from ui.main_window import (
        DuplicateFolderDialog,
        FavoriteRootManagerDialog,
        FolderTreeView,
        MainWindow,
        PreflightDialog,
        RecipePathRow,
        ResponsiveRecipeSettings,
        RunHistoryDialog,
    )

    PYSIDE_AVAILABLE = True
except ImportError:  # pragma: no cover
    Qt = None  # type: ignore[assignment]
    QTest = None  # type: ignore[assignment]
    QApplication = None  # type: ignore[assignment]
    MainWindow = None  # type: ignore[assignment]
    PYSIDE_AVAILABLE = False


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 is required for main window tests.")
class MainWindowTest(unittest.TestCase):
    """Verify default status tab and folder-selection tab switching."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])

    def _make_window(
        self,
        folder_index_database_path=None,
        ui_settings_path=None,
    ) -> MainWindow:  # noqa: ANN001
        config = AppConfig(
            rabbitmq=RabbitMQConfig(host="127.0.0.1", port=5672, username="guest", password="guest"),
            publish=PublishConfig(image_extensions=[".jpg"]),
            recipe_config=RecipeConfig(
                default_alias="Recipe A",
                recipes=[
                    RecipeItem(alias="Recipe A", path="recipes/a.json"),
                    RecipeItem(alias="Recipe B", path="recipes/b.json"),
                ],
            ),
            ui=UiConfig(),
            mock_mode=True,
        )
        return MainWindow(
            config,
            folder_index_database_path=folder_index_database_path,
            ui_settings_path=ui_settings_path,
        )

    def test_folder_table_columns_are_resizable_and_persist_independently(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            settings_path = root / "ui_state.ini"
            first = self._make_window(root / "first.sqlite3", settings_path)
            try:
                self.assertEqual(first.active_folder_table.columnWidth(2), 180)
                self.assertEqual(first.active_folder_table.columnWidth(3), 380)
                self.assertEqual(first.active_folder_table.columnWidth(4), 240)
                self.assertEqual(first.active_folder_table.columnWidth(9), 110)
                for column in range(first.active_folder_table_model.columnCount()):
                    self.assertEqual(
                        first.active_folder_table.horizontalHeader().sectionResizeMode(column),
                        QHeaderView.Interactive,
                    )
                first.active_folder_table.setColumnWidth(3, 520)
                first.active_folder_table.setColumnWidth(4, 330)
                first.completed_folder_table.setColumnWidth(2, 275)
                first.completed_folder_table.setColumnWidth(9, 145)
            finally:
                first.close()

            restored = self._make_window(root / "second.sqlite3", settings_path)
            try:
                self.assertEqual(restored.active_folder_table.columnWidth(3), 520)
                self.assertEqual(restored.active_folder_table.columnWidth(4), 330)
                self.assertEqual(restored.completed_folder_table.columnWidth(2), 275)
                self.assertEqual(restored.completed_folder_table.columnWidth(9), 145)
                self.assertEqual(restored.completed_folder_table.columnWidth(3), 380)
            finally:
                restored.close()

    def test_favorite_root_cache_search_renders_actual_folder_path(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "favorite_root"
            target = root / "project_alpha"
            target.mkdir(parents=True)
            database = Path(temp_dir) / "folder_index.sqlite3"
            window = self._make_window(database)
            try:
                window._folder_index_repository.add_favorite(str(root))
                window._folder_index_repository.refresh_root(str(root), full=True)
                window._reload_favorite_roots(selected_path=str(root))
                window.folder_search_edit.setText("project_alpha")

                self.assertEqual(window.favorite_root_list.count(), 1)
                self.assertIn(str(root), window.favorite_root_list.item(0).text())
                self.assertEqual(window.folder_search_results.count(), 1)
                self.assertEqual(
                    window.folder_search_results.item(0).data(Qt.UserRole),
                    str(target),
                )
            finally:
                window.close()

    def test_unified_folder_input_navigates_paths_and_searches_words(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "target"
            root.mkdir()
            window = self._make_window(Path(temp_dir) / "folder_index.sqlite3")
            navigated: list[str] = []
            searched: list[str] = []
            try:
                self.assertIs(window.path_jump_edit, window.folder_search_edit)
                self.assertEqual(window.btn_path_jump.text(), "검색")
                self.assertEqual(
                    window.btn_manage_favorite_roots.text(), "즐겨찾기 관리"
                )
                self.assertGreater(
                    window.btn_manage_favorite_roots.maximumWidth(),
                    window.btn_manage_favorite_roots.minimumWidth(),
                )
                self.assertGreater(
                    window.btn_refresh_favorite_roots.maximumWidth(),
                    window.btn_refresh_favorite_roots.minimumWidth(),
                )
                window.jump_to_path = (  # type: ignore[method-assign]
                    lambda path, show_feedback=True: navigated.append(path) or True
                )
                window._run_live_folder_search = (  # type: ignore[method-assign]
                    lambda: searched.append(window.path_jump_edit.text())
                )

                window.path_jump_edit.setText(str(root))
                window._on_path_jump_requested()
                window.path_jump_edit.setText("inspection")
                window._on_path_jump_requested()

                self.assertEqual(navigated, [str(root)])
                self.assertEqual(searched, ["inspection"])
            finally:
                window.close()

    def test_cancel_search_stops_background_refresh_and_keeps_results(self) -> None:
        window = self._make_window()
        worker = Mock()
        try:
            window._folder_index_worker = worker
            window._pending_folder_index_job = ([r"D:\root"], False, None, False)
            window.folder_search_results.addItem("cached result")
            window.btn_cancel_folder_search.setEnabled(True)

            window._cancel_folder_search()

            worker.cancel.assert_called_once_with()
            self.assertIsNone(window._pending_folder_index_job)
            self.assertFalse(window.btn_cancel_folder_search.isEnabled())
            self.assertEqual(window.folder_search_results.count(), 1)
            self.assertIn("일시정지 요청", window.folder_search_status.text())
        finally:
            window._folder_index_worker = None
            window.close()

    def test_typing_search_does_not_cancel_active_full_index(self) -> None:
        window = self._make_window()
        worker = Mock()
        try:
            window._folder_index_worker = worker
            window.folder_search_edit.setText("needle")
            window._on_folder_search_changed("needle")

            worker.cancel.assert_not_called()
            self.assertTrue(window._folder_search_debounce.isActive())
        finally:
            window._folder_index_worker = None
            window.close()

    def test_background_folder_index_refresh_updates_search_without_blocking_ui(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "favorite_root"
            target = root / "background_target"
            target.mkdir(parents=True)
            window = self._make_window(Path(temp_dir) / "folder_index.sqlite3")
            try:
                window._folder_index_repository.add_favorite(str(root))
                window._reload_favorite_roots(selected_path=str(root))
                window.folder_search_edit.setText("background_target")
                window._start_folder_index_job(
                    [str(root)],
                    full=True,
                )
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline and window._folder_index_thread is not None:
                    self._app.processEvents()
                    time.sleep(0.01)
                self._app.processEvents()

                self.assertIsNone(window._folder_index_thread)
                self.assertEqual(window.folder_search_results.count(), 1)
                self.assertEqual(
                    window.folder_search_results.item(0).data(Qt.UserRole),
                    str(target),
                )
                self.assertIn("캐시 결과", window.folder_search_status.text())
            finally:
                window.close()

    def test_startup_automatically_resumes_incomplete_persistent_index(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "root"
            for index in range(6):
                (root / f"folder-{index}").mkdir(parents=True)
            database = Path(temp_dir) / "folder_index.sqlite3"
            repository = FolderIndexRepository(database)
            repository.add_favorite(str(root))
            repository.prepare_full_scan(str(root))
            repository.resume_scan(str(root), max_folders=2)
            repository.close()

            window = self._make_window(database)
            try:
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    self._app.processEvents()
                    favorite = window._folder_index_repository.get_favorite(str(root))
                    if favorite and favorite.index_status == INDEX_READY:
                        break
                    time.sleep(0.01)

                favorite = window._folder_index_repository.get_favorite(str(root))
                assert favorite is not None
                self.assertEqual(favorite.index_status, INDEX_READY)
                self.assertEqual(favorite.pending_count, 0)
                self.assertEqual(favorite.indexed_count, 7)
            finally:
                window.close()

    def test_favorite_root_manager_moves_and_removes_selected_root(self) -> None:
        with TemporaryDirectory() as temp_dir:
            first = Path(temp_dir) / "first"
            second = Path(temp_dir) / "second"
            first.mkdir()
            second.mkdir()
            window = self._make_window(Path(temp_dir) / "folder_index.sqlite3")
            try:
                window._folder_index_repository.add_favorite(str(first))
                window._folder_index_repository.add_favorite(str(second))
                window._reload_favorite_roots(selected_path=str(second))
                dialog = FavoriteRootManagerDialog(
                    window._folder_index_repository, window
                )
                dialog.changed.connect(window._on_favorite_roots_managed)
                dialog._reload(selected_path=str(second))

                dialog._move_root("up")
                self.assertEqual(
                    [
                        window.favorite_root_list.item(index).data(Qt.UserRole)
                        for index in range(window.favorite_root_list.count())
                    ],
                    [str(second), str(first)],
                )

                dialog._remove_root()
                self.assertEqual(window.favorite_root_list.count(), 1)
                self.assertEqual(
                    window.favorite_root_list.item(0).data(Qt.UserRole),
                    str(first),
                )
                self.assertTrue(hasattr(window, "btn_manage_favorite_roots"))
                self.assertTrue(hasattr(window, "btn_refresh_favorite_roots"))
                self.assertFalse(hasattr(window, "btn_add_favorite_root"))
                dialog.close()
            finally:
                window.close()

    def test_favorite_root_manager_adds_selected_directory(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "new-root"
            root.mkdir()
            window = self._make_window(Path(temp_dir) / "folder_index.sqlite3")
            dialog = FavoriteRootManagerDialog(window._folder_index_repository, window)
            changes: list[tuple[str, str]] = []
            dialog.changed.connect(lambda action, path: changes.append((action, path)))
            try:
                self.assertEqual(dialog.add_button.text(), "폴더 추가")
                self.assertEqual(dialog.scope_combo.currentData(), SCOPE_DEPTH)
                self.assertEqual(
                    dialog.scope_combo.currentData(dialog.SCOPE_DEPTH_ROLE), 5
                )
                self.assertEqual(
                    [
                        dialog.scope_combo.itemData(index, dialog.SCOPE_DEPTH_ROLE)
                        for index in range(dialog.scope_combo.count())
                        if dialog.scope_combo.itemData(index) == SCOPE_DEPTH
                    ],
                    [3, 4, 5, 6],
                )
                with patch(
                    "ui.main_window.QFileDialog.getExistingDirectory",
                    return_value=str(root),
                ):
                    dialog._add_root()

                self.assertEqual(dialog.root_table.rowCount(), 1)
                self.assertEqual(dialog.root_table.columnCount(), 7)
                self.assertEqual(
                    [
                        dialog.root_table.horizontalHeaderItem(column).text()
                        for column in range(dialog.root_table.columnCount())
                    ],
                    ["Root 경로", "색인 범위", "상태", "색인됨", "대기", "오류", "마지막 완료"],
                )
                self.assertEqual(
                    dialog.root_table.item(0, 0).data(Qt.UserRole),
                    str(root),
                )
                self.assertEqual(dialog.root_table.item(0, 1).text(), "하위 5계층")
                favorite = window._folder_index_repository.get_favorite(str(root))
                assert favorite is not None
                self.assertEqual(favorite.scope_mode, SCOPE_DEPTH)
                self.assertEqual(favorite.max_depth, 5)
                self.assertEqual(changes, [("add", str(root))])
            finally:
                dialog.close()
                window.close()

    def test_favorite_root_manager_applies_depth_and_excluded_scope(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "root"
            root.mkdir()
            window = self._make_window(Path(temp_dir) / "folder_index.sqlite3")
            window._folder_index_repository.add_favorite(str(root))
            dialog = FavoriteRootManagerDialog(window._folder_index_repository, window)
            changes: list[tuple[str, str]] = []
            dialog.changed.connect(lambda action, path: changes.append((action, path)))
            try:
                dialog._reload(selected_path=str(root))
                for depth in (3, 4, 5, 6):
                    dialog.scope_combo.setCurrentIndex(
                        dialog._find_scope_index(SCOPE_DEPTH, depth)
                    )
                    dialog._apply_scope()
                    favorite = window._folder_index_repository.get_favorite(str(root))
                    assert favorite is not None
                    self.assertEqual(favorite.scope_mode, SCOPE_DEPTH)
                    self.assertEqual(favorite.max_depth, depth)
                    self.assertEqual(
                        dialog.root_table.item(0, 1).text(), f"하위 {depth}계층"
                    )

                dialog.scope_combo.setCurrentIndex(dialog.scope_combo.findData(SCOPE_EXCLUDED))
                dialog._apply_scope()
                favorite = window._folder_index_repository.get_favorite(str(root))
                assert favorite is not None
                self.assertEqual(favorite.scope_mode, SCOPE_EXCLUDED)
                self.assertEqual(changes, [("scope", str(root))] * 5)
            finally:
                dialog.close()
                window.close()

    def test_favorite_root_scope_edit_survives_periodic_status_reload(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "root"
            root.mkdir()
            window = self._make_window(Path(temp_dir) / "folder_index.sqlite3")
            window._folder_index_repository.add_favorite(str(root))
            dialog = FavoriteRootManagerDialog(window._folder_index_repository, window)
            try:
                dialog._reload(selected_path=str(root))
                depth_index = dialog._find_scope_index(SCOPE_DEPTH, 6)
                dialog.scope_combo.setCurrentIndex(depth_index)

                # The one-second status refresh must not overwrite an unapplied edit.
                dialog._reload(selected_path=str(root))

                self.assertEqual(dialog.scope_combo.currentData(), SCOPE_DEPTH)
                dialog._apply_scope()
                favorite = window._folder_index_repository.get_favorite(str(root))
                assert favorite is not None
                self.assertEqual(favorite.scope_mode, SCOPE_DEPTH)
                self.assertEqual(favorite.max_depth, 6)
            finally:
                dialog.close()
                window.close()

    def test_folder_search_more_appends_next_page_and_shows_total(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "root"
            for index in range(205):
                (root / f"page-target-{index:03d}").mkdir(parents=True)
            window = self._make_window(Path(temp_dir) / "folder_index.sqlite3")
            try:
                window._folder_index_repository.add_favorite(str(root))
                window._folder_index_repository.refresh_root(str(root), full=True)
                window.folder_search_edit.setText("page-target")

                self.assertEqual(window.folder_search_results.count(), 200)
                self.assertFalse(window.btn_more_folder_search.isHidden())
                self.assertIn("200/205", window.folder_search_status.text())

                window._load_more_folder_search_results()

                self.assertEqual(window.folder_search_results.count(), 205)
                self.assertFalse(window.btn_more_folder_search.isVisible())
                self.assertIn("205/205", window.folder_search_status.text())
            finally:
                window.close()

    def test_favorite_click_navigates_and_offline_root_is_marked(self) -> None:
        with TemporaryDirectory() as temp_dir:
            online = Path(temp_dir) / "online"
            offline = Path(temp_dir) / "offline"
            online.mkdir()
            window = self._make_window(Path(temp_dir) / "folder_index.sqlite3")
            navigated: list[str] = []
            try:
                window._folder_index_repository.add_favorite(str(online))
                window._folder_index_repository.add_favorite(str(offline))
                window._reload_favorite_roots(selected_path=str(online))
                window.jump_to_path = (  # type: ignore[method-assign]
                    lambda path, show_feedback=True: navigated.append(path) or True
                )

                window._on_favorite_root_clicked(window.favorite_root_list.item(0))

                self.assertEqual(navigated, [str(online)])
                self.assertIn("오프라인", window.favorite_root_list.item(1).text())
                self.assertTrue(window._folder_periodic_refresh.isActive())
                self.assertEqual(window._folder_periodic_refresh.interval(), 5 * 60 * 1000)
                self.assertIn(str(online), window._favorite_root_watcher.directories())
            finally:
                window.close()

    def test_missing_search_result_is_verified_before_navigation(self) -> None:
        window = self._make_window()
        errors: list[str] = []
        try:
            item = QListWidgetItem("missing")
            item.setData(Qt.UserRole, r"Z:\missing\target")
            item.setData(Qt.UserRole + 1, r"Z:\missing")
            window._show_path_error = errors.append  # type: ignore[method-assign]
            window._on_folder_search_result_clicked(item)

            self.assertEqual(len(errors), 1)
            self.assertEqual(window._folder_index_repository.search("target"), [])
        finally:
            window.close()

    def test_status_sidebar_defaults_to_log_tab(self) -> None:
        window = self._make_window()
        try:
            self.assertEqual(window.status_tabs.currentIndex(), window.STATUS_TAB_LOG)
        finally:
            window.close()

    def test_duplicate_folder_dialog_renders_path_and_location_table(self) -> None:
        dialog = DuplicateFolderDialog(
            [
                (r"D:\data\pending", "진행중/대기 폴더"),
                (r"D:\data\done", "완료된 폴더"),
            ]
        )
        try:
            self.assertEqual(dialog.windowTitle(), "중복 폴더+Recipe 안내")
            self.assertEqual(dialog.table.rowCount(), 2)
            self.assertEqual(dialog.table.columnCount(), 2)
            self.assertEqual(dialog.table.item(0, 0).text(), r"D:\data\pending")
            self.assertEqual(dialog.table.item(0, 1).text(), "진행중/대기 폴더")
            self.assertEqual(dialog.table.item(1, 1).text(), "완료된 폴더")
        finally:
            dialog.close()

    def test_preflight_dialog_blocks_start_when_broker_is_unavailable(self) -> None:
        report = {
            "folder_count": 2,
            "held_folder_count": 0,
            "recipe_count": 1,
            "image_count": 6000,
            "message_count": 12000,
            "dispatchable_message_count": 12000,
            "missing_recipes": ["missing.json"],
            "inaccessible_folders": [],
            "inaccessible_image_count": 0,
            "broker_connected": False,
            "request_queue": "request.queue",
            "priority": 2,
            "initial_open_folders": 1,
            "max_active_open_folders": 3,
            "warning_threshold": 10000,
            "threshold_exceeded": True,
            "issues": ["RabbitMQ 연결 실패"],
        }
        dialog = PreflightDialog(report)
        try:
            self.assertEqual(dialog.windowTitle(), "전송 전 사전 점검")
            self.assertEqual(dialog.summary_table.rowCount(), 10)
            self.assertFalse(dialog.start_button.isEnabled())
        finally:
            dialog.close()

    def test_run_history_dialog_lists_aggregates_and_export_selection(self) -> None:
        row = RunHistorySummary(
            session_id="session-1",
            state="COMPLETED",
            created_at="2026-08-13T01:00:00+00:00",
            ended_at="2026-08-13T02:00:00+00:00",
            folder_count=2,
            recipe_count=1,
            total=10,
            success=8,
            fail=1,
            timeout=1,
            error=0,
            cancelled=0,
        )
        dialog = RunHistoryDialog([row])
        exported: list[str] = []
        dialog.export_requested.connect(exported.append)
        try:
            self.assertEqual(dialog.table.rowCount(), 1)
            self.assertEqual(dialog.table.item(0, 0).text(), "2026-08-13 10:00:00.0")
            self.assertEqual(dialog.table.item(0, 1).text(), "2026-08-13 11:00:00.0")
            self.assertEqual(dialog.table.item(0, 8).text(), "80.0%")
            dialog.export_button.click()
            self.assertEqual(exported, ["session-1"])
        finally:
            dialog.close()

    def test_paused_state_uses_explicit_resume_label(self) -> None:
        window = self._make_window()
        try:
            window.set_paused_state(True)
            self.assertEqual(window.btn_start.text(), "전송 재개")
            self.assertTrue(window.btn_start.isEnabled())
            self.assertFalse(window.btn_stop.isEnabled())
            self.assertFalse(window.recipe_multi_button.isEnabled())
        finally:
            window.close()

    def test_pending_folder_controls_emit_move_and_hold_requests(self) -> None:
        window = self._make_window()
        moves: list[tuple[list[str], str]] = []
        holds: list[tuple[list[str], bool]] = []
        window.move_folders_requested.connect(lambda paths, operation: moves.append((paths, operation)))
        window.hold_folders_requested.connect(lambda paths, held: holds.append((paths, held)))
        try:
            window.show()
            self._app.processEvents()
            window.set_folder_rows(
                [
                    FolderSummary(
                        folder_path="first",
                        total=1,
                        completed=0,
                        success=0,
                        fail=0,
                        timeout=0,
                        error=0,
                        progress=0.0,
                        status=TaskStatus.PENDING,
                    ),
                    FolderSummary(
                        folder_path="second",
                        total=1,
                        completed=0,
                        success=0,
                        fail=0,
                        timeout=0,
                        error=0,
                        progress=0.0,
                        status=TaskStatus.PENDING,
                    ),
                ]
            )
            selected_index = window.active_folder_table_model.index(1, 0)
            selection_model = window.active_folder_table.selectionModel()
            selection_model.setCurrentIndex(
                selected_index,
                QItemSelectionModel.ClearAndSelect
                | QItemSelectionModel.Rows
                | QItemSelectionModel.Current,
            )
            self._app.processEvents()
            self.assertEqual(
                window._selected_folder_paths_from_table(
                    window.active_folder_table,
                    window.active_folder_table_model,
                ),
                ["second"],
            )
            for button in (
                window.btn_move_folder_top,
                window.btn_move_folder_bottom,
                window.btn_hold_folders,
                window.btn_release_folders,
            ):
                button.setEnabled(True)
            expected_labels = (
                (window.btn_move_folder_top, "맨 위로 이동"),
                (window.btn_move_folder_up, "위로 이동"),
                (window.btn_move_folder_down, "아래로 이동"),
                (window.btn_move_folder_bottom, "맨 아래로 이동"),
                (window.btn_hold_folders, "보류"),
                (window.btn_release_folders, "보류 해제"),
            )
            for button, label in expected_labels:
                self.assertEqual(button.text(), "")
                self.assertFalse(button.icon().isNull())
                self.assertEqual(button.toolTip(), label)
                self.assertEqual(button.accessibleName(), label)
            window.btn_move_folder_top.click()
            window.btn_move_folder_bottom.click()
            window.btn_hold_folders.click()
            window.btn_release_folders.click()

            self.assertEqual(moves, [(["second"], "top"), (["second"], "bottom")])
            self.assertEqual(holds, [(["second"], True), (["second"], False)])
        finally:
            window.close()

    def test_log_document_is_bounded_by_configuration(self) -> None:
        window = self._make_window()
        try:
            self.assertEqual(
                window.log_text.document().maximumBlockCount(),
                window._config.publish.ui_log_max_lines,
            )
        finally:
            window.close()

    def test_overall_progress_hides_partial_ratio_until_total_is_final(self) -> None:
        window = self._make_window()
        try:
            window.set_overall_stats(
                {
                    "total": 20,
                    "total_final": False,
                    "completed": 5,
                    "progress": 25.0,
                }
            )

            self.assertEqual(window.overall_progress.value(), 0)
            self.assertIn("전체 모수 산정 중", window.overall_label.text())

            window.set_overall_stats(
                {
                    "total": 20,
                    "total_final": True,
                    "completed": 5,
                    "progress": 25.0,
                }
            )
            self.assertEqual(window.overall_progress.value(), 25)
            self.assertIn("전체 진행률 25.0% (5/20)", window.overall_label.text())
        finally:
            window.close()

    def test_window_uses_ipdk_branding_and_removes_drive_combo(self) -> None:
        window = self._make_window()
        try:
            self.assertEqual(window.windowTitle(), "IPDK_plus")
            self.assertFalse(hasattr(window, "brand_icon_label"))
            self.assertFalse(hasattr(window, "drive_combo"))
            self.assertFalse(hasattr(window, "action_edit"))
            self.assertFalse(hasattr(window, "polling_combo"))
            self.assertEqual(window.btn_add_subfolders.text(), "하위 폴더 추가")
            self.assertFalse(window.folder_tree.rootIndex().isValid())
            self.assertTrue(window.folder_tree.isHeaderHidden())
            self.assertTrue(hasattr(window, "main_splitter"))
            self.assertEqual(window.folder_tree.horizontalScrollBarPolicy(), Qt.ScrollBarAsNeeded)
            self.assertEqual(window.folder_tree.textElideMode(), Qt.ElideNone)
            self.assertIn("\n", window.connection_label.text())
            self.assertIn("127.0.0.1:5672", window.connection_label.text())
            self.assertIn("request_queue: task.request", window.connection_label.text())
        finally:
            window.close()

    def test_help_menu_action_opens_searchable_help_dialog(self) -> None:
        window = self._make_window()
        try:
            self.assertTrue(hasattr(window, "action_open_help"))
            self.assertEqual(window.action_open_help.text(), "도움말 열기")
            self.assertEqual(window.action_open_help.shortcut().toString(), "F1")
            self.assertTrue(hasattr(window, "action_check_update"))
            self.assertEqual(window.action_check_update.text(), "업데이트 확인")

            window.show()
            self._app.processEvents()
            QTest.keyClick(window, Qt.Key_F1)
            self._app.processEvents()

            self.assertIsNotNone(window._help_dialog)
            dialog = window._help_dialog
            self.assertTrue(dialog.isVisible())
            self.assertGreaterEqual(dialog.topic_list.count(), 11)

            dialog.search_edit.setText("RabbitMQ")
            self._app.processEvents()
            self.assertRegex(dialog.match_count_label.text(), r"^\d+ / \d+$")

            first_count = dialog.match_count_label.text()
            QTest.keyClick(dialog.search_edit, Qt.Key_Return)
            self._app.processEvents()
            self.assertNotEqual(dialog.match_count_label.text(), first_count)

            QTest.keyClick(dialog.search_edit, Qt.Key_Return, Qt.ShiftModifier)
            self._app.processEvents()
            self.assertEqual(dialog.match_count_label.text(), first_count)

            window.action_open_help.trigger()
            self._app.processEvents()
            self.assertIs(window._help_dialog, dialog)
        finally:
            window.close()

    def test_active_folder_single_selection_switches_to_detail_tab(self) -> None:
        window = self._make_window()
        selected_paths: list[str] = []
        window.folder_row_selected.connect(selected_paths.append)
        try:
            window.set_folder_rows(
                [
                    FolderSummary(
                        folder_path="folder_a",
                        total=10,
                        completed=2,
                        success=2,
                        fail=0,
                        timeout=0,
                        error=0,
                        progress=20.0,
                        status=TaskStatus.RUNNING,
                    )
                ]
            )
            window.status_tabs.setCurrentIndex(window.STATUS_TAB_LOG)

            window.active_folder_table.selectRow(0)
            self._app.processEvents()

            self.assertEqual(window.status_tabs.currentIndex(), window.STATUS_TAB_DETAIL)
            self.assertEqual(selected_paths, ["folder_a"])
        finally:
            window.close()

    def test_status_sidebar_toggle_button_collapses_and_expands_panel(self) -> None:
        window = self._make_window()
        try:
            window.show()
            self._app.processEvents()

            self.assertTrue(window.status_sidebar_panel.isVisible())
            self.assertTrue(window.status_tabs.isVisible())
            self.assertEqual(window.btn_toggle_sidebar.text(), "")
            self.assertTrue(window.btn_toggle_sidebar.autoRaise())
            self.assertFalse(window.status_sidebar_panel.isAncestorOf(window.btn_toggle_sidebar))
            self.assertTrue(window.center_panel.isAncestorOf(window.btn_toggle_sidebar))

            window.btn_toggle_sidebar.setChecked(True)
            self._app.processEvents()
            self.assertFalse(window.status_sidebar_panel.isVisible())
            self.assertFalse(window.status_tabs.isVisible())

            window.btn_toggle_sidebar.setChecked(False)
            self._app.processEvents()
            self.assertTrue(window.status_sidebar_panel.isVisible())
            self.assertTrue(window.status_tabs.isVisible())
        finally:
            window.close()

    def test_copy_folder_paths_to_clipboard(self) -> None:
        window = self._make_window()
        try:
            window._copy_folder_paths_to_clipboard(["folder_a", "folder_b"])
            clipboard_text = self._app.clipboard().text()
            self.assertEqual(clipboard_text, "folder_a\nfolder_b")
        finally:
            window.close()

    def test_initial_horizontal_scrollbars_are_aligned_to_left(self) -> None:
        window = self._make_window()
        try:
            window.show()
            self._app.processEvents()
            self._app.processEvents()

            widgets = [
                window.active_folder_table,
                window.completed_folder_table,
                window.image_table,
                window.log_text,
            ]
            for widget in widgets:
                scrollbar = widget.horizontalScrollBar()
                self.assertEqual(scrollbar.value(), scrollbar.minimum())
        finally:
            window.close()

    def test_horizontal_alignment_reset_preserves_tree_context(self) -> None:
        window = self._make_window()
        try:
            window.folder_tree.setColumnWidth(0, 1200)
            window.show()
            self._app.processEvents()
            scrollbar = window.folder_tree.horizontalScrollBar()
            self.assertGreater(scrollbar.maximum(), scrollbar.minimum())
            scrollbar.setValue(scrollbar.maximum())

            window._reset_horizontal_scrollbars_to_left()

            self.assertEqual(scrollbar.value(), scrollbar.maximum())
        finally:
            window.close()

    def test_deep_tree_index_is_centered_horizontally(self) -> None:
        anchor = MainWindow._calculate_tree_horizontal_anchor(
            depth=8,
            indentation=22,
            label_width=80,
        )
        scroll_value = MainWindow._calculate_centered_tree_scroll_value(
            anchor=anchor,
            viewport_width=280,
            minimum=0,
            maximum=900,
        )

        self.assertEqual(anchor, 266)
        self.assertEqual(scroll_value, 126)
        self.assertEqual(anchor - scroll_value, 140)

    def test_tree_ensure_visible_preserves_managed_horizontal_position(self) -> None:
        tree = FolderTreeView()
        model = QStandardItemModel(tree)
        parent = model.invisibleRootItem()
        target = None
        for depth in range(10):
            target = QStandardItem(f"Level {depth}")
            parent.appendRow(target)
            parent = target

        tree.setModel(model)
        tree.setColumnWidth(0, 620)
        tree.resize(280, 320)
        tree.expandAll()
        tree.show()
        self._app.processEvents()

        try:
            scrollbar = tree.horizontalScrollBar()
            self.assertGreater(scrollbar.maximum(), scrollbar.minimum())
            expected_value = min(180, scrollbar.maximum())
            scrollbar.setValue(expected_value)

            tree.scrollTo(target.index(), QAbstractItemView.EnsureVisible)

            self.assertEqual(scrollbar.value(), expected_value)
        finally:
            tree.close()

    def test_set_runtime_options_enabled_toggles_recipe_and_priority(self) -> None:
        window = self._make_window()
        try:
            window.set_runtime_options_enabled(False)
            self.assertFalse(window.recipe_multi_button.isEnabled())
            self.assertFalse(window.priority_combo.isEnabled())

            window.set_runtime_options_enabled(True)
            self.assertTrue(window.recipe_multi_button.isEnabled())
            self.assertTrue(window.priority_combo.isEnabled())
        finally:
            window.close()

    def test_multi_recipe_selector_returns_all_checked_recipes(self) -> None:
        window = self._make_window()
        try:
            self.assertEqual(window.current_recipe_selections(), [("Recipe A", "recipes/a.json")])
            self.assertFalse(hasattr(window, "recipe_combo"))
            self.assertFalse(hasattr(window, "recipe_multi_check"))

            window._recipe_actions[1].trigger()

            self.assertEqual(
                window.current_recipe_selections(),
                [("Recipe A", "recipes/a.json"), ("Recipe B", "recipes/b.json")],
            )
            self.assertEqual(window.recipe_multi_button.text(), "Recipe 2개 선택")
            self.assertEqual(
                [(row.alias, row.path) for row in window.recipe_path_rows],
                [("Recipe A", "recipes/a.json"), ("Recipe B", "recipes/b.json")],
            )

            window.set_runtime_options_enabled(False)
            self.assertFalse(window.recipe_multi_button.isEnabled())
        finally:
            window.close()

    def test_recipe_selector_supports_empty_and_multiple_states(self) -> None:
        window = self._make_window()
        try:
            window._recipe_actions[0].trigger()
            self.assertEqual(window.current_recipe_selections(), [])
            self.assertEqual(window.recipe_multi_button.text(), "Recipe 선택")
            self.assertTrue(window.recipe_path_empty_label.isVisibleTo(window.recipe_paths_panel))
            self.assertEqual(window.recipe_path_rows, [])

            for action in window._recipe_actions:
                if not action.isChecked():
                    action.trigger()
            self.assertEqual(len(window.current_recipe_selections()), 2)
            self.assertEqual(window.recipe_multi_button.text(), "Recipe 2개 선택")
            self.assertFalse(window.recipe_path_empty_label.isVisible())
        finally:
            window.close()

    def test_recipe_selection_survives_refresh_and_drops_removed_recipe(self) -> None:
        window = self._make_window()
        try:
            window._recipe_actions[1].trigger()
            window._populate_recipe_selector()
            self.assertEqual(len(window.current_recipe_selections()), 2)

            window._config.recipe_config.recipes = [
                RecipeItem(alias="Recipe B", path="recipes/b.json")
            ]
            window._populate_recipe_selector()
            self.assertEqual(
                window.current_recipe_selections(),
                [("Recipe B", "recipes/b.json")],
            )
        finally:
            window.close()

    def test_recipe_selector_ignores_duplicate_paths(self) -> None:
        window = self._make_window()
        try:
            window._config.recipe_config.recipes.append(
                RecipeItem(alias="Recipe A Duplicate", path="recipes/a.json")
            )
            window._populate_recipe_selector()
            for action in window._recipe_actions:
                if not action.isChecked():
                    action.trigger()

            self.assertEqual(len(window._recipe_actions), 2)
            self.assertEqual(
                window.current_recipe_selections(),
                [("Recipe A", "recipes/a.json"), ("Recipe B", "recipes/b.json")],
            )
        finally:
            window.close()

    def test_recipe_and_priority_layout_reflows_without_overlap(self) -> None:
        settings = ResponsiveRecipeSettings(
            QLabel("Recipe"),
            QToolButton(),
            QLabel("Priority"),
            QComboBox(),
        )
        try:
            settings.show()
            for width in (560, 700, 900):
                settings.resize(width, 100)
                self._app.processEvents()
                self.assertFalse(settings.is_compact)
                self.assertFalse(
                    settings.recipe_selector.geometry().intersects(
                        settings.priority_selector.geometry()
                    )
                )
                control_gap = (
                    settings.priority_label.geometry().left()
                    - settings.recipe_selector.geometry().right()
                )
                self.assertGreaterEqual(control_gap, 14)
                self.assertLessEqual(control_gap, 18)

            settings.resize(480, 100)
            self._app.processEvents()
            self.assertTrue(settings.is_compact)
            self.assertGreater(
                settings.priority_selector.geometry().top(),
                settings.recipe_selector.geometry().top(),
            )
        finally:
            settings.close()

    def test_recipe_selector_uses_compact_combo_width_and_single_alias(self) -> None:
        window = self._make_window()
        try:
            window.show()
            self._app.processEvents()

            self.assertEqual(window.recipe_multi_button.text(), "Recipe A")
            self.assertGreaterEqual(window.recipe_multi_button.width(), 220)
            self.assertLessEqual(window.recipe_multi_button.width(), 280)
            self.assertEqual(
                window.recipe_multi_button.height(),
                window.priority_combo.height(),
            )
            self.assertEqual(window.priority_combo.objectName(), "priorityCombo")
            self.assertIn("combo_down.svg", window.priority_combo.styleSheet())
            self.assertIn("margin-right: 5px", window.priority_combo.styleSheet())
            self.assertNotEqual(
                window.recipe_multi_button.sizePolicy().horizontalPolicy(),
                QSizePolicy.Expanding,
            )
        finally:
            window.close()

    def test_recipe_menu_contains_only_individual_recipe_actions(self) -> None:
        window = self._make_window()
        try:
            menu = window.recipe_multi_button.menu()
            self.assertIsNotNone(menu)
            self.assertEqual(
                [action.text() for action in menu.actions()],
                ["Recipe A", "Recipe B"],
            )
            self.assertTrue(all(action.isCheckable() for action in menu.actions()))
        finally:
            window.close()

    def test_recipe_path_panel_keeps_multiple_rows_separate(self) -> None:
        window = self._make_window()
        try:
            window.show()
            window._recipe_actions[1].trigger()
            self._app.processEvents()
            self._app.processEvents()

            self.assertEqual(len(window.recipe_path_rows), 2)
            first, second = window.recipe_path_rows
            self.assertFalse(first.geometry().intersects(second.geometry()))
            self.assertGreaterEqual(
                window.recipe_paths_panel.height(),
                first.height() + second.height() + window.recipe_paths_layout.spacing(),
            )
            self.assertLessEqual(window.recipe_paths_scroll.height(), 185)
        finally:
            window.close()

    def test_recipe_path_row_reflows_and_elides_long_path(self) -> None:
        long_path = "D:/" + "/very-long-folder" * 20 + "/recipe.json"
        row = RecipePathRow("Very Long Recipe Alias", long_path)
        try:
            row.show()
            row.resize(600, 50)
            self._app.processEvents()
            self.assertFalse(row.is_compact)
            self.assertFalse(row.path_label.geometry().intersects(row.alias_label.geometry()))
            self.assertGreaterEqual(
                row.path_label.geometry().left(),
                row.alias_label.geometry().right(),
            )

            row.resize(380, 80)
            self._app.processEvents()
            self._app.processEvents()
            self.assertTrue(row.is_compact)
            self.assertGreater(row.path_label.geometry().top(), row.alias_label.geometry().top())
            self.assertFalse(row.path_label.geometry().intersects(row.alias_label.geometry()))
            self.assertGreaterEqual(
                row.height(),
                row.alias_label.height() + row.path_label.height() + 3,
            )
            self.assertEqual(row.path_label.toolTip(), long_path)
            self.assertIn("…", row.path_label.text())
        finally:
            row.close()


if __name__ == "__main__":
    unittest.main()
