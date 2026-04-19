"""Main GUI window for task registration, control, and tracking."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from PySide6.QtCore import QDir, QItemSelectionModel, QModelIndex, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QIcon, QShowEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QToolButton,
    QDialog,
    QFileSystemModel,
    QFrame,
    QGroupBox,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTabWidget,
    QTableView,
    QTextEdit,
    QTreeView,
    QVBoxLayout,
    QWidget,
    QComboBox,
    QProgressBar,
    QDialogButtonBox,
    QPlainTextEdit,
)

from app.runtime_paths import resolve_ui_icon_path
from config.models import AppConfig
from models.task_models import FolderSummary, ImageTask
from ui.models import FolderTableModel, ImageTableModel, ProgressBarDelegate
from ui.widgets import MQButtonDelegate, StatusBadgeDelegate


class MQPreviewDialog(QDialog):
    """Dialog displaying connection info and message payload previews."""

    def __init__(self, preview_data: dict[str, Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("MQ 미리보기")
        self.resize(920, 680)

        layout = QVBoxLayout(self)

        header = QLabel("선택한 작업의 MQ 연결/메시지 정보를 확인합니다.")
        layout.addWidget(header)

        self._text = QPlainTextEdit(self)
        self._text.setReadOnly(True)
        self._text.setPlainText(self._format_preview(preview_data))
        layout.addWidget(self._text, stretch=1)

        buttons = QDialogButtonBox(QDialogButtonBox.Close, parent=self)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

    @staticmethod
    def _format_preview(preview_data: dict[str, Any]) -> str:
        """Convert preview dictionary into readable block text."""

        sections = [
            "=== Connection ===",
            json.dumps(preview_data.get("connection", {}), ensure_ascii=False, indent=2),
            "",
            "=== Message ===",
            json.dumps(preview_data.get("message", {}), ensure_ascii=False, indent=2),
            "",
            "=== Message (Received Meta / 수신 메타) ===",
            json.dumps(
                preview_data.get("message", {}).get("received_meta", {}),
                ensure_ascii=False,
                indent=2,
            ),
            "",
            "=== Payload (Expected / 현재 선택 기준 예상값) ===",
            json.dumps(preview_data.get("payload", {}).get("expected", {}), ensure_ascii=False, indent=2),
            "",
            "=== Payload (Published / 실제 전송값) ===",
            json.dumps(preview_data.get("payload", {}).get("published", {}), ensure_ascii=False, indent=2),
            "",
            "=== Payload (Received Raw / 매칭 원본 응답) ===",
            json.dumps(preview_data.get("payload", {}).get("received", {}), ensure_ascii=False, indent=2),
        ]
        return "\n".join(sections)


class MainWindow(QMainWindow):
    """Main application window with modern, operator-friendly layout."""

    STATUS_TAB_DETAIL = 0
    STATUS_TAB_LOG = 1

    add_folder_requested = Signal(list)
    add_subfolders_requested = Signal(list)
    delete_folders_requested = Signal(list)
    clear_requested = Signal()
    start_requested = Signal()
    stop_requested = Signal()
    reset_requested = Signal()
    folder_row_selected = Signal(str)
    mq_preview_requested = Signal(str)

    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self._config = config
        self._is_syncing_navigation = False
        self._active_result_queue: str | None = None
        self._is_syncing_folder_selection = False
        self._pending_jump_target: str | None = None
        self._pending_jump_show_feedback = False
        self._pending_jump_attempts = 0
        self._max_pending_jump_attempts = 10
        self._initial_scroll_alignment_done = False
        self._last_status_sidebar_width = 420
        self._build_ui()
        self._apply_defaults()

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802
        """Align horizontal scroll positions once when the main window is first shown."""

        super().showEvent(event)
        self._apply_initial_scroll_alignment_once()

    def _build_ui(self) -> None:
        self.setWindowTitle(self._config.ui.app_name)
        self.resize(self._config.ui.window_width, self._config.ui.window_height)

        root = QWidget(self)
        self.setCentralWidget(root)

        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(16, 16, 16, 16)
        root_layout.setSpacing(14)

        left_panel = self._build_left_panel()
        center_panel = self._build_center_panel()
        status_sidebar = self._build_status_sidebar()

        left_panel.setMinimumWidth(280)
        center_panel.setMinimumWidth(560)
        status_sidebar.setMinimumWidth(300)

        self.main_splitter = QSplitter(Qt.Horizontal)
        self.main_splitter.setObjectName("mainSplitter")
        self.main_splitter.setChildrenCollapsible(False)
        self.main_splitter.addWidget(left_panel)
        self.main_splitter.addWidget(center_panel)
        self.main_splitter.addWidget(status_sidebar)
        self.main_splitter.setStretchFactor(0, 3)
        self.main_splitter.setStretchFactor(1, 8)
        self.main_splitter.setStretchFactor(2, 5)
        self.main_splitter.setSizes([360, 900, self._last_status_sidebar_width])

        self.left_panel = left_panel
        self.center_panel = center_panel
        self.status_sidebar_panel = status_sidebar

        root_layout.addWidget(self.main_splitter, stretch=1)

    def _build_left_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("leftPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(14, 12, 14, 14)
        layout.setSpacing(10)

        title = QLabel("폴더 탐색")
        title.setObjectName("panelTitle")
        layout.addWidget(title)

        jump_row = QHBoxLayout()
        self.path_jump_edit = QLineEdit()
        self.path_jump_edit.setPlaceholderText(r"경로 입력 (예: D:\data\set1)")
        self.path_jump_edit.returnPressed.connect(self._on_path_jump_requested)
        self.btn_path_jump = QPushButton("이동")
        self.btn_path_jump.clicked.connect(self._on_path_jump_requested)
        jump_row.addWidget(self.path_jump_edit, stretch=1)
        jump_row.addWidget(self.btn_path_jump, stretch=0)
        layout.addLayout(jump_row)

        self.folder_tree = QTreeView()
        self.folder_tree.setObjectName("folderTree")
        self.folder_tree.setHeaderHidden(True)
        self.folder_tree.setAnimated(True)
        self.folder_tree.setIndentation(22)
        self.folder_tree.setUniformRowHeights(True)
        self.folder_tree.setAllColumnsShowFocus(True)
        self.folder_tree.setAlternatingRowColors(True)
        self.folder_tree.setSelectionBehavior(QTreeView.SelectRows)
        self.folder_tree.setSelectionMode(QTreeView.ExtendedSelection)
        self.folder_tree.setDragDropMode(QTreeView.NoDragDrop)
        self.folder_tree.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.folder_tree.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.folder_tree.setTextElideMode(Qt.ElideNone)

        self.file_system_model = QFileSystemModel(self.folder_tree)
        self.file_system_model.setRootPath("")
        self.file_system_model.setFilter(QDir.AllDirs | QDir.NoDotAndDotDot | QDir.Drives)
        self.file_system_model.directoryLoaded.connect(self._on_directory_loaded)

        self.folder_tree.setModel(self.file_system_model)
        for col in range(1, 4):
            self.folder_tree.hideColumn(col)
        self.folder_tree.setColumnWidth(0, 520)
        if self.folder_tree.selectionModel():
            self.folder_tree.selectionModel().currentChanged.connect(self._on_tree_current_changed)

        self.folder_tree.setRootIndex(QModelIndex())
        self.jump_to_path(str(Path.home()), show_feedback=False)

        layout.addWidget(self.folder_tree, stretch=1)

        self.btn_add_folder = QPushButton("폴더 추가")
        self.btn_add_subfolders = QPushButton("sub_folder 추가")
        self.btn_clear_selection = QPushButton("선택 해제")

        self.btn_add_folder.clicked.connect(self._on_add_folder_clicked)
        self.btn_add_subfolders.clicked.connect(self._on_add_subfolders_clicked)
        self.btn_clear_selection.clicked.connect(self._on_clear_clicked)

        layout.addWidget(self.btn_add_folder)
        layout.addWidget(self.btn_add_subfolders)
        layout.addWidget(self.btn_clear_selection)

        return panel

    def _build_center_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        layout.addWidget(self._build_control_panel())
        layout.addWidget(self._build_folder_progress_panel(), stretch=1)

        return panel

    def _build_status_sidebar(self) -> QWidget:
        """Build right-side status/log sidebar that spans full window height."""

        panel = QFrame()
        panel.setObjectName("statusSidebar")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._build_bottom_panel())
        return panel

    def _build_control_panel(self) -> QWidget:
        panel = QGroupBox("작업 설정 및 제어")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        connection_row = QHBoxLayout()
        connection_row.setSpacing(10)

        self.connection_label = QLabel(self._build_connection_status_text("대기"))
        self.connection_label.setObjectName("connectionStatus")
        connection_row.addWidget(self.connection_label, stretch=1)

        self.btn_toggle_sidebar = QToolButton(panel)
        self.btn_toggle_sidebar.setObjectName("statusSidebarToggle")
        self.btn_toggle_sidebar.setToolButtonStyle(Qt.ToolButtonIconOnly)
        self.btn_toggle_sidebar.setAutoRaise(True)
        self.btn_toggle_sidebar.setCheckable(True)
        self.btn_toggle_sidebar.setIconSize(QSize(18, 18))
        self.btn_toggle_sidebar.toggled.connect(self._on_toggle_status_sidebar)
        self.btn_toggle_sidebar.setAccessibleName("상태/로그 사이드바 토글")
        connection_row.addWidget(self.btn_toggle_sidebar, stretch=0, alignment=Qt.AlignRight)
        self._update_status_sidebar_toggle_icon(collapsed=False)
        layout.addLayout(connection_row)

        self.queue_metrics_label = QLabel("Worker Count: -    Queued Messages: -")
        self.queue_metrics_label.setObjectName("queueMetricsLabel")
        layout.addWidget(self.queue_metrics_label)

        row_recipe_priority = QHBoxLayout()

        recipe_col = QHBoxLayout()
        recipe_col.addWidget(QLabel("Recipe"), stretch=0)
        self.recipe_combo = QComboBox()
        self.recipe_combo.currentIndexChanged.connect(self._on_recipe_changed)
        self.recipe_combo.setMinimumWidth(220)
        recipe_col.addWidget(self.recipe_combo, stretch=1)

        priority_col = QHBoxLayout()
        priority_col.addWidget(QLabel("Priority"), stretch=0)
        self.priority_combo = QComboBox()
        self.priority_combo.setMinimumContentsLength(3)
        self.priority_combo.setMinimumWidth(92)
        priority_col.addWidget(self.priority_combo, stretch=0)
        priority_col.addStretch(1)

        row_recipe_priority.addLayout(recipe_col, stretch=1)
        row_recipe_priority.addSpacing(12)
        row_recipe_priority.addLayout(priority_col, stretch=1)
        layout.addLayout(row_recipe_priority)

        recipe_path_row = QHBoxLayout()
        recipe_path_row.addWidget(QLabel("선택 경로"), stretch=0)
        self.recipe_path_preview = QLineEdit()
        self.recipe_path_preview.setReadOnly(True)
        recipe_path_row.addWidget(self.recipe_path_preview, stretch=1)
        layout.addLayout(recipe_path_row)

        self.overall_progress = QProgressBar()
        self.overall_progress.setRange(0, 100)
        self.overall_progress.setValue(0)
        self.overall_label = QLabel("전체 진행률 0.0% (0/0)")
        layout.addWidget(self.overall_progress)
        layout.addWidget(self.overall_label)

        button_row = QHBoxLayout()
        self.btn_start = QPushButton("전송 시작")
        self.btn_stop = QPushButton("중지")
        self.btn_reset = QPushButton("초기화")
        self.btn_start.clicked.connect(self.start_requested.emit)
        self.btn_stop.clicked.connect(self.stop_requested.emit)
        self.btn_reset.clicked.connect(self.reset_requested.emit)
        button_row.addWidget(self.btn_start)
        button_row.addWidget(self.btn_stop)
        button_row.addWidget(self.btn_reset)
        layout.addLayout(button_row)

        return panel

    def _build_folder_progress_panel(self) -> QWidget:
        panel = QGroupBox("폴더 단위 진행 현황")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        self.active_folder_table_model = FolderTableModel()
        self.completed_folder_table_model = FolderTableModel()
        # Keep backward-compatible attribute names for existing references.
        self.folder_table_model = self.active_folder_table_model

        active_label = QLabel("진행중/대기 폴더")
        active_label.setObjectName("subPanelTitle")
        layout.addWidget(active_label)

        self.active_folder_table = self._create_folder_table(
            self.active_folder_table_model,
            selection_mode=QTableView.ExtendedSelection,
        )
        # Keep active list visibly larger from first render as requested.
        self.active_folder_table.setMinimumHeight(190)
        self.active_folder_table.selectionModel().selectionChanged.connect(self._on_active_folder_selection_changed)
        self.active_folder_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.active_folder_table.customContextMenuRequested.connect(self._on_active_folder_context_menu)
        # Keep backward-compatible attribute name.
        self.folder_table = self.active_folder_table
        layout.addWidget(self.active_folder_table, stretch=11)

        action_row = QHBoxLayout()
        action_row.addStretch(1)
        self.btn_delete_active_folders = QPushButton("선택 삭제")
        self.btn_delete_active_folders.setEnabled(False)
        self.btn_delete_active_folders.clicked.connect(self._on_delete_active_folders_clicked)
        action_row.addWidget(self.btn_delete_active_folders)
        layout.addLayout(action_row)

        completed_label = QLabel("완료된 폴더")
        completed_label.setObjectName("subPanelTitle")
        layout.addWidget(completed_label)

        self.completed_folder_table = self._create_folder_table(self.completed_folder_table_model)
        # Preserve completed-list readability without stealing too much initial height.
        self.completed_folder_table.setMinimumHeight(140)
        self.completed_folder_table.selectionModel().selectionChanged.connect(
            self._on_completed_folder_selection_changed
        )
        self.completed_folder_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.completed_folder_table.customContextMenuRequested.connect(self._on_completed_folder_context_menu)
        layout.addWidget(self.completed_folder_table, stretch=9)
        return panel

    def _create_folder_table(
        self,
        model: FolderTableModel,
        selection_mode: QAbstractItemView.SelectionMode = QTableView.SingleSelection,
    ) -> QTableView:
        """Create one folder table with shared visual/column policy."""

        table = QTableView()
        table.setModel(model)
        table.setSelectionBehavior(QTableView.SelectRows)
        table.setSelectionMode(selection_mode)
        table.setAlternatingRowColors(True)
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(34)
        table.verticalHeader().setMinimumSectionSize(30)
        table.setWordWrap(False)
        table.setTextElideMode(Qt.ElideRight)
        table.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        table.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        table.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)

        header = table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(QHeaderView.ResizeToContents)
        header.setResizeContentsPrecision(-1)
        header.setMinimumSectionSize(56)
        header.setSectionResizeMode(0, QHeaderView.Interactive)
        header.setSectionResizeMode(1, QHeaderView.Interactive)
        table.setColumnWidth(0, 190)
        table.setColumnWidth(1, 120)
        table.setItemDelegateForColumn(0, ProgressBarDelegate(table))
        table.setItemDelegateForColumn(1, StatusBadgeDelegate(table))
        return table

    def _build_bottom_panel(self) -> QWidget:
        panel = QGroupBox("상태 및 로그")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        self.status_tabs = QTabWidget()
        self.status_tabs.setObjectName("bottomTabs")

        self.image_table_model = ImageTableModel()
        self.image_table = QTableView()
        self.image_table.setModel(self.image_table_model)
        self.image_table.setSelectionBehavior(QTableView.SelectRows)
        self.image_table.setAlternatingRowColors(True)
        self.image_table.verticalHeader().setVisible(False)
        self.image_table.verticalHeader().setDefaultSectionSize(34)
        self.image_table.verticalHeader().setMinimumSectionSize(30)
        self.image_table.setWordWrap(False)
        self.image_table.setTextElideMode(Qt.ElideRight)
        self.image_table.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.image_table.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.image_table.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)

        image_header = self.image_table.horizontalHeader()
        image_header.setStretchLastSection(False)
        image_header.setSectionResizeMode(QHeaderView.ResizeToContents)
        image_header.setResizeContentsPrecision(-1)
        image_header.setMinimumSectionSize(70)

        self._mq_button_delegate = MQButtonDelegate(self.image_table)
        self._mq_button_delegate.clicked.connect(self._on_mq_button_clicked)
        self.image_table.setItemDelegateForColumn(0, self._mq_button_delegate)
        self.image_table.setItemDelegateForColumn(2, StatusBadgeDelegate(self.image_table))

        detail_tab = QWidget()
        detail_layout = QVBoxLayout(detail_tab)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.addWidget(self.image_table)

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setPlaceholderText("작업 로그가 여기에 표시됩니다.")
        self.log_text.setLineWrapMode(QTextEdit.NoWrap)
        self.log_text.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)

        log_tab = QWidget()
        log_layout = QVBoxLayout(log_tab)
        log_layout.setContentsMargins(0, 0, 0, 0)
        log_layout.addWidget(self.log_text)

        self.status_tabs.addTab(detail_tab, "상세 상태")
        self.status_tabs.addTab(log_tab, "로그")
        self.status_tabs.setCurrentIndex(self.STATUS_TAB_LOG)
        layout.addWidget(self.status_tabs)
        return panel

    def _apply_defaults(self) -> None:
        self._populate_recipe_selector()
        self._populate_priority_selector()
        self.set_queue_metrics(None, None)

    def _apply_initial_scroll_alignment_once(self) -> None:
        """Reset horizontal scrollbars to left once at startup."""

        if self._initial_scroll_alignment_done:
            return
        self._initial_scroll_alignment_done = True
        self._reset_horizontal_scrollbars_to_left()
        # One extra pass handles async layout/model updates right after first paint.
        QTimer.singleShot(80, self._reset_horizontal_scrollbars_to_left)

    def _reset_horizontal_scrollbars_to_left(self) -> None:
        """Move known horizontal scrollbars to their minimum position."""

        for widget in (
            self.folder_tree,
            self.active_folder_table,
            self.completed_folder_table,
            self.image_table,
            self.log_text,
        ):
            scrollbar = widget.horizontalScrollBar()
            if scrollbar is not None:
                scrollbar.setValue(scrollbar.minimum())

    def jump_to_path(self, path: str, show_feedback: bool = True) -> bool:
        """Move tree focus to a specific path without auto-registering tasks."""

        cleaned_path = path.strip()
        if not cleaned_path:
            if show_feedback:
                self._show_path_error("이동할 경로를 입력해주세요.")
            return False

        target_path = self._normalize_navigation_path(cleaned_path)
        if not target_path:
            if show_feedback:
                self._show_path_error(f"경로를 해석할 수 없습니다: {cleaned_path}")
            return False

        if os.path.isfile(target_path):
            target_path = os.path.dirname(target_path)

        if not os.path.isdir(target_path):
            if show_feedback:
                self._show_path_error(f"유효한 폴더 경로가 아닙니다: {target_path}")
            return False

        self._pending_jump_target = target_path
        self._pending_jump_show_feedback = show_feedback
        self._pending_jump_attempts = 0

        if self._try_focus_tree_path(target_path):
            self._schedule_pending_jump_finalization()
            return True

        # First-click fallback: wait for QFileSystemModel async directory loading.
        self.file_system_model.setRootPath(target_path)
        parent_path = os.path.dirname(target_path)
        if parent_path and parent_path != target_path:
            self.file_system_model.setRootPath(parent_path)
        QTimer.singleShot(80, self._retry_pending_jump)
        return True

    def current_runtime_settings(self) -> tuple[str, str, int, int]:
        """Return runtime settings using recipe/priority UI and config defaults."""

        action = self._config.publish.default_action
        recipe_path = str(self.recipe_combo.currentData() or "").strip()
        if not recipe_path:
            recipe_path = self._config.recipe_config.default_path

        polling_interval = max(1, int(self._config.publish.polling_interval_seconds))

        try:
            priority = max(0, int(self.priority_combo.currentText().strip() or "0"))
        except ValueError:
            priority = 0

        return action, recipe_path, polling_interval, priority

    def selected_tree_folder(self) -> str | None:
        """Return currently focused folder path from left tree."""

        index = self.folder_tree.currentIndex()
        if not index.isValid():
            return None
        path = self.file_system_model.filePath(index)
        if not path:
            return None
        return path

    def selected_tree_folders(self) -> list[str]:
        """Return unique selected folder paths from left tree."""

        selection_model = self.folder_tree.selectionModel()
        if selection_model is None:
            return []
        indexes = selection_model.selectedRows(0)
        if not indexes:
            focused = self.selected_tree_folder()
            return [focused] if focused else []

        unique_paths: list[str] = []
        seen: set[str] = set()
        for index in indexes:
            path = self.file_system_model.filePath(index)
            if not path or path in seen:
                continue
            seen.add(path)
            unique_paths.append(path)
        return unique_paths

    def append_log(self, message: str) -> None:
        """Append one line to the log panel."""

        self.log_text.append(message)

    def set_connection_status(self, connected: bool, label: str) -> None:
        """Set connection status badge text/state."""

        state = "connected" if connected else "disconnected"
        self.connection_label.setProperty("state", state)
        self.connection_label.setText(self._build_connection_status_text(label))
        self.connection_label.style().unpolish(self.connection_label)
        self.connection_label.style().polish(self.connection_label)

    def _build_connection_status_text(self, status_label: str) -> str:
        """Build connection badge text including broker endpoint and request queue."""

        host = str(self._config.rabbitmq.host or "-").strip() or "-"
        port = int(self._config.rabbitmq.port)
        request_queue = str(self._config.rabbitmq.request_queue or "-").strip() or "-"
        return f"연결 상태: {status_label} | {host}:{port} | request_queue: {request_queue}"

    def set_queue_metrics(self, worker_count: int | None, queued_messages: int | None) -> None:
        """Render queue consumer/message counters near connection status."""

        workers_text = str(worker_count) if isinstance(worker_count, int) and worker_count >= 0 else "-"
        queued_text = (
            str(queued_messages) if isinstance(queued_messages, int) and queued_messages >= 0 else "-"
        )
        self.queue_metrics_label.setText(
            f"Worker Count: {workers_text}    Queued Messages: {queued_text}"
        )

    def set_overall_stats(self, stats: dict[str, float | int | None]) -> None:
        """Update overall progress widgets."""

        progress = int(float(stats.get("progress", 0.0)))
        completed = int(stats.get("completed", 0))
        total = int(stats.get("total", 0))

        avg_seconds = stats.get("avg_processing_seconds")
        avg_text = (
            f"{float(avg_seconds):.1f}s"
            if isinstance(avg_seconds, (int, float)) and float(avg_seconds) > 0
            else "-"
        )
        eta_seconds = stats.get("eta_seconds")
        eta_text = (
            self._format_duration(float(eta_seconds))
            if isinstance(eta_seconds, (int, float)) and float(eta_seconds) >= 0
            else "-"
        )

        self.overall_progress.setValue(progress)
        self.overall_label.setText(
            f"전체 진행률 {float(stats.get('progress', 0.0)):.1f}% ({completed}/{total}) "
            f"| Avg Time/Image {avg_text} | ETA {eta_text}"
        )

    def set_running_state(self, running: bool) -> None:
        """Toggle buttons based on active task flow."""

        self.btn_start.setEnabled(not running)
        self.btn_stop.setEnabled(running)
        self.btn_add_folder.setEnabled(True)
        self.btn_add_subfolders.setEnabled(True)

    def set_folder_rows(self, rows: list[FolderSummary]) -> None:
        """Replace folder table rows."""

        active_rows = [row for row in rows if not row.status.is_done]
        completed_rows = [row for row in rows if row.status.is_done]
        self.active_folder_table_model.set_rows(active_rows)
        self.completed_folder_table_model.set_rows(completed_rows)
        if not rows:
            self.active_folder_table.clearSelection()
            self.completed_folder_table.clearSelection()

    def upsert_folder_row(self, row: FolderSummary) -> None:
        """Insert or update one folder row."""

        if row.status.is_done:
            self.active_folder_table_model.remove_by_folder_path(row.folder_path)
            self.completed_folder_table_model.upsert_summary(row)
        else:
            self.completed_folder_table_model.remove_by_folder_path(row.folder_path)
            self.active_folder_table_model.upsert_summary(row)

    def set_image_tasks(self, tasks: list[ImageTask]) -> None:
        """Replace image detail rows for selected folder."""

        self.image_table_model.set_tasks(tasks)
        if not tasks:
            self.image_table.clearSelection()

    def update_image_task(self, task: ImageTask) -> None:
        """Update one image row in detail table if visible."""

        self.image_table_model.update_task(task)

    def clear_progress_views(self) -> None:
        """Clear folder/image tables and their current selections."""

        self.active_folder_table_model.clear()
        self.completed_folder_table_model.clear()
        self.image_table_model.clear()
        self.active_folder_table.clearSelection()
        self.completed_folder_table.clearSelection()
        self.image_table.clearSelection()
        self.btn_delete_active_folders.setEnabled(False)

    def set_active_result_queue(self, queue_name: str | None) -> None:
        """Track currently active result queue for MQ preview dialog."""

        self._active_result_queue = queue_name

    def show_mq_preview(self, preview_data: dict[str, Any]) -> None:
        """Open modal dialog for one task's MQ preview information."""

        dialog = MQPreviewDialog(preview_data=preview_data, parent=self)
        dialog.exec()

    def confirm_reset(self) -> bool:
        """Show reset confirmation dialog."""

        result = QMessageBox.question(
            self,
            "초기화 확인",
            "현재 등록된 작업과 진행 상태를 모두 초기화할까요?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        return result == QMessageBox.Yes

    def _on_add_folder_clicked(self) -> None:
        folders = self.selected_tree_folders()
        if folders:
            self.add_folder_requested.emit(folders)

    def _on_add_subfolders_clicked(self) -> None:
        folders = self.selected_tree_folders()
        if folders:
            self.add_subfolders_requested.emit(folders)

    def _on_clear_clicked(self) -> None:
        self.folder_tree.clearSelection()
        self.clear_requested.emit()

    def _on_mq_button_clicked(self, request_id: str) -> None:
        """Forward selected request id to controller for preview generation."""

        self.mq_preview_requested.emit(request_id)

    def _on_delete_active_folders_clicked(self) -> None:
        """Emit selected active folder paths to delete from queue."""

        folder_paths = self._selected_folder_paths_from_table(self.active_folder_table, self.active_folder_table_model)
        if not folder_paths:
            return
        self.delete_folders_requested.emit(folder_paths)

    def _load_sidebar_toggle_icon(self, filename: str) -> QIcon:
        """Load sidebar toggle icon from bundled resources."""

        icon_path = resolve_ui_icon_path(filename)
        if icon_path is None or not icon_path.exists():
            return QIcon()
        icon = QIcon(str(icon_path))
        return icon if not icon.isNull() else QIcon()

    def _update_status_sidebar_toggle_icon(self, collapsed: bool) -> None:
        """Render correct collapse/expand icon and accessibility text."""

        icon_name = "status_sidebar_collapse.svg" if collapsed else "status_sidebar_expand.svg"
        icon = self._load_sidebar_toggle_icon(icon_name)
        if not icon.isNull():
            self.btn_toggle_sidebar.setIcon(icon)
            self.btn_toggle_sidebar.setArrowType(Qt.NoArrow)
        else:
            self.btn_toggle_sidebar.setIcon(QIcon())
            self.btn_toggle_sidebar.setArrowType(Qt.LeftArrow if collapsed else Qt.RightArrow)

        self.btn_toggle_sidebar.setToolTip("상태/로그 펼치기" if collapsed else "상태/로그 숨기기")

    def _on_toggle_status_sidebar(self, collapsed: bool) -> None:
        """Collapse/expand the right status sidebar for flexible workspace."""

        if not hasattr(self, "main_splitter") or not hasattr(self, "status_sidebar_panel"):
            return

        self._update_status_sidebar_toggle_icon(collapsed)

        sizes = self.main_splitter.sizes()
        total_width = sum(max(0, size) for size in sizes) if sizes else 0
        left_width = max(280, sizes[0] if len(sizes) >= 1 and sizes[0] > 0 else 360)

        if collapsed:
            if len(sizes) >= 3:
                self._last_status_sidebar_width = max(260, int(sizes[2]))
            self.status_sidebar_panel.hide()
            center_width = max(560, total_width - left_width) if total_width > 0 else 900
            self.main_splitter.setSizes([left_width, center_width, 0])
            return

        self.status_sidebar_panel.show()
        self.status_sidebar_panel.setMaximumWidth(16777215)
        self.status_sidebar_panel.setMinimumWidth(300)
        if len(sizes) < 3 or total_width <= 0:
            self.main_splitter.setSizes([360, 900, self._last_status_sidebar_width])
            return

        right_width = max(300, min(self._last_status_sidebar_width, total_width // 2))
        center_width = max(560, total_width - left_width - right_width)
        self.main_splitter.setSizes([left_width, center_width, right_width])

    def _emit_folder_selection(self, table: QTableView, model: FolderTableModel) -> None:
        """Emit folder selection from one table and clear opposite table selection."""

        index = table.currentIndex()
        if not index.isValid():
            return
        folder_path = model.folder_at(index.row())
        if folder_path:
            self.folder_row_selected.emit(folder_path)

    def _on_active_folder_selection_changed(self, *_args) -> None:
        if self._is_syncing_folder_selection:
            return
        self._is_syncing_folder_selection = True
        try:
            self.completed_folder_table.clearSelection()
            selected_paths = self._selected_folder_paths_from_table(self.active_folder_table, self.active_folder_table_model)
            self.btn_delete_active_folders.setEnabled(bool(selected_paths))
            if len(selected_paths) == 1:
                self.folder_row_selected.emit(selected_paths[0])
                self._show_detail_status_tab()
            elif len(selected_paths) > 1:
                self.set_image_tasks([])
            else:
                self.set_image_tasks([])
        finally:
            self._is_syncing_folder_selection = False

    def _on_completed_folder_selection_changed(self, *_args) -> None:
        if self._is_syncing_folder_selection:
            return
        self._is_syncing_folder_selection = True
        try:
            self.active_folder_table.clearSelection()
            self.btn_delete_active_folders.setEnabled(False)
            self._emit_folder_selection(self.completed_folder_table, self.completed_folder_table_model)
        finally:
            self._is_syncing_folder_selection = False

    def _on_active_folder_context_menu(self, position) -> None:  # noqa: ANN001
        """Open context menu for active/pending folder rows."""

        self._show_folder_context_menu(
            table=self.active_folder_table,
            model=self.active_folder_table_model,
            position=position,
        )

    def _on_completed_folder_context_menu(self, position) -> None:  # noqa: ANN001
        """Open context menu for completed folder rows."""

        self._show_folder_context_menu(
            table=self.completed_folder_table,
            model=self.completed_folder_table_model,
            position=position,
        )

    def _show_folder_context_menu(
        self,
        table: QTableView,
        model: FolderTableModel,
        position,
    ) -> None:  # noqa: ANN001
        """Render a right-click menu for copying selected folder paths."""

        index = table.indexAt(position)
        if index.isValid():
            selection_model = table.selectionModel()
            if selection_model is not None and not selection_model.isSelected(index):
                selection_flags = QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows
                selection_model.setCurrentIndex(index, selection_flags | QItemSelectionModel.Current)
                selection_model.select(index, selection_flags)

        selected_paths = self._selected_folder_paths_from_table(table, model)
        if not selected_paths:
            return

        menu = QMenu(table)
        copy_action = menu.addAction("경로 복사")
        chosen = menu.exec(table.viewport().mapToGlobal(position))
        if chosen is copy_action:
            self._copy_folder_paths_to_clipboard(selected_paths)

    def _copy_folder_paths_to_clipboard(self, folder_paths: list[str]) -> None:
        """Copy one or more folder paths to clipboard and append UI log."""

        if not folder_paths:
            return
        QApplication.clipboard().setText("\n".join(folder_paths))
        self.append_log(f"[클립보드] 폴더 경로 {len(folder_paths)}건 복사")

    @staticmethod
    def _selected_folder_paths_from_table(table: QTableView, model: FolderTableModel) -> list[str]:
        """Collect selected folder paths from a folder table."""

        selection_model = table.selectionModel()
        if selection_model is None:
            return []
        selected_rows = selection_model.selectedRows()
        paths: list[str] = []
        seen: set[str] = set()
        for index in selected_rows:
            folder_path = model.folder_at(index.row())
            if not folder_path or folder_path in seen:
                continue
            seen.add(folder_path)
            paths.append(folder_path)
        return paths

    def _show_detail_status_tab(self) -> None:
        """Switch the right sidebar to the detail tab when appropriate."""

        if hasattr(self, "status_tabs") and self.status_tabs is not None:
            self.status_tabs.setCurrentIndex(self.STATUS_TAB_DETAIL)

    def _on_path_jump_requested(self) -> None:
        """Handle explicit path jump request from left panel."""

        input_path = self.path_jump_edit.text()
        self.jump_to_path(input_path, show_feedback=True)

    def _show_path_error(self, message: str) -> None:
        """Show path validation errors in both dialog and log panel."""

        QMessageBox.warning(self, "경로 이동 실패", message)
        self.append_log(f"[탐색] {message}")

    def _expand_parent_chain(self, index: QModelIndex) -> None:
        """Expand ancestor nodes so target path is visible in the tree."""

        parent = index.parent()
        while parent.isValid():
            self.folder_tree.expand(parent)
            parent = parent.parent()

    def _on_tree_current_changed(self, current: QModelIndex, _previous: QModelIndex) -> None:
        """Reflect current tree selection immediately into the path input."""

        if self._is_syncing_navigation or not current.isValid():
            return

        selected_path = self.file_system_model.filePath(current)
        if not selected_path:
            return
        if self._pending_jump_target and not self._paths_match(selected_path, self._pending_jump_target):
            return

        self._is_syncing_navigation = True
        try:
            self.path_jump_edit.setText(selected_path)
        finally:
            self._is_syncing_navigation = False

    def _on_directory_loaded(self, _path: str) -> None:
        """Retry pending jump after filesystem model loads directories."""

        if not self._pending_jump_target:
            return
        QTimer.singleShot(0, self._retry_pending_jump)

    def _populate_recipe_selector(self) -> None:
        """Populate recipe alias combo from top-level recipe config."""

        self.recipe_combo.blockSignals(True)
        self.recipe_combo.clear()

        for recipe_item in self._config.recipe_config.recipes:
            self.recipe_combo.addItem(recipe_item.alias, recipe_item.path)

        default_alias = (self._config.recipe_config.default_alias or "").strip().lower()
        selected_idx = 0
        if default_alias:
            for idx in range(self.recipe_combo.count()):
                alias = self.recipe_combo.itemText(idx).strip().lower()
                if alias == default_alias:
                    selected_idx = idx
                    break
        self.recipe_combo.setCurrentIndex(selected_idx)
        self.recipe_combo.blockSignals(False)
        self._on_recipe_changed(selected_idx)

    def _on_recipe_changed(self, index: int) -> None:
        """Update path preview when user selects recipe alias."""

        if index < 0:
            self.recipe_path_preview.clear()
            return
        recipe_path = str(self.recipe_combo.itemData(index) or "")
        self.recipe_path_preview.setText(recipe_path)
        self.recipe_path_preview.setToolTip(recipe_path)

    def _populate_priority_selector(self) -> None:
        """Populate request priority combo from queue declare max priority."""

        max_priority = self._config.rabbitmq.request_queue_max_priority or 0
        default_priority = max(0, min(self._config.publish.default_priority, max_priority))

        self.priority_combo.blockSignals(True)
        self.priority_combo.clear()
        for priority in range(0, max_priority + 1):
            self.priority_combo.addItem(str(priority), priority)
        self.priority_combo.setCurrentText(str(default_priority))
        self.priority_combo.blockSignals(False)

    def _retry_pending_jump(self) -> None:
        """Retry async path focus after QFileSystemModel has loaded indexes."""

        if not self._pending_jump_target:
            return

        target_path = self._pending_jump_target
        if self._try_focus_tree_path(target_path):
            self._schedule_pending_jump_finalization()
            return

        self._pending_jump_attempts += 1
        if self._pending_jump_attempts >= self._max_pending_jump_attempts:
            if self._pending_jump_show_feedback:
                self._show_path_error(f"트리에서 경로를 찾을 수 없습니다: {target_path}")
            self._clear_pending_jump()
            return

        QTimer.singleShot(100, self._retry_pending_jump)

    def _try_focus_tree_path(self, target_path: str) -> bool:
        """Try selecting/centering a path in the tree immediately."""

        model_index = self.file_system_model.index(target_path)
        if not model_index.isValid():
            return False

        self._is_syncing_navigation = True
        try:
            # Keep global root visible so all drives remain visible in the tree.
            self.folder_tree.setRootIndex(QModelIndex())
            self._expand_parent_chain(model_index)
            selection_model = self.folder_tree.selectionModel()
            if selection_model is not None:
                selection_flags = (
                    QItemSelectionModel.ClearAndSelect
                    | QItemSelectionModel.Current
                    | QItemSelectionModel.Rows
                )
                selection_model.setCurrentIndex(model_index, selection_flags)
                selection_model.select(model_index, QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows)
            self.folder_tree.setCurrentIndex(model_index)
            self.folder_tree.scrollTo(model_index, QTreeView.PositionAtCenter)
            self.folder_tree.expand(model_index)
            self.folder_tree.setFocus(Qt.OtherFocusReason)
            self.path_jump_edit.setText(target_path)
        finally:
            self._is_syncing_navigation = False

        return True

    def _clear_pending_jump(self) -> None:
        """Clear async jump retry state."""

        self._pending_jump_target = None
        self._pending_jump_show_feedback = False
        self._pending_jump_attempts = 0

    def _schedule_pending_jump_finalization(self) -> None:
        """Re-assert exact current index after async model/view updates settle."""

        if not self._pending_jump_target:
            return
        QTimer.singleShot(0, self._finalize_pending_jump)

    def _finalize_pending_jump(self) -> None:
        """Ensure current tree index, highlight, and input path all match the target path."""

        target_path = self._pending_jump_target
        if not target_path:
            return

        if not self._try_focus_tree_path(target_path):
            self._pending_jump_attempts += 1
            if self._pending_jump_attempts >= self._max_pending_jump_attempts:
                if self._pending_jump_show_feedback:
                    self._show_path_error(f"트리에서 경로를 찾을 수 없습니다: {target_path}")
                self._clear_pending_jump()
                return
            QTimer.singleShot(100, self._retry_pending_jump)
            return

        current_path = self.file_system_model.filePath(self.folder_tree.currentIndex())
        if not self._paths_match(current_path, target_path):
            self._pending_jump_attempts += 1
            if self._pending_jump_attempts >= self._max_pending_jump_attempts:
                if self._pending_jump_show_feedback:
                    self._show_path_error(f"경로 선택을 확정하지 못했습니다: {target_path}")
                self._clear_pending_jump()
                return
            QTimer.singleShot(80, self._finalize_pending_jump)
            return

        tree_scrollbar = self.folder_tree.horizontalScrollBar()
        if tree_scrollbar is not None:
            tree_scrollbar.setValue(tree_scrollbar.minimum())

        if self._pending_jump_show_feedback:
            self.append_log(f"[탐색] 경로 이동 완료: {target_path}")
        self._clear_pending_jump()

    @staticmethod
    def _normalize_navigation_path(path: str) -> str:
        """Normalize user-entered navigation path without resolving network aliases.

        This intentionally avoids ``Path.resolve()`` so mapped drives or UNC paths
        stay as entered instead of being canonicalized to server/IP targets.
        """

        expanded = os.path.expandvars(os.path.expanduser(path.strip()))
        if not expanded:
            return ""

        normalized = os.path.normpath(expanded)
        if os.path.isabs(normalized):
            return normalized

        return os.path.abspath(os.path.join(os.getcwd(), normalized))

    @staticmethod
    def _paths_match(left: str, right: str) -> bool:
        """Compare filesystem paths using normalized Windows-friendly semantics."""

        if not left or not right:
            return False
        return os.path.normcase(os.path.normpath(left)) == os.path.normcase(os.path.normpath(right))

    @staticmethod
    def _format_duration(seconds: float) -> str:
        """Format seconds as H:MM:SS or MM:SS."""

        total_seconds = max(0, int(round(seconds)))
        hours, rem = divmod(total_seconds, 3600)
        minutes, secs = divmod(rem, 60)
        if hours:
            return f"{hours:d}:{minutes:02d}:{secs:02d}"
        return f"{minutes:02d}:{secs:02d}"
