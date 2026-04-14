"""Main GUI window for task registration, control, and tracking."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QDir, Qt, Signal
from PySide6.QtWidgets import (
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
)

from config.models import AppConfig
from models.task_models import FolderSummary, ImageTask
from ui.models import FolderTableModel, ImageTableModel, ProgressBarDelegate
from ui.widgets import StatusBadgeDelegate


class MainWindow(QMainWindow):
    """Main application window with modern, operator-friendly layout."""

    add_folder_requested = Signal(str)
    add_subfolders_requested = Signal(str)
    clear_requested = Signal()
    start_requested = Signal()
    stop_requested = Signal()
    reset_requested = Signal()
    folder_row_selected = Signal(str)

    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self._config = config
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

        self.folder_tree = QTreeView()
        self.folder_tree.setHeaderHidden(True)
        self.folder_tree.setAnimated(True)
        self.folder_tree.setIndentation(18)

        self.file_system_model = QFileSystemModel(self.folder_tree)
        self.file_system_model.setRootPath(QDir.rootPath())
        self.file_system_model.setFilter(QDir.AllDirs | QDir.NoDotAndDotDot)

        self.folder_tree.setModel(self.file_system_model)
        for col in range(1, 4):
            self.folder_tree.hideColumn(col)

        home_index = self.file_system_model.index(str(Path.home()))
        self.folder_tree.setRootIndex(home_index)
        self.folder_tree.expand(home_index)

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
        row_recipe.addWidget(QLabel("Recipe Path"), stretch=0)
        self.recipe_edit = QLineEdit()
        row_recipe.addWidget(self.recipe_edit, stretch=1)
        layout.addLayout(row_recipe)

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
        self.folder_table.horizontalHeader().setStretchLastSection(False)
        self.folder_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.folder_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.folder_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.folder_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.folder_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.folder_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeToContents)
        self.folder_table.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeToContents)
        self.folder_table.horizontalHeader().setSectionResizeMode(7, QHeaderView.ResizeToContents)

        self.folder_table.setItemDelegateForColumn(6, ProgressBarDelegate(self.folder_table))
        self.folder_table.setItemDelegateForColumn(7, StatusBadgeDelegate(self.folder_table))
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
        self.image_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        for col in range(1, self.image_table_model.columnCount()):
            self.image_table.horizontalHeader().setSectionResizeMode(col, QHeaderView.ResizeToContents)
        self.image_table.setItemDelegateForColumn(2, StatusBadgeDelegate(self.image_table))

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setPlaceholderText("작업 로그가 여기에 표시됩니다.")

        splitter.addWidget(self.image_table)
        splitter.addWidget(self.log_text)
        splitter.setSizes([400, 220])

        layout.addWidget(splitter)
        return panel

    def _apply_defaults(self) -> None:
        self.action_edit.setText(self._config.publish.default_action)
        self.recipe_edit.setText(self._config.publish.default_recipe_path)

        idx = self.polling_combo.findText(str(self._config.publish.polling_interval_seconds))
        if idx >= 0:
            self.polling_combo.setCurrentIndex(idx)
        else:
            self.polling_combo.setCurrentText(str(self._config.publish.polling_interval_seconds))

    def current_runtime_settings(self) -> tuple[str, str, int]:
        """Return action, recipe path, polling interval from UI fields."""

        action = self.action_edit.text().strip()
        recipe_path = self.recipe_edit.text().strip()

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

    def upsert_folder_row(self, row: FolderSummary) -> None:
        """Insert or update one folder row."""

        self.folder_table_model.upsert_summary(row)

    def set_image_tasks(self, tasks: list[ImageTask]) -> None:
        """Replace image detail rows for selected folder."""

        self.image_table_model.set_tasks(tasks)

    def update_image_task(self, task: ImageTask) -> None:
        """Update one image row in detail table if visible."""

        self.image_table_model.update_task(task)

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

    def _on_folder_row_selection_changed(self, *_args) -> None:
        index = self.folder_table.currentIndex()
        if not index.isValid():
            return
        folder_path = self.folder_table_model.folder_at(index.row())
        if folder_path:
            self.folder_row_selected.emit(folder_path)
