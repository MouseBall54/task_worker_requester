"""Main GUI window for task registration, control, and tracking."""

from __future__ import annotations

import json
from pathlib import PureWindowsPath
from pathlib import Path
from typing import Any

from PySide6.QtCore import QDir, QModelIndex, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFileSystemModel,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableView,
    QTextEdit,
    QTreeView,
    QVBoxLayout,
    QWidget,
    QComboBox,
    QProgressBar,
    QHeaderView,
    QDialogButtonBox,
    QPlainTextEdit,
)

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
            "=== Payload (Expected / 현재 선택 기준 예상값) ===",
            json.dumps(preview_data.get("payload", {}).get("expected", {}), ensure_ascii=False, indent=2),
            "",
            "=== Payload (Published / 실제 전송값) ===",
            json.dumps(preview_data.get("payload", {}).get("published", {}), ensure_ascii=False, indent=2),
        ]
        return "\n".join(sections)


class MainWindow(QMainWindow):
    """Main application window with modern, operator-friendly layout."""

    add_folder_requested = Signal(str)
    add_subfolders_requested = Signal(str)
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
        self._build_ui()
        self._apply_defaults()

    def _build_ui(self) -> None:
        self.setWindowTitle(self._config.ui.app_name)
        self.resize(self._config.ui.window_width, self._config.ui.window_height)

        root = QWidget(self)
        self.setCentralWidget(root)

        main_layout = QHBoxLayout(root)
        main_layout.setContentsMargins(16, 16, 16, 16)
        main_layout.setSpacing(16)

        left_panel = self._build_left_panel()
        right_panel = self._build_right_panel()

        left_panel.setFixedWidth(360)
        main_layout.addWidget(left_panel)
        main_layout.addWidget(right_panel, stretch=1)

    def _build_left_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("leftPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(12)

        title = QLabel("폴더 선택")
        title.setObjectName("panelTitle")
        layout.addWidget(title)

        drive_row = QHBoxLayout()
        drive_row.addWidget(QLabel("드라이브"), stretch=0)
        self.drive_combo = QComboBox()
        self.drive_combo.currentIndexChanged.connect(self._on_drive_changed)
        drive_row.addWidget(self.drive_combo, stretch=1)
        layout.addLayout(drive_row)

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
        self.folder_tree.setHeaderHidden(True)
        self.folder_tree.setAnimated(True)
        self.folder_tree.setIndentation(18)

        self.file_system_model = QFileSystemModel(self.folder_tree)
        self.file_system_model.setRootPath("")
        self.file_system_model.setFilter(QDir.AllDirs | QDir.NoDotAndDotDot | QDir.Drives)

        self.folder_tree.setModel(self.file_system_model)
        for col in range(1, 4):
            self.folder_tree.hideColumn(col)
        if self.folder_tree.selectionModel():
            self.folder_tree.selectionModel().currentChanged.connect(self._on_tree_current_changed)

        self.folder_tree.setRootIndex(QModelIndex())
        self._populate_drive_combo()
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

    def _build_right_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        layout.addWidget(self._build_control_panel())
        layout.addWidget(self._build_folder_progress_panel(), stretch=1)
        layout.addWidget(self._build_bottom_panel(), stretch=1)

        return panel

    def _build_control_panel(self) -> QWidget:
        panel = QGroupBox("작업 설정 및 제어")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        self.connection_label = QLabel("연결 상태: 대기")
        self.connection_label.setObjectName("connectionStatus")
        layout.addWidget(self.connection_label)

        row_action = QHBoxLayout()
        row_action.addWidget(QLabel("Action"), stretch=0)
        self.action_edit = QLineEdit()
        row_action.addWidget(self.action_edit, stretch=1)
        layout.addLayout(row_action)

        row_recipe = QHBoxLayout()
        row_recipe.addWidget(QLabel("Recipe"), stretch=0)
        self.recipe_combo = QComboBox()
        self.recipe_combo.currentIndexChanged.connect(self._on_recipe_changed)
        row_recipe.addWidget(self.recipe_combo, stretch=1)
        layout.addLayout(row_recipe)

        recipe_path_row = QHBoxLayout()
        recipe_path_row.addWidget(QLabel("선택 경로"), stretch=0)
        self.recipe_path_preview = QLineEdit()
        self.recipe_path_preview.setReadOnly(True)
        recipe_path_row.addWidget(self.recipe_path_preview, stretch=1)
        layout.addLayout(recipe_path_row)

        row_polling = QHBoxLayout()
        row_polling.addWidget(QLabel("Polling 간격"), stretch=0)
        self.polling_combo = QComboBox()
        self.polling_combo.addItems(["3", "5", "10", "15"])
        self.polling_combo.setEditable(True)
        row_polling.addWidget(self.polling_combo, stretch=0)
        row_polling.addWidget(QLabel("초"), stretch=0)
        row_polling.addStretch(1)
        layout.addLayout(row_polling)

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

        self.folder_table_model = FolderTableModel()
        self.folder_table = QTableView()
        self.folder_table.setModel(self.folder_table_model)
        self.folder_table.setSelectionBehavior(QTableView.SelectRows)
        self.folder_table.setSelectionMode(QTableView.SingleSelection)
        self.folder_table.setAlternatingRowColors(True)
        self.folder_table.verticalHeader().setVisible(False)
        self.folder_table.verticalHeader().setDefaultSectionSize(38)
        self.folder_table.verticalHeader().setMinimumSectionSize(34)
        self.folder_table.setWordWrap(False)
        self.folder_table.setTextElideMode(Qt.ElideRight)
        self.folder_table.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.folder_table.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.folder_table.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)

        folder_header = self.folder_table.horizontalHeader()
        folder_header.setStretchLastSection(False)
        folder_header.setSectionResizeMode(QHeaderView.ResizeToContents)
        folder_header.setResizeContentsPrecision(-1)
        folder_header.setMinimumSectionSize(56)
        self.folder_table.setItemDelegateForColumn(0, ProgressBarDelegate(self.folder_table))
        self.folder_table.setItemDelegateForColumn(1, StatusBadgeDelegate(self.folder_table))
        self.folder_table.selectionModel().selectionChanged.connect(self._on_folder_row_selection_changed)

        layout.addWidget(self.folder_table)
        return panel

    def _build_bottom_panel(self) -> QWidget:
        panel = QGroupBox("상세 상태 및 로그")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(8, 8, 8, 8)

        splitter = QSplitter(Qt.Vertical)

        self.image_table_model = ImageTableModel()
        self.image_table = QTableView()
        self.image_table.setModel(self.image_table_model)
        self.image_table.setSelectionBehavior(QTableView.SelectRows)
        self.image_table.setAlternatingRowColors(True)
        self.image_table.verticalHeader().setVisible(False)
        self.image_table.verticalHeader().setDefaultSectionSize(38)
        self.image_table.verticalHeader().setMinimumSectionSize(34)
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
        self.image_table.setItemDelegateForColumn(3, StatusBadgeDelegate(self.image_table))

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setPlaceholderText("작업 로그가 여기에 표시됩니다.")
        self.log_text.setLineWrapMode(QTextEdit.NoWrap)
        self.log_text.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)

        splitter.addWidget(self.image_table)
        splitter.addWidget(self.log_text)
        splitter.setSizes([400, 220])

        layout.addWidget(splitter)
        return panel

    def _apply_defaults(self) -> None:
        self.action_edit.setText(self._config.publish.default_action)
        self._populate_recipe_selector()

        idx = self.polling_combo.findText(str(self._config.publish.polling_interval_seconds))
        if idx >= 0:
            self.polling_combo.setCurrentIndex(idx)
        else:
            self.polling_combo.setCurrentText(str(self._config.publish.polling_interval_seconds))

    def jump_to_path(self, path: str, show_feedback: bool = True) -> bool:
        """Move tree focus to a specific path without auto-registering tasks."""

        cleaned_path = path.strip()
        if not cleaned_path:
            if show_feedback:
                self._show_path_error("이동할 경로를 입력해주세요.")
            return False

        target = Path(cleaned_path).expanduser()
        try:
            if not target.is_absolute():
                target = (Path.cwd() / target).resolve()
            else:
                target = target.resolve()
        except OSError:
            if show_feedback:
                self._show_path_error(f"경로를 해석할 수 없습니다: {cleaned_path}")
            return False

        if target.is_file():
            target = target.parent

        if not target.exists() or not target.is_dir():
            if show_feedback:
                self._show_path_error(f"유효한 폴더 경로가 아닙니다: {target}")
            return False

        target_path = str(target)
        model_index = self.file_system_model.index(target_path)
        if not model_index.isValid():
            if show_feedback:
                self._show_path_error(f"트리에서 경로를 찾을 수 없습니다: {target_path}")
            return False

        self._is_syncing_navigation = True
        try:
            # Keep global root visible so all drives remain visible in the tree.
            self.folder_tree.setRootIndex(QModelIndex())

            self._expand_parent_chain(model_index)
            self.folder_tree.setCurrentIndex(model_index)
            self.folder_tree.scrollTo(model_index, QTreeView.PositionAtCenter)
            self.folder_tree.expand(model_index)
            self.folder_tree.setFocus(Qt.OtherFocusReason)

            self.path_jump_edit.setText(str(target))
            self._sync_drive_combo_for_path(target)
        finally:
            self._is_syncing_navigation = False

        if show_feedback:
            self.append_log(f"[탐색] 경로 이동 완료: {target}")
        return True

    def current_runtime_settings(self) -> tuple[str, str, int]:
        """Return action, recipe path, polling interval from UI fields."""

        action = self.action_edit.text().strip()
        recipe_path = str(self.recipe_combo.currentData() or "").strip()
        if not recipe_path:
            recipe_path = self._config.publish.default_recipe_path

        polling_text = self.polling_combo.currentText().strip() or "5"
        try:
            polling_interval = max(1, int(polling_text))
        except ValueError:
            polling_interval = 5

        return action, recipe_path, polling_interval

    def selected_tree_folder(self) -> str | None:
        """Return currently selected folder path from left tree."""

        index = self.folder_tree.currentIndex()
        if not index.isValid():
            return None
        path = self.file_system_model.filePath(index)
        if not path:
            return None
        return path

    def append_log(self, message: str) -> None:
        """Append one line to the log panel."""

        self.log_text.append(message)

    def set_connection_status(self, connected: bool, label: str) -> None:
        """Set connection status badge text/state."""

        state = "connected" if connected else "disconnected"
        self.connection_label.setProperty("state", state)
        self.connection_label.setText(f"연결 상태: {label}")
        self.connection_label.style().unpolish(self.connection_label)
        self.connection_label.style().polish(self.connection_label)

    def set_overall_stats(self, stats: dict[str, float | int]) -> None:
        """Update overall progress widgets."""

        progress = int(float(stats.get("progress", 0.0)))
        completed = int(stats.get("completed", 0))
        total = int(stats.get("total", 0))
        self.overall_progress.setValue(progress)
        self.overall_label.setText(
            f"전체 진행률 {float(stats.get('progress', 0.0)):.1f}% ({completed}/{total})"
        )

    def set_running_state(self, running: bool) -> None:
        """Toggle buttons based on active task flow."""

        self.btn_start.setEnabled(not running)
        self.btn_stop.setEnabled(running)
        self.btn_add_folder.setEnabled(not running)
        self.btn_add_subfolders.setEnabled(not running)

    def set_folder_rows(self, rows: list[FolderSummary]) -> None:
        """Replace folder table rows."""

        self.folder_table_model.set_rows(rows)
        if not rows:
            self.folder_table.clearSelection()

    def upsert_folder_row(self, row: FolderSummary) -> None:
        """Insert or update one folder row."""

        self.folder_table_model.upsert_summary(row)

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

        self.folder_table_model.clear()
        self.image_table_model.clear()
        self.folder_table.clearSelection()
        self.image_table.clearSelection()

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
        folder = self.selected_tree_folder()
        if folder:
            self.add_folder_requested.emit(folder)

    def _on_add_subfolders_clicked(self) -> None:
        folder = self.selected_tree_folder()
        if folder:
            self.add_subfolders_requested.emit(folder)

    def _on_clear_clicked(self) -> None:
        self.folder_tree.clearSelection()
        self.clear_requested.emit()

    def _on_mq_button_clicked(self, request_id: str) -> None:
        """Forward selected request id to controller for preview generation."""

        self.mq_preview_requested.emit(request_id)

    def _on_folder_row_selection_changed(self, *_args) -> None:
        index = self.folder_table.currentIndex()
        if not index.isValid():
            return
        folder_path = self.folder_table_model.folder_at(index.row())
        if folder_path:
            self.folder_row_selected.emit(folder_path)

    def _populate_drive_combo(self) -> None:
        """Populate drive selector from OS drive list."""

        self.drive_combo.blockSignals(True)
        self.drive_combo.clear()

        seen: set[str] = set()
        drives = QDir.drives()
        for drive_info in drives:
            drive_path = drive_info.absoluteFilePath()
            normalized_key = drive_path.lower()
            if normalized_key in seen:
                continue
            seen.add(normalized_key)

            label = self._drive_label(drive_path)
            self.drive_combo.addItem(label, drive_path)

        if self.drive_combo.count() == 0:
            # Non-Windows fallback: keep at least one root entry.
            self.drive_combo.addItem(QDir.rootPath(), QDir.rootPath())

        self.drive_combo.blockSignals(False)
        self._sync_drive_combo_for_path(Path.home())

    def _on_drive_changed(self, index: int) -> None:
        """Jump tree focus when user picks a drive from combo box."""

        if index < 0 or self._is_syncing_navigation:
            return
        drive_path = str(self.drive_combo.itemData(index) or "").strip()
        if not drive_path:
            return

        drive_index = self.file_system_model.index(drive_path)
        if not drive_index.isValid():
            self._show_path_error(f"드라이브를 트리에서 찾을 수 없습니다: {drive_path}")
            return

        self._is_syncing_navigation = True
        try:
            # Always show the global drive list in the tree.
            self.folder_tree.setRootIndex(QModelIndex())
            self.folder_tree.setCurrentIndex(drive_index)
            self.folder_tree.scrollTo(drive_index, QTreeView.PositionAtCenter)
            self.folder_tree.expand(drive_index)
            self.folder_tree.setFocus(Qt.OtherFocusReason)
            self.path_jump_edit.setText(str(Path(drive_path)))
        finally:
            self._is_syncing_navigation = False

        self.append_log(f"[탐색] 드라이브 선택: {drive_path}")

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

    def _sync_drive_combo_for_path(self, target_path: str | Path) -> None:
        """Sync drive combo selection to the current path."""

        target = Path(target_path)
        drive = PureWindowsPath(str(target)).drive
        if not drive:
            return

        target_drive = drive.lower().rstrip("\\/")
        for idx in range(self.drive_combo.count()):
            data = str(self.drive_combo.itemData(idx) or "")
            combo_drive = PureWindowsPath(data).drive.lower().rstrip("\\/")
            if combo_drive == target_drive:
                self.drive_combo.blockSignals(True)
                self.drive_combo.setCurrentIndex(idx)
                self.drive_combo.blockSignals(False)
                return

    def _on_tree_current_changed(self, current: QModelIndex, _previous: QModelIndex) -> None:
        """Reflect current tree selection immediately into path input and drive combo."""

        if self._is_syncing_navigation or not current.isValid():
            return

        selected_path = self.file_system_model.filePath(current)
        if not selected_path:
            return

        self._is_syncing_navigation = True
        try:
            self.path_jump_edit.setText(selected_path)
            self._sync_drive_combo_for_path(selected_path)
        finally:
            self._is_syncing_navigation = False

    @staticmethod
    def _drive_label(drive_path: str) -> str:
        """Return user-friendly label for drive combo entries."""

        drive = PureWindowsPath(drive_path).drive
        if drive:
            return f"{drive}\\"
        return drive_path

    def _populate_recipe_selector(self) -> None:
        """Populate recipe alias combo from config presets."""

        self.recipe_combo.blockSignals(True)
        self.recipe_combo.clear()

        for preset in self._config.publish.recipe_presets:
            self.recipe_combo.addItem(preset.alias, preset.path)

        default_alias = (self._config.publish.default_recipe_alias or "").strip().lower()
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
