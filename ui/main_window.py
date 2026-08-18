"""Main GUI window for task registration, control, and tracking."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from PySide6.QtCore import (
    QByteArray,
    QDir,
    QFileSystemWatcher,
    QItemSelectionModel,
    QModelIndex,
    QSize,
    QSettings,
    Qt,
    QThread,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import QAction, QCloseEvent, QDesktopServices, QIcon, QKeySequence, QShowEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QToolButton,
    QDialog,
    QFileDialog,
    QFileSystemModel,
    QFrame,
    QGroupBox,
    QHeaderView,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTabWidget,
    QTableView,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QTreeView,
    QVBoxLayout,
    QWidget,
    QComboBox,
    QProgressBar,
    QSizePolicy,
    QDialogButtonBox,
    QPlainTextEdit,
)

from app.runtime_paths import resolve_ui_icon_path
from config.models import AppConfig
from config.config_loader import ConfigError
from models.task_models import FolderSummary, ImageTask, RunHistorySummary
from services.workers.folder_index_worker import FolderIndexWorker
from state.folder_index_repository import (
    INDEX_EMPTY,
    INDEX_EXCLUDED,
    INDEX_INCOMPLETE,
    INDEX_INDEXING,
    INDEX_OFFLINE,
    INDEX_PAUSED,
    INDEX_READY,
    SCOPE_DEPTH,
    SCOPE_EXCLUDED,
    SCOPE_FULL,
    FolderIndexRepository,
    FolderSearchResult,
)
from ui.help_dialog import HelpDialog
from ui.settings_dialog import AppConfigSettingsDialog, RecipeConfigSettingsDialog
from ui.models import FolderTableModel, ImageTableModel, ProgressBarDelegate
from ui.widgets import MQButtonDelegate, StatusBadgeDelegate
from utils.time_utils import format_seoul_display


class FolderTreeView(QTreeView):
    """Keep app-managed horizontal alignment during Qt selection scrolling."""

    def scrollTo(  # noqa: N802
        self,
        index: QModelIndex,
        hint: QAbstractItemView.ScrollHint = QAbstractItemView.EnsureVisible,
    ) -> None:
        horizontal_scrollbar = self.horizontalScrollBar()
        preserve_horizontal_position = hint == QAbstractItemView.EnsureVisible
        horizontal_value = horizontal_scrollbar.value()

        super().scrollTo(index, hint)

        if preserve_horizontal_position:
            horizontal_scrollbar.setValue(horizontal_value)


def _folder_index_status_label(status: str) -> str:
    return {
        INDEX_EMPTY: "미색인",
        INDEX_INDEXING: "색인 중",
        INDEX_PAUSED: "일시정지",
        INDEX_INCOMPLETE: "미완료",
        INDEX_READY: "완료",
        INDEX_OFFLINE: "오프라인",
        INDEX_EXCLUDED: "색인 제외",
    }.get(status, status)


def _folder_scope_label(scope_mode: str, max_depth: int | None) -> str:
    if scope_mode == SCOPE_DEPTH:
        return f"하위 {max_depth or 5}계층"
    if scope_mode == SCOPE_EXCLUDED:
        return "색인 제외"
    return "전체 계층"


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


class DuplicateFolderDialog(QDialog):
    """Display folders skipped because the same path is already registered."""

    def __init__(
        self,
        rows: list[tuple[str, str]],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("중복 폴더+Recipe 안내")
        self.resize(820, 420)

        layout = QVBoxLayout(self)
        header = QLabel(
            f"이미 등록되어 추가하지 않은 폴더+Recipe 조합이 {len(rows)}개 있습니다."
        )
        layout.addWidget(header)

        self.table = QTableWidget(len(rows), 2, self)
        self.table.setHorizontalHeaderLabels(["폴더 경로 + Recipe", "현재 위치"])
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        for row_index, (folder_path, location) in enumerate(rows):
            self.table.setItem(row_index, 0, QTableWidgetItem(folder_path))
            self.table.setItem(row_index, 1, QTableWidgetItem(location))
        layout.addWidget(self.table, stretch=1)

        buttons = QDialogButtonBox(QDialogButtonBox.Close, parent=self)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)


class FavoriteRootManagerDialog(QDialog):
    """Manage persistent favorite roots outside the main navigation panel."""

    SCOPE_DEPTH_ROLE = Qt.UserRole + 1
    changed = Signal(str, str)

    def __init__(
        self,
        repository: FolderIndexRepository,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._repository = repository
        self._scope_editor_path: str | None = None
        self._scope_combo_dirty = False
        self._syncing_scope_combo = False
        self.setWindowTitle("즐겨찾기 Root 관리")
        self.resize(1120, 520)

        layout = QVBoxLayout(self)
        description = QLabel(
            "검색과 빠른 이동에 사용할 Root를 추가하고 표시 순서를 관리합니다."
        )
        description.setWordWrap(True)
        layout.addWidget(description)

        self.root_table = QTableWidget(0, 7, self)
        self.root_table.setObjectName("favoriteRootManagerTable")
        self.root_table.setHorizontalHeaderLabels(
            ["Root 경로", "색인 범위", "상태", "색인됨", "대기", "오류", "마지막 완료"]
        )
        self.root_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.root_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.root_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.root_table.setAlternatingRowColors(True)
        self.root_table.verticalHeader().setVisible(False)
        self.root_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.Stretch
        )
        for column in range(1, 6):
            self.root_table.horizontalHeader().setSectionResizeMode(
                column, QHeaderView.ResizeToContents
            )
        self.root_table.horizontalHeader().setSectionResizeMode(6, QHeaderView.Interactive)
        self.root_table.setColumnWidth(6, 190)
        self.root_table.itemSelectionChanged.connect(self._sync_buttons)
        layout.addWidget(self.root_table, stretch=1)

        action_row = QHBoxLayout()
        self.add_button = QPushButton("폴더 추가", self)
        self.remove_button = QPushButton("삭제", self)
        self.up_button = QPushButton("위로", self)
        self.down_button = QPushButton("아래로", self)
        self.scope_combo = QComboBox(self)
        self.scope_combo.addItem("전체 계층", SCOPE_FULL)
        for depth in (3, 4, 5, 6):
            self.scope_combo.addItem(f"하위 {depth}계층", SCOPE_DEPTH)
            self.scope_combo.setItemData(
                self.scope_combo.count() - 1, depth, self.SCOPE_DEPTH_ROLE
            )
        self.scope_combo.addItem("색인 제외", SCOPE_EXCLUDED)
        self.scope_combo.setCurrentIndex(self._find_scope_index(SCOPE_DEPTH, 5))
        self.scope_combo.currentIndexChanged.connect(self._on_scope_combo_changed)
        self.apply_scope_button = QPushButton("범위 적용", self)
        self.reindex_button = QPushButton("전체 재색인", self)
        self.refresh_button = QPushButton("증분 갱신", self)
        self.pause_button = QPushButton("색인 일시정지", self)
        self.resume_button = QPushButton("색인 재개", self)
        self.errors_button = QPushButton("오류 보기", self)
        self.add_button.clicked.connect(self._add_root)
        self.remove_button.clicked.connect(self._remove_root)
        self.up_button.clicked.connect(lambda: self._move_root("up"))
        self.down_button.clicked.connect(lambda: self._move_root("down"))
        self.apply_scope_button.clicked.connect(self._apply_scope)
        self.reindex_button.clicked.connect(lambda: self._emit_action("reindex"))
        self.refresh_button.clicked.connect(lambda: self._emit_action("refresh"))
        self.pause_button.clicked.connect(self._pause_root)
        self.resume_button.clicked.connect(self._resume_root)
        self.errors_button.clicked.connect(self._show_errors)
        action_row.addWidget(self.add_button)
        action_row.addWidget(self.remove_button)
        action_row.addStretch(1)
        action_row.addWidget(self.up_button)
        action_row.addWidget(self.down_button)
        layout.addLayout(action_row)

        index_row = QHBoxLayout()
        index_row.addWidget(QLabel("선택 Root 색인 범위", self))
        index_row.addWidget(self.scope_combo)
        index_row.addWidget(self.apply_scope_button)
        index_row.addSpacing(12)
        index_row.addWidget(self.reindex_button)
        index_row.addWidget(self.refresh_button)
        index_row.addWidget(self.pause_button)
        index_row.addWidget(self.resume_button)
        index_row.addWidget(self.errors_button)
        index_row.addStretch(1)
        layout.addLayout(index_row)

        buttons = QDialogButtonBox(QDialogButtonBox.Close, parent=self)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._reload()
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(1000)
        self._status_timer.timeout.connect(
            lambda: self._reload(selected_path=self._selected_path())
        )
        self.finished.connect(lambda _result: self._status_timer.stop())
        self._status_timer.start()

    def _reload(self, selected_path: str | None = None) -> None:
        favorites = self._repository.list_favorites()
        self.root_table.blockSignals(True)
        try:
            self.root_table.setRowCount(len(favorites))
            for row, favorite in enumerate(favorites):
                path_item = QTableWidgetItem(favorite.path)
                path_item.setData(Qt.UserRole, favorite.path)
                scope_item = QTableWidgetItem(_folder_scope_label(favorite.scope_mode, favorite.max_depth))
                status_item = QTableWidgetItem(_folder_index_status_label(favorite.index_status))
                status_item.setTextAlignment(Qt.AlignCenter)
                indexed_item = QTableWidgetItem(f"{favorite.indexed_count:,}")
                pending_item = QTableWidgetItem(f"{favorite.pending_count:,}")
                error_item = QTableWidgetItem(f"{favorite.error_count:,}")
                completed_item = QTableWidgetItem(
                    format_seoul_display(favorite.last_completed) or "-"
                )
                for item in (indexed_item, pending_item, error_item):
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                checked = (
                    format_seoul_display(favorite.last_checked)
                    or "아직 확인하지 않음"
                )
                for item in (path_item, scope_item, status_item, indexed_item, pending_item, error_item, completed_item):
                    item.setToolTip(f"{favorite.path}\n마지막 확인: {checked}")
                self.root_table.setItem(row, 0, path_item)
                self.root_table.setItem(row, 1, scope_item)
                self.root_table.setItem(row, 2, status_item)
                self.root_table.setItem(row, 3, indexed_item)
                self.root_table.setItem(row, 4, pending_item)
                self.root_table.setItem(row, 5, error_item)
                self.root_table.setItem(row, 6, completed_item)
                if favorite.path == selected_path:
                    self.root_table.setCurrentCell(row, 0)
                    self.root_table.selectRow(row)
        finally:
            self.root_table.blockSignals(False)
        self._sync_buttons()

    def _selected_path(self) -> str | None:
        row = self.root_table.currentRow()
        item = self.root_table.item(row, 0) if row >= 0 else None
        if item is None:
            return None
        return str(item.data(Qt.UserRole) or "").strip() or None

    def _find_scope_index(self, scope_mode: str, max_depth: int | None) -> int:
        for index in range(self.scope_combo.count()):
            if self.scope_combo.itemData(index) != scope_mode:
                continue
            if scope_mode != SCOPE_DEPTH:
                return index
            if self.scope_combo.itemData(index, self.SCOPE_DEPTH_ROLE) == (max_depth or 5):
                return index
        return -1

    def _sync_buttons(self) -> None:
        row = self.root_table.currentRow()
        count = self.root_table.rowCount()
        self.remove_button.setEnabled(row >= 0)
        self.up_button.setEnabled(row > 0)
        self.down_button.setEnabled(row >= 0 and row < count - 1)
        path = self._selected_path()
        favorite = self._repository.get_favorite(path) if path else None
        selected = favorite is not None
        self.scope_combo.setEnabled(selected)
        for button in (
            self.apply_scope_button,
            self.reindex_button,
            self.refresh_button,
            self.pause_button,
            self.resume_button,
            self.errors_button,
        ):
            button.setEnabled(selected)
        if favorite is not None:
            if self._scope_editor_path != favorite.path:
                self._scope_editor_path = favorite.path
                self._scope_combo_dirty = False
            if not self._scope_combo_dirty:
                combo_index = self._find_scope_index(
                    favorite.scope_mode, favorite.max_depth
                )
                if combo_index >= 0:
                    self._syncing_scope_combo = True
                    try:
                        self.scope_combo.setCurrentIndex(combo_index)
                    finally:
                        self._syncing_scope_combo = False
            self.pause_button.setEnabled(favorite.index_status == INDEX_INDEXING)
            self.resume_button.setEnabled(
                favorite.index_status in {INDEX_PAUSED, INDEX_INCOMPLETE, INDEX_OFFLINE, INDEX_EMPTY}
                and favorite.scope_mode != SCOPE_EXCLUDED
            )
            self.errors_button.setEnabled(favorite.error_count > 0)
        else:
            self._scope_editor_path = None
            self._scope_combo_dirty = False

    def _on_scope_combo_changed(self, _index: int) -> None:
        if self._syncing_scope_combo:
            return
        path = self._selected_path()
        if path:
            self._scope_editor_path = path
            self._scope_combo_dirty = True

    def _add_root(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "즐겨찾기 Root 선택")
        if not path:
            return
        if not self._repository.add_favorite(
            path, scope_mode=SCOPE_DEPTH, max_depth=5
        ):
            QMessageBox.information(self, "즐겨찾기 Root", "이미 등록된 경로입니다.")
            self._reload(selected_path=path)
            return
        self._reload(selected_path=path)
        self.changed.emit("add", path)

    def _remove_root(self) -> None:
        path = self._selected_path()
        if path and self._repository.remove_favorite(path):
            self._reload()
            self.changed.emit("remove", path)

    def _move_root(self, operation: str) -> None:
        path = self._selected_path()
        if path and self._repository.move_favorite(path, operation):
            self._reload(selected_path=path)
            self.changed.emit("move", path)

    def _apply_scope(self) -> None:
        path = self._selected_path()
        mode = str(self.scope_combo.currentData() or SCOPE_FULL)
        depth = self.scope_combo.currentData(self.SCOPE_DEPTH_ROLE)
        if path and self._repository.set_scope(
            path, mode, int(depth) if mode == SCOPE_DEPTH else None
        ):
            self._scope_editor_path = path
            self._scope_combo_dirty = False
            self._reload(selected_path=path)
            self.changed.emit("scope", path)

    def _emit_action(self, action: str) -> None:
        path = self._selected_path()
        if path:
            self.changed.emit(action, path)

    def _pause_root(self) -> None:
        path = self._selected_path()
        if path and self._repository.pause_root(path):
            self._reload(selected_path=path)
            self.changed.emit("pause", path)

    def _resume_root(self) -> None:
        path = self._selected_path()
        if path and self._repository.resume_root(path):
            self._reload(selected_path=path)
            self.changed.emit("resume", path)

    def _show_errors(self) -> None:
        path = self._selected_path()
        if not path:
            return
        errors = self._repository.list_errors(path)
        dialog = QDialog(self)
        dialog.setWindowTitle("폴더 색인 오류")
        dialog.resize(920, 430)
        layout = QVBoxLayout(dialog)
        table = QTableWidget(len(errors), 4, dialog)
        table.setHorizontalHeaderLabels(["종류", "폴더 경로", "재시도", "오류 내용"])
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        for row, error in enumerate(errors):
            table.setItem(row, 0, QTableWidgetItem(error.error_kind))
            table.setItem(row, 1, QTableWidgetItem(error.folder_path))
            table.setItem(row, 2, QTableWidgetItem(str(error.retry_count)))
            table.setItem(row, 3, QTableWidgetItem(error.message))
        layout.addWidget(table)
        buttons = QDialogButtonBox(QDialogButtonBox.Close, parent=dialog)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()


class PreflightDialog(QDialog):
    """Confirm the fully inventoried workload immediately before publishing."""

    def __init__(self, report: dict[str, Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("전송 전 사전 점검")
        self.resize(760, 560)
        layout = QVBoxLayout(self)

        total = int(report.get("message_count", 0))
        threshold = int(report.get("warning_threshold", 0))
        heading = QLabel(
            f"전체 모수 산정이 완료되었습니다. 최종 MQ 메시지 {total:,}건을 확인하세요."
        )
        heading.setWordWrap(True)
        layout.addWidget(heading)

        summary = QTableWidget(10, 2, self)
        self.summary_table = summary
        summary.setHorizontalHeaderLabels(["점검 항목", "결과"])
        summary.setEditTriggers(QAbstractItemView.NoEditTriggers)
        summary.setSelectionMode(QAbstractItemView.NoSelection)
        summary.verticalHeader().setVisible(False)
        summary.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        summary.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        summary_rows = [
            ("대기열", f"{int(report.get('folder_count', 0)):,}개 (보류 {int(report.get('held_folder_count', 0)):,}개)"),
            ("Recipe", f"{int(report.get('recipe_count', 0)):,}개"),
            ("고유 이미지", f"{int(report.get('image_count', 0)):,}개"),
            ("최종 메시지", f"{total:,}건"),
            ("이번 전송 대상", f"{int(report.get('dispatchable_message_count', total)):,}건"),
            ("Recipe 파일", f"누락 {len(report.get('missing_recipes', [])):,}개"),
            ("폴더 접근", f"접근 불가 {len(report.get('inaccessible_folders', [])):,}개"),
            ("이미지 접근", f"접근 불가 {int(report.get('inaccessible_image_count', 0)):,}개"),
            (
                "RabbitMQ",
                (
                    f"연결 가능 · Worker {report.get('worker_count', '-')} · "
                    f"대기 메시지 {report.get('queued_message_count', '-')}"
                    if report.get("broker_connected")
                    else "연결 실패"
                ),
            ),
            (
                "전송 정책",
                f"queue={report.get('request_queue', '')}, priority={report.get('priority', 0)}, "
                f"초기 {report.get('initial_open_folders', 0)}개 / 최대 {report.get('max_active_open_folders', 0)}개",
            ),
        ]
        for row_index, (label, value) in enumerate(summary_rows):
            summary.setItem(row_index, 0, QTableWidgetItem(label))
            summary.setItem(row_index, 1, QTableWidgetItem(value))
        layout.addWidget(summary)

        warnings: list[str] = []
        if report.get("threshold_exceeded"):
            warnings.append(f"설정된 대량 작업 확인 기준 {threshold:,}건 이상입니다.")
        warnings.extend(str(item) for item in report.get("issues", []))
        detail_paths = [
            *(f"Recipe 누락: {path}" for path in report.get("missing_recipes", [])[:10]),
            *(f"폴더 접근 불가: {path}" for path in report.get("inaccessible_folders", [])[:10]),
        ]
        notice = QTextEdit(self)
        notice.setReadOnly(True)
        notice.setPlainText("\n".join([*warnings, *detail_paths]) or "차단 또는 경고 항목이 없습니다.")
        notice.setMaximumHeight(150)
        layout.addWidget(notice)

        buttons = QDialogButtonBox(parent=self)
        self.start_button = buttons.addButton("확인 후 전송 시작", QDialogButtonBox.AcceptRole)
        cancel_button = buttons.addButton("취소", QDialogButtonBox.RejectRole)
        self.start_button.setEnabled(
            bool(report.get("broker_connected"))
            and int(report.get("dispatchable_message_count", total)) > 0
        )
        self.start_button.clicked.connect(self.accept)
        cancel_button.clicked.connect(self.reject)
        layout.addWidget(buttons)


class RunHistoryDialog(QDialog):
    """Browse persisted run aggregates and request CSV export."""

    export_requested = Signal(str)

    def __init__(self, rows: list[RunHistorySummary], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("실행 이력")
        self.resize(1100, 520)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"보존된 실행 이력 {len(rows)}건"))

        headers = [
            "시작", "종료", "상태", "대기열", "Recipe", "전체", "성공", "실패",
            "성공률", "평균 처리", "주요 오류",
        ]
        self.table = QTableWidget(len(rows), len(headers), self)
        self.table.setHorizontalHeaderLabels(headers)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self._session_ids: list[str] = []
        for row_index, item in enumerate(rows):
            self._session_ids.append(item.session_id)
            failed = item.fail + item.timeout + item.error + item.cancelled
            values = [
                format_seoul_display(item.created_at),
                format_seoul_display(item.ended_at) or "-",
                item.state,
                str(item.folder_count),
                str(item.recipe_count),
                str(item.total),
                str(item.success),
                str(failed),
                f"{item.success_rate:.1f}%",
                (
                    f"{item.avg_processing_seconds:.1f}s"
                    if item.avg_processing_seconds is not None
                    else "-"
                ),
                "; ".join(item.error_types[:3]) or "-",
            ]
            for column, value in enumerate(values):
                self.table.setItem(row_index, column, QTableWidgetItem(value))
        if rows:
            self.table.selectRow(0)
        layout.addWidget(self.table, stretch=1)

        buttons = QDialogButtonBox(QDialogButtonBox.Close, parent=self)
        self.export_button = buttons.addButton("선택 이력 CSV 내보내기", QDialogButtonBox.ActionRole)
        self.export_button.setEnabled(bool(rows))
        self.export_button.clicked.connect(self._emit_export)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _emit_export(self) -> None:
        row = self.table.currentRow()
        if 0 <= row < len(self._session_ids):
            self.export_requested.emit(self._session_ids[row])


class ResponsiveRecipeSettings(QWidget):
    """Keep Recipe and Priority controls readable at narrow widths."""

    COMPACT_WIDTH = 500

    def __init__(
        self,
        recipe_label: QLabel,
        recipe_selector: QWidget,
        priority_label: QLabel,
        priority_selector: QWidget,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.recipe_label = recipe_label
        self.recipe_selector = recipe_selector
        self.priority_label = priority_label
        self.priority_selector = priority_selector
        self._compact = False

        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(16)
        self.grid.setVerticalSpacing(8)
        self._apply_layout(compact=False)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._apply_layout(compact=event.size().width() < self.COMPACT_WIDTH)

    def _apply_layout(self, *, compact: bool) -> None:
        if compact == self._compact and self.grid.count() == 4:
            return
        self._compact = compact
        for widget in (
            self.recipe_label,
            self.recipe_selector,
            self.priority_label,
            self.priority_selector,
        ):
            self.grid.removeWidget(widget)

        self.grid.addWidget(self.recipe_label, 0, 0)
        self.grid.addWidget(
            self.recipe_selector,
            0,
            1,
            alignment=Qt.AlignLeft | Qt.AlignVCenter,
        )
        if compact:
            self.grid.addWidget(self.priority_label, 1, 0)
            self.grid.addWidget(self.priority_selector, 1, 1, alignment=Qt.AlignLeft)
        else:
            self.grid.addWidget(self.priority_label, 0, 2)
            self.grid.addWidget(
                self.priority_selector,
                0,
                3,
                alignment=Qt.AlignLeft | Qt.AlignVCenter,
            )
        for column in range(5):
            self.grid.setColumnStretch(column, 0)
        self.grid.setColumnStretch(4, 1)

    @property
    def is_compact(self) -> bool:
        return self._compact


class RecipePathRow(QFrame):
    """Render one selected Recipe and its path responsively."""

    COMPACT_WIDTH = 520
    layout_height_changed = Signal()

    def __init__(self, alias: str, path: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("recipePathRow")
        self.alias = alias
        self.path = path
        self._compact = False

        self.alias_label = QLabel(alias, self)
        self.alias_label.setObjectName("recipePathAlias")
        self.alias_label.setToolTip(alias)
        self.alias_label.setMinimumWidth(120)
        self.alias_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.alias_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)

        self.path_label = QLabel(path, self)
        self.path_label.setObjectName("recipePathValue")
        self.path_label.setToolTip(path)
        self.path_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.path_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)

        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(10, 7, 10, 7)
        self.grid.setHorizontalSpacing(12)
        self.grid.setVerticalSpacing(3)
        self._apply_layout(compact=False)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.set_available_width(event.size().width())
        self.grid.activate()
        self._update_elided_text()

    def set_available_width(self, width: int) -> None:
        """Select one- or two-line layout before the parent assigns final geometry."""

        self._apply_layout(compact=max(0, int(width)) < self.COMPACT_WIDTH)

    def _apply_layout(self, *, compact: bool) -> None:
        if compact == self._compact and self.grid.count() == 2:
            return
        self._compact = compact
        self.grid.removeWidget(self.alias_label)
        self.grid.removeWidget(self.path_label)
        if compact:
            self.alias_label.setMinimumWidth(0)
            self.alias_label.setMaximumWidth(16777215)
            self.alias_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            self.grid.setColumnMinimumWidth(0, 0)
            self.grid.addWidget(self.alias_label, 0, 0)
            self.grid.addWidget(self.path_label, 1, 0)
        else:
            self.alias_label.setMinimumWidth(160)
            self.alias_label.setMaximumWidth(180)
            self.alias_label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
            self.grid.setColumnMinimumWidth(0, 160)
            self.grid.addWidget(self.alias_label, 0, 0)
            self.grid.addWidget(self.path_label, 0, 1)
        self.grid.setColumnStretch(0, 0)
        self.grid.setColumnStretch(1, 0)
        self.grid.setColumnStretch(1 if not compact else 0, 1)
        line_height = max(
            self.alias_label.fontMetrics().height(),
            self.path_label.fontMetrics().height(),
        )
        content_height = line_height * (2 if compact else 1)
        required_height = 14 + content_height + (5 if compact else 0)
        self.setFixedHeight(required_height)
        self.grid.invalidate()
        self.updateGeometry()
        self.layout_height_changed.emit()

    def _update_elided_text(self) -> None:
        alias_width = max(0, self.alias_label.width() - 2)
        self.alias_label.setText(
            self.alias_label.fontMetrics().elidedText(self.alias, Qt.ElideRight, alias_width)
        )
        path_width = max(0, self.path_label.width() - 2)
        self.path_label.setText(
            self.path_label.fontMetrics().elidedText(self.path, Qt.ElideMiddle, path_width)
        )

    @property
    def is_compact(self) -> bool:
        return self._compact


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
    image_page_requested = Signal(int)
    move_folders_requested = Signal(list, str)
    hold_folders_requested = Signal(list, bool)
    history_requested = Signal()
    history_export_requested = Signal(str, str)

    def __init__(
        self,
        config: AppConfig,
        config_path: str | Path | None = None,
        folder_index_database_path: str | Path | None = None,
        ui_settings_path: str | Path | None = None,
    ) -> None:
        super().__init__()
        self._config = config
        self._config_path = Path(config_path) if config_path is not None else None
        self._folder_index_repository = FolderIndexRepository(
            folder_index_database_path or ":memory:"
        )
        self._folder_index_thread: QThread | None = None
        self._folder_index_worker: FolderIndexWorker | None = None
        self._pending_folder_index_job: tuple[list[str], bool, int | None, bool] | None = None
        self._folder_index_shutting_down = False
        self._ui_settings: QSettings | None = None
        if ui_settings_path is not None:
            settings_path = Path(ui_settings_path)
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            self._ui_settings = QSettings(str(settings_path), QSettings.IniFormat)
        self._folder_table_header_save_timer = QTimer(self)
        self._folder_table_header_save_timer.setSingleShot(True)
        self._folder_table_header_save_timer.setInterval(300)
        self._folder_table_header_save_timer.timeout.connect(
            self._save_folder_table_header_states
        )
        self._is_syncing_navigation = False
        self._active_result_queue: str | None = None
        self._is_syncing_folder_selection = False
        self._pending_jump_target: str | None = None
        self._pending_jump_show_feedback = False
        self._pending_jump_attempts = 0
        self._max_pending_jump_attempts = 10
        self._initial_scroll_alignment_done = False
        self._last_status_sidebar_width = 440
        self._help_dialog: HelpDialog | None = None
        self._history_dialog: RunHistoryDialog | None = None
        self._runtime_options_enabled = True
        self._image_page_has_more = False
        self._image_page_request_pending = False
        self._folder_search_page_limit = 200
        self._folder_search_total = 0
        self._folder_search_offset = 0
        self._recipe_actions: list[QAction] = []
        self._build_ui()
        self._build_menu_bar()
        self._apply_defaults()
        self._setup_folder_navigation_index()

    def _build_menu_bar(self) -> None:
        """Build top-level app actions."""

        self.task_menu = self.menuBar().addMenu("작업")
        self.action_open_history = QAction("실행 이력", self)
        self.action_open_history.triggered.connect(self.history_requested.emit)
        self.task_menu.addAction(self.action_open_history)

        self.settings_menu = self.menuBar().addMenu("설정")
        self.action_edit_app_config = QAction("MQ 연결 설정", self)
        self.action_edit_app_config.setEnabled(
            self._config_path is not None and self._config_path.exists()
        )
        self.action_edit_app_config.triggered.connect(self._open_app_config_settings)
        self.settings_menu.addAction(self.action_edit_app_config)
        self.action_edit_recipe_config = QAction("Recipe 설정", self)
        recipe_path = self._recipe_config_path()
        self.action_edit_recipe_config.setEnabled(
            self._config_path is not None
            and recipe_path is not None
            and recipe_path.exists()
        )
        self.action_edit_recipe_config.triggered.connect(self._open_recipe_config_settings)
        self.settings_menu.addAction(self.action_edit_recipe_config)

        self.help_menu = self.menuBar().addMenu("도움말")
        self.action_open_help = QAction("도움말 열기", self)
        self.action_open_help.setShortcut(QKeySequence.HelpContents)
        self.action_open_help.triggered.connect(self._open_help_dialog)
        self.help_menu.addAction(self.action_open_help)
        self.addAction(self.action_open_help)

        self.help_menu.addSeparator()
        self.action_check_update = QAction("업데이트 확인", self)
        self.action_check_update.setEnabled(self._config.update.enabled)
        self.action_check_update.triggered.connect(self._open_update_link)
        self.help_menu.addAction(self.action_check_update)

    def _recipe_config_path(self) -> Path | None:
        value = str(self._config.recipe_config_path or "").strip()
        return Path(value) if value else None

    def _open_app_config_settings(self) -> None:
        if self._config_path is None:
            QMessageBox.warning(self, "설정 파일 없음", "현재 실행에 사용된 app_config.yaml 경로를 찾지 못했습니다.")
            return
        try:
            dialog = AppConfigSettingsDialog(self._config_path, self)
        except (ConfigError, OSError) as exc:
            QMessageBox.critical(self, "설정 열기 실패", str(exc))
            return
        dialog.settings_saved.connect(self._on_runtime_settings_saved)
        dialog.exec()

    def _open_recipe_config_settings(self) -> None:
        recipe_path = self._recipe_config_path()
        if self._config_path is None or recipe_path is None:
            QMessageBox.warning(self, "설정 파일 없음", "현재 실행에 사용된 recipe_config.yaml 경로를 찾지 못했습니다.")
            return
        try:
            dialog = RecipeConfigSettingsDialog(
                self._config_path, recipe_path, self
            )
        except (ConfigError, OSError) as exc:
            QMessageBox.critical(self, "Recipe 설정 열기 실패", str(exc))
            return
        dialog.settings_saved.connect(self._on_runtime_settings_saved)
        dialog.exec()

    def _on_runtime_settings_saved(self, path: str) -> None:
        self.append_log(f"[설정] 저장 완료 · 재시작 후 적용: {path}")

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802
        """Align horizontal scroll positions once when the main window is first shown."""

        super().showEvent(event)
        self._apply_initial_scroll_alignment_once()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        """Stop folder-index work before Qt destroys its thread objects."""

        self._save_folder_table_header_states()
        self.shutdown_folder_navigation()
        super().closeEvent(event)

    def _setup_folder_navigation_index(self) -> None:
        """Wire persistent favorite roots, cache search, and refresh scheduling."""

        self._folder_search_debounce = QTimer(self)
        self._folder_search_debounce.setSingleShot(True)
        self._folder_search_debounce.setInterval(350)
        self._folder_search_debounce.timeout.connect(self._run_live_folder_search)

        self._folder_watcher_debounce = QTimer(self)
        self._folder_watcher_debounce.setSingleShot(True)
        self._folder_watcher_debounce.setInterval(1200)
        self._folder_watcher_debounce.timeout.connect(self._refresh_favorite_roots_incrementally)

        self._folder_periodic_refresh = QTimer(self)
        self._folder_periodic_refresh.setInterval(5 * 60 * 1000)
        self._folder_periodic_refresh.timeout.connect(self._refresh_favorite_roots_incrementally)
        self._folder_periodic_refresh.start()

        self._favorite_root_watcher = QFileSystemWatcher(self)
        self._favorite_root_watcher.directoryChanged.connect(
            lambda _path: self._folder_watcher_debounce.start()
        )

        self.btn_manage_favorite_roots.clicked.connect(self._open_favorite_root_manager)
        self.btn_refresh_favorite_roots.clicked.connect(self._refresh_selected_favorite_roots)
        self.favorite_root_list.itemClicked.connect(self._on_favorite_root_clicked)
        self.favorite_root_list.itemSelectionChanged.connect(self._sync_favorite_buttons)
        self.folder_search_edit.textChanged.connect(self._on_folder_search_changed)
        self.folder_search_results.itemClicked.connect(self._on_folder_search_result_clicked)
        self.btn_cancel_folder_search.clicked.connect(self._cancel_folder_search)
        self.btn_more_folder_search.clicked.connect(self._load_more_folder_search_results)

        self._reload_favorite_roots()
        resumable = [
            favorite.path
            for favorite in self._folder_index_repository.list_favorites()
            if favorite.scope_mode != SCOPE_EXCLUDED
            and favorite.index_status != INDEX_PAUSED
        ]
        if resumable:
            QTimer.singleShot(
                0,
                lambda: self._start_folder_index_job(
                    resumable, full=False, max_folders_per_root=None, retry_errors=True
                ),
            )

    def _reload_favorite_roots(self, selected_path: str | None = None) -> None:
        favorites = self._folder_index_repository.list_favorites()
        current_item = self.favorite_root_list.currentItem()
        preserved = selected_path or (
            str(current_item.data(Qt.UserRole)) if current_item is not None else ""
        )
        self.favorite_root_list.clear()
        for favorite in favorites:
            status = "●" if favorite.online else "○"
            suffix = _folder_index_status_label(favorite.index_status)
            item = QListWidgetItem(f"{status} {favorite.path} · {suffix}")
            item.setData(Qt.UserRole, favorite.path)
            checked = (
                format_seoul_display(favorite.last_checked)
                or "아직 확인하지 않음"
            )
            item.setToolTip(f"{favorite.path}\n마지막 확인: {checked}")
            self.favorite_root_list.addItem(item)
            if favorite.path == preserved:
                self.favorite_root_list.setCurrentItem(item)
        self._sync_favorite_root_watcher(favorites)
        self._sync_favorite_buttons()
        if not favorites and not self.folder_search_edit.text().strip():
            self.folder_search_status.setText(
                "즐겨찾기 Root를 추가하면 빠른 검색을 사용할 수 있습니다."
            )

    def _sync_favorite_root_watcher(self, favorites) -> None:  # noqa: ANN001
        watched = self._favorite_root_watcher.directories()
        if watched:
            self._favorite_root_watcher.removePaths(watched)
        online_paths = [favorite.path for favorite in favorites if os.path.isdir(favorite.path)]
        if online_paths:
            self._favorite_root_watcher.addPaths(online_paths)

    def _sync_favorite_buttons(self) -> None:
        count = self.favorite_root_list.count()
        self.btn_refresh_favorite_roots.setEnabled(count > 0)

    def _selected_favorite_root(self) -> str | None:
        item = self.favorite_root_list.currentItem()
        if item is None:
            return None
        path = str(item.data(Qt.UserRole) or "").strip()
        return path or None

    def _open_favorite_root_manager(self) -> None:
        dialog = FavoriteRootManagerDialog(self._folder_index_repository, self)
        dialog.changed.connect(self._on_favorite_roots_managed)
        dialog.exec()
        self._reload_favorite_roots()
        self._show_cached_folder_search()

    def _on_favorite_roots_managed(self, action: str, path: str) -> None:
        if action == "remove":
            self._cancel_active_folder_index_job(clear_pending=True)
            self.append_log(f"[폴더 검색] 즐겨찾기 Root 삭제: {path}")
        elif action == "add":
            self.append_log(f"[폴더 검색] 즐겨찾기 Root 추가: {path}")
        self._reload_favorite_roots(selected_path=path if action != "remove" else None)
        self._show_cached_folder_search()
        if action in {"add", "reindex"}:
            self._start_folder_index_job([path], full=True)
        elif action in {"refresh", "resume", "scope"}:
            favorite = self._folder_index_repository.get_favorite(path)
            if favorite and favorite.scope_mode != SCOPE_EXCLUDED:
                self._start_folder_index_job([path], full=False, retry_errors=True)
        elif action == "pause":
            self._cancel_active_folder_index_job(clear_pending=False)

    def _on_favorite_root_clicked(self, item: QListWidgetItem) -> None:
        path = str(item.data(Qt.UserRole) or "")
        if not os.path.isdir(path):
            self.folder_search_status.setText(f"오프라인 Root: {path}")
            return
        self.jump_to_path(path, show_feedback=True)

    def _refresh_selected_favorite_roots(self) -> None:
        selected = self._selected_favorite_root()
        roots = [selected] if selected else [
            favorite.path for favorite in self._folder_index_repository.list_favorites()
        ]
        self._start_folder_index_job(
            [path for path in roots if path],
            full=True,
        )

    def _refresh_favorite_roots_incrementally(self) -> None:
        roots = [favorite.path for favorite in self._folder_index_repository.list_favorites()]
        if roots:
            self._start_folder_index_job(
                roots,
                full=False,
                max_folders_per_root=2000,
                retry_errors=False,
            )

    def _on_folder_search_changed(self, query: str) -> None:
        if self._is_syncing_navigation:
            return
        self._show_cached_folder_search()
        self._folder_search_debounce.stop()
        if not query.strip():
            self.btn_more_folder_search.hide()
            return
        self._folder_search_debounce.start()

    def _show_cached_folder_search(self) -> None:
        query = self.folder_search_edit.text().strip()
        if not query:
            self.folder_search_results.clear()
            self.folder_search_results.hide()
            self.btn_more_folder_search.hide()
            favorite_count = len(self._folder_index_repository.list_favorites())
            self.folder_search_status.setText(
                "검색어를 입력하세요."
                if favorite_count
                else "즐겨찾기 Root를 추가하면 빠른 검색을 사용할 수 있습니다."
            )
            return
        normalized_path = self._normalize_navigation_path(query)
        if os.path.isdir(normalized_path):
            self.folder_search_results.clear()
            self.folder_search_results.hide()
            self.folder_search_status.setText("Enter 또는 검색을 누르면 경로로 이동합니다.")
            return
        page = self._folder_index_repository.search_page(
            query, limit=self._folder_search_page_limit
        )
        self._folder_search_offset = len(page.results)
        self._folder_search_total = page.total_count
        self._render_folder_search_results(page.results)
        self.btn_more_folder_search.setVisible(page.has_more)
        self.folder_search_status.setText(
            f"캐시 결과 {len(page.results):,}/{page.total_count:,}개"
        )

    def _run_live_folder_search(self) -> None:
        query = self.folder_search_edit.text().strip()
        if not query or os.path.isdir(self._normalize_navigation_path(query)):
            return
        self._show_cached_folder_search()

    def _load_more_folder_search_results(self) -> None:
        query = self.folder_search_edit.text().strip()
        if not query:
            return
        page = self._folder_index_repository.search_page(
            query,
            limit=self._folder_search_page_limit,
            offset=self._folder_search_offset,
        )
        self._append_folder_search_results(page.results)
        self._folder_search_offset += len(page.results)
        self._folder_search_total = page.total_count
        self.btn_more_folder_search.setVisible(page.has_more)
        self.folder_search_status.setText(
            f"캐시 결과 {self._folder_search_offset:,}/{page.total_count:,}개"
        )

    def _render_folder_search_results(self, results: list[FolderSearchResult]) -> None:
        self.folder_search_results.clear()
        self._append_folder_search_results(results)

    def _append_folder_search_results(self, results: list[FolderSearchResult]) -> None:
        for result in results:
            status = "" if result.root_online else "[오프라인] "
            item = QListWidgetItem(f"{status}{result.name}  —  {result.parent_path}")
            item.setData(Qt.UserRole, result.path)
            item.setData(Qt.UserRole + 1, result.root_path)
            item.setToolTip(result.path)
            self.folder_search_results.addItem(item)
        self.folder_search_results.setVisible(self.folder_search_results.count() > 0)

    def _on_folder_search_result_clicked(self, item: QListWidgetItem) -> None:
        path = str(item.data(Qt.UserRole) or "")
        root_path = str(item.data(Qt.UserRole + 1) or "")
        if self._folder_index_repository.path_is_directory(path):
            self.jump_to_path(path, show_feedback=True)
            return
        self._show_path_error(f"검색 결과 폴더가 현재 존재하지 않습니다: {path}")
        if root_path:
            self._folder_index_repository.mark_path_missing(root_path, path)
            self._show_cached_folder_search()

    def _start_folder_index_job(
        self,
        root_paths: list[str],
        *,
        full: bool,
        max_folders_per_root: int | None = None,
        retry_errors: bool = False,
    ) -> None:
        roots = list(dict.fromkeys(path for path in root_paths if path))
        if self._folder_index_shutting_down or not roots:
            return
        if self._folder_index_thread is not None:
            if self._pending_folder_index_job is None:
                self._pending_folder_index_job = (
                    roots, bool(full), max_folders_per_root, bool(retry_errors)
                )
            else:
                pending_roots, pending_full, pending_max, pending_retry = (
                    self._pending_folder_index_job
                )
                combined_full = pending_full or bool(full)
                combined_max = (
                    None
                    if combined_full or pending_max is None or max_folders_per_root is None
                    else max(pending_max, max_folders_per_root)
                )
                self._pending_folder_index_job = (
                    list(dict.fromkeys([*pending_roots, *roots])),
                    combined_full,
                    combined_max,
                    pending_retry or bool(retry_errors),
                )
            return

        thread = QThread(self)
        worker = FolderIndexWorker(
            self._folder_index_repository,
            roots,
            full=full,
            max_folders_per_root=max_folders_per_root,
            retry_errors=retry_errors,
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._on_folder_index_progress)
        worker.completed.connect(self._on_folder_index_completed)
        worker.failed.connect(self._on_folder_index_failed)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._on_folder_index_thread_finished)
        self._folder_index_thread = thread
        self._folder_index_worker = worker
        self.btn_cancel_folder_search.setEnabled(True)
        thread.start(QThread.LowPriority)

    def _on_folder_index_progress(self, root_path: str, count: int) -> None:
        favorite = self._folder_index_repository.get_favorite(root_path)
        if favorite:
            self.folder_search_status.setText(
                f"백그라운드 색인 중 · {Path(root_path).name or root_path} · "
                f"색인 {favorite.indexed_count:,} · 대기 {favorite.pending_count:,} · "
                f"오류 {favorite.error_count:,}"
            )

    def _on_folder_index_completed(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        self._reload_favorite_roots()
        self._show_cached_folder_search()
        outcomes = list(payload.get("outcomes") or [])
        if not self.folder_search_edit.text().strip() and outcomes:
            indexed = sum(outcome.indexed_count for outcome in outcomes)
            pending = sum(outcome.pending_count for outcome in outcomes)
            errors = sum(outcome.error_count for outcome in outcomes)
            self.folder_search_status.setText(
                f"색인 상태 · 등록 {indexed:,} · 대기 {pending:,} · 오류 {errors:,}"
            )

    def _on_folder_index_failed(self, message: str) -> None:
        self.folder_search_status.setText(f"폴더 인덱스 갱신 실패: {message}")
        self.append_log(f"[폴더 검색] 인덱스 갱신 실패: {message}")

    def _on_folder_index_thread_finished(self) -> None:
        self._folder_index_thread = None
        self._folder_index_worker = None
        self.btn_cancel_folder_search.setEnabled(False)
        pending = self._pending_folder_index_job
        self._pending_folder_index_job = None
        if pending and not self._folder_index_shutting_down:
            roots, full, max_folders, retry_errors = pending
            self._start_folder_index_job(
                roots,
                full=full,
                max_folders_per_root=max_folders,
                retry_errors=retry_errors,
            )

    def _cancel_folder_search(self) -> None:
        self._folder_search_debounce.stop()
        for favorite in self._folder_index_repository.list_favorites():
            if favorite.index_status == INDEX_INDEXING:
                self._folder_index_repository.pause_root(favorite.path)
        self._cancel_active_folder_index_job(clear_pending=True)
        self.btn_cancel_folder_search.setEnabled(False)
        self.folder_search_status.setText(
            f"색인 일시정지 요청 · 캐시 결과 {self.folder_search_results.count():,}개 유지"
        )

    def _cancel_active_folder_index_job(self, *, clear_pending: bool) -> None:
        if clear_pending:
            self._pending_folder_index_job = None
        if self._folder_index_worker is not None:
            self._folder_index_worker.cancel()

    def shutdown_folder_navigation(self) -> None:
        """Stop background indexing and close its independent SQLite connection."""

        if self._folder_index_shutting_down:
            return
        self._folder_index_shutting_down = True
        self._folder_periodic_refresh.stop()
        self._folder_search_debounce.stop()
        self._folder_watcher_debounce.stop()
        self._pending_folder_index_job = None
        if self._folder_index_worker is not None:
            self._folder_index_worker.cancel()
        thread = self._folder_index_thread
        if thread is not None and thread.isRunning():
            thread.quit()
            thread.wait(3000)
        if thread is None or not thread.isRunning():
            self._folder_index_repository.close()

    def _build_ui(self) -> None:
        self.setWindowTitle(self._config.ui.app_name)
        # Keep the existing layout-derived minimum while leaving expansion unrestricted.
        self.setMaximumSize(16_777_215, 16_777_215)
        self.resize(self._config.ui.window_width, self._config.ui.window_height)

        root = QWidget(self)
        root.setObjectName("appRoot")
        self.setCentralWidget(root)

        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(14, 14, 14, 14)
        root_layout.setSpacing(12)

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
        self.main_splitter.setSizes([340, 900, self._last_status_sidebar_width])

        self.left_panel = left_panel
        self.center_panel = center_panel
        self.status_sidebar_panel = status_sidebar

        root_layout.addWidget(self.main_splitter, stretch=1)

    def _build_left_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("leftPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(11)

        title = QLabel("폴더 탐색")
        title.setObjectName("panelTitle")
        layout.addWidget(title)

        favorite_header = QHBoxLayout()
        self.btn_manage_favorite_roots = QPushButton("즐겨찾기 관리")
        self.btn_refresh_favorite_roots = QPushButton("새로고침")
        for button, tooltip in (
            (self.btn_manage_favorite_roots, "즐겨찾기 Root 목록 관리"),
            (self.btn_refresh_favorite_roots, "선택 또는 전체 Root를 다시 색인"),
        ):
            button.setToolTip(tooltip)
            button.setMinimumWidth(0)
            button.setFixedHeight(30)
            button.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
            favorite_header.addWidget(button)
        favorite_header.addStretch(1)
        layout.addLayout(favorite_header)

        self.favorite_root_list = QListWidget()
        self.favorite_root_list.setObjectName("favoriteRootList")
        self.favorite_root_list.setMaximumHeight(92)
        self.favorite_root_list.setSelectionMode(QAbstractItemView.SingleSelection)
        layout.addWidget(self.favorite_root_list)

        search_row = QHBoxLayout()
        self.path_jump_edit = QLineEdit()
        self.path_jump_edit.setPlaceholderText(
            r"폴더 경로 이동 또는 즐겨찾기 내 폴더 검색"
        )
        self.path_jump_edit.returnPressed.connect(self._on_path_jump_requested)
        self.folder_search_edit = self.path_jump_edit
        self.btn_path_jump = QPushButton("검색")
        self.btn_path_jump.clicked.connect(self._on_path_jump_requested)
        self.btn_cancel_folder_search = QPushButton("색인 일시정지")
        self.btn_cancel_folder_search.setEnabled(False)
        search_row.addWidget(self.path_jump_edit, stretch=1)
        search_row.addWidget(self.btn_path_jump)
        search_row.addWidget(self.btn_cancel_folder_search)
        layout.addLayout(search_row)

        self.folder_search_status = QLabel("즐겨찾기 Root를 추가하면 빠른 검색을 사용할 수 있습니다.")
        self.folder_search_status.setObjectName("folderSearchStatus")
        self.folder_search_status.setWordWrap(True)
        layout.addWidget(self.folder_search_status)

        self.folder_search_results = QListWidget()
        self.folder_search_results.setObjectName("folderSearchResults")
        self.folder_search_results.setMaximumHeight(132)
        self.folder_search_results.hide()
        layout.addWidget(self.folder_search_results)

        self.btn_more_folder_search = QPushButton("검색 결과 더 보기")
        self.btn_more_folder_search.hide()
        layout.addWidget(self.btn_more_folder_search)

        self.folder_tree = FolderTreeView()
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
        self.path_jump_edit.clear()

        layout.addWidget(self.folder_tree, stretch=1)

        self.btn_add_folder = QPushButton("폴더 추가")
        self.btn_add_subfolders = QPushButton("하위 폴더 추가")
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
        panel.setObjectName("centerPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(11)

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
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(11)

        connection_row = QHBoxLayout()
        connection_row.setSpacing(12)

        self.connection_label = QLabel(self._build_connection_status_text("대기"))
        self.connection_label.setObjectName("connectionStatus")
        self.connection_label.setWordWrap(True)
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

        recipe_label = QLabel("Recipe")
        self.recipe_multi_button = QToolButton()
        self.recipe_multi_button.setObjectName("recipeMultiButton")
        self.recipe_multi_button.setPopupMode(QToolButton.InstantPopup)
        recipe_menu = QMenu(self.recipe_multi_button)
        recipe_menu.setObjectName("recipeComboMenu")
        self.recipe_multi_button.setMenu(recipe_menu)
        self.recipe_multi_button.setMinimumWidth(220)
        self.recipe_multi_button.setMaximumWidth(280)
        self.recipe_multi_button.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.recipe_multi_button.setAccessibleName("Recipe 선택")

        priority_label = QLabel("Priority")
        self.priority_combo = QComboBox()
        self.priority_combo.setObjectName("priorityCombo")
        priority_arrow_path = resolve_ui_icon_path("combo_down.svg")
        if priority_arrow_path is not None:
            priority_arrow_url = priority_arrow_path.as_posix()
            self.priority_combo.setStyleSheet(
                f"""
                QComboBox#priorityCombo {{
                    padding-right: 27px;
                }}
                QComboBox#priorityCombo::drop-down {{
                    subcontrol-origin: padding;
                    subcontrol-position: right center;
                    width: 18px;
                    margin-right: 5px;
                    border: none;
                }}
                QComboBox#priorityCombo::down-arrow {{
                    image: url("{priority_arrow_url}");
                    width: 8px;
                    height: 5px;
                }}
                """
            )
        self.priority_combo.setMinimumContentsLength(3)
        self.priority_combo.setFixedWidth(80)
        control_height = max(
            self.recipe_multi_button.minimumSizeHint().height(),
            self.priority_combo.sizeHint().height(),
        )
        self.recipe_multi_button.setFixedHeight(control_height)
        self.priority_combo.setFixedHeight(control_height)

        self.recipe_settings = ResponsiveRecipeSettings(
            recipe_label,
            self.recipe_multi_button,
            priority_label,
            self.priority_combo,
            panel,
        )
        layout.addWidget(self.recipe_settings)

        recipe_paths_label = QLabel("Recipe 선택 경로")
        recipe_paths_label.setObjectName("recipePathsTitle")
        layout.addWidget(recipe_paths_label)

        self.recipe_paths_scroll = QScrollArea(panel)
        self.recipe_paths_scroll.setObjectName("recipePathsScroll")
        self.recipe_paths_scroll.setWidgetResizable(True)
        self.recipe_paths_scroll.setFrameShape(QFrame.NoFrame)
        self.recipe_paths_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.recipe_paths_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)

        self.recipe_paths_panel = QFrame()
        self.recipe_paths_panel.setObjectName("recipePathsPanel")
        self.recipe_paths_layout = QVBoxLayout(self.recipe_paths_panel)
        self.recipe_paths_layout.setContentsMargins(6, 6, 6, 6)
        self.recipe_paths_layout.setSpacing(6)
        self.recipe_path_empty_label = QLabel("선택된 Recipe가 없습니다.", self.recipe_paths_panel)
        self.recipe_path_empty_label.setObjectName("recipePathEmpty")
        self.recipe_paths_layout.addWidget(self.recipe_path_empty_label)
        self.recipe_path_rows: list[RecipePathRow] = []
        self.recipe_paths_scroll.setWidget(self.recipe_paths_panel)
        layout.addWidget(self.recipe_paths_scroll)

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
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(9)

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
            settings_key="active",
        )
        # Keep active list visibly larger from first render as requested.
        self.active_folder_table.setMinimumHeight(198)
        self.active_folder_table.selectionModel().selectionChanged.connect(self._on_active_folder_selection_changed)
        self.active_folder_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.active_folder_table.customContextMenuRequested.connect(self._on_active_folder_context_menu)
        # Keep backward-compatible attribute name.
        self.folder_table = self.active_folder_table
        layout.addWidget(self.active_folder_table, stretch=11)

        action_row = QHBoxLayout()
        self.btn_move_folder_top = QPushButton()
        self.btn_move_folder_up = QPushButton()
        self.btn_move_folder_down = QPushButton()
        self.btn_move_folder_bottom = QPushButton()
        self.btn_hold_folders = QPushButton()
        self.btn_release_folders = QPushButton()
        folder_action_buttons = (
            (self.btn_move_folder_top, "folder_move_top.svg", "맨 위로 이동"),
            (self.btn_move_folder_up, "folder_move_up.svg", "위로 이동"),
            (self.btn_move_folder_down, "folder_move_down.svg", "아래로 이동"),
            (self.btn_move_folder_bottom, "folder_move_bottom.svg", "맨 아래로 이동"),
            (self.btn_hold_folders, "folder_hold.svg", "보류"),
            (self.btn_release_folders, "folder_release.svg", "보류 해제"),
        )
        for index, (button, icon_name, label) in enumerate(folder_action_buttons):
            button.setObjectName("folderActionIconButton")
            button.setIcon(self._load_sidebar_toggle_icon(icon_name))
            button.setIconSize(QSize(22, 22))
            button.setToolTip(label)
            button.setAccessibleName(label)
            button.setFixedSize(36, 34)
            button.setEnabled(False)
            action_row.addWidget(button)
            if index == 3:
                action_row.addSpacing(6)
        self.btn_move_folder_top.clicked.connect(lambda: self._emit_folder_move("top"))
        self.btn_move_folder_up.clicked.connect(lambda: self._emit_folder_move("up"))
        self.btn_move_folder_down.clicked.connect(lambda: self._emit_folder_move("down"))
        self.btn_move_folder_bottom.clicked.connect(lambda: self._emit_folder_move("bottom"))
        self.btn_hold_folders.clicked.connect(lambda: self._emit_folder_hold(True))
        self.btn_release_folders.clicked.connect(lambda: self._emit_folder_hold(False))
        action_row.addStretch(1)
        self.btn_delete_active_folders = QPushButton("선택 삭제")
        self.btn_delete_active_folders.setEnabled(False)
        self.btn_delete_active_folders.clicked.connect(self._on_delete_active_folders_clicked)
        action_row.addWidget(self.btn_delete_active_folders)
        layout.addLayout(action_row)

        completed_label = QLabel("완료된 폴더")
        completed_label.setObjectName("subPanelTitle")
        layout.addWidget(completed_label)

        self.completed_folder_table = self._create_folder_table(
            self.completed_folder_table_model,
            settings_key="completed",
        )
        # Preserve completed-list readability without stealing too much initial height.
        self.completed_folder_table.setMinimumHeight(148)
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
        settings_key: str = "",
    ) -> QTableView:
        """Create one folder table with shared visual/column policy."""

        table = QTableView()
        table.setModel(model)
        table.setSelectionBehavior(QTableView.SelectRows)
        table.setSelectionMode(selection_mode)
        table.setAlternatingRowColors(True)
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(36)
        table.verticalHeader().setMinimumSectionSize(32)
        table.setWordWrap(False)
        table.setTextElideMode(Qt.ElideRight)
        table.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        table.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        table.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)

        header = table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.setMinimumSectionSize(64)
        default_widths = (100, 200, 180, 380, 240, 80, 80, 80, 80, 110)
        for column, width in enumerate(default_widths):
            table.setColumnWidth(column, width)
        table.setItemDelegateForColumn(1, ProgressBarDelegate(table))
        table.setItemDelegateForColumn(2, StatusBadgeDelegate(table))
        self._restore_folder_table_header_state(table, settings_key)
        header.sectionResized.connect(
            lambda *_args: self._folder_table_header_save_timer.start()
        )
        return table

    def _restore_folder_table_header_state(
        self,
        table: QTableView,
        settings_key: str,
    ) -> None:
        """Restore one user-resized folder table header when available."""

        if self._ui_settings is None or not settings_key:
            return
        state = self._ui_settings.value(f"folder_tables/{settings_key}/header_state")
        if isinstance(state, QByteArray) and not state.isEmpty():
            table.horizontalHeader().restoreState(state)
        elif isinstance(state, (bytes, bytearray)) and state:
            table.horizontalHeader().restoreState(QByteArray(bytes(state)))

    def _save_folder_table_header_states(self) -> None:
        """Persist independently resized active/completed folder table columns."""

        if self._ui_settings is None:
            return
        tables = (
            ("active", getattr(self, "active_folder_table", None)),
            ("completed", getattr(self, "completed_folder_table", None)),
        )
        for settings_key, table in tables:
            if table is not None:
                self._ui_settings.setValue(
                    f"folder_tables/{settings_key}/header_state",
                    table.horizontalHeader().saveState(),
                )
        self._ui_settings.sync()

    def _build_bottom_panel(self) -> QWidget:
        panel = QGroupBox("상태 및 로그")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(9)

        self.status_tabs = QTabWidget()
        self.status_tabs.setObjectName("bottomTabs")

        self.image_table_model = ImageTableModel()
        self.image_table = QTableView()
        self.image_table.setModel(self.image_table_model)
        self.image_table.setSelectionBehavior(QTableView.SelectRows)
        self.image_table.setAlternatingRowColors(True)
        self.image_table.verticalHeader().setVisible(False)
        self.image_table.verticalHeader().setDefaultSectionSize(36)
        self.image_table.verticalScrollBar().valueChanged.connect(self._on_image_table_scrolled)
        self.image_table.verticalHeader().setMinimumSectionSize(32)
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

        detail_tab = QWidget()
        detail_layout = QVBoxLayout(detail_tab)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.addWidget(self.image_table)

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setPlaceholderText("작업 로그가 여기에 표시됩니다.")
        self.log_text.setLineWrapMode(QTextEdit.NoWrap)
        self.log_text.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.log_text.document().setMaximumBlockCount(
            max(100, int(self._config.publish.ui_log_max_lines))
        )

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
        self.set_runtime_options_enabled(True)

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

        self._is_syncing_navigation = True
        try:
            self.path_jump_edit.setText(target_path)
        finally:
            self._is_syncing_navigation = False

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
        selections = self.current_recipe_selections()
        recipe_path = selections[0][1] if selections else ""
        if not recipe_path:
            recipe_path = self._config.recipe_config.default_path

        polling_interval = max(1, int(self._config.publish.polling_interval_seconds))

        try:
            priority = max(0, int(self.priority_combo.currentText().strip() or "0"))
        except ValueError:
            priority = 0

        return action, recipe_path, polling_interval, priority

    def current_recipe_selections(self) -> list[tuple[str, str]]:
        """Return currently selected recipe alias/path pairs in display order."""

        return [
            (action.text().strip() or str(action.data()), str(action.data() or "").strip())
            for action in self._recipe_actions
            if action.isChecked() and str(action.data() or "").strip()
        ]

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
        return (
            f"연결 상태: {status_label}\n"
            f"host: {host}:{port} | request_queue: {request_queue}"
        )

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
        total_final = bool(stats.get("total_final", True))

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

        if not total_final:
            self.overall_progress.setValue(0)
            self.overall_label.setText(
                f"전체 진행률 집계 중 ({completed}/{total} 확인, 전체 모수 산정 중)"
            )
            return

        self.overall_progress.setValue(progress)
        self.overall_label.setText(
            f"전체 진행률 {float(stats.get('progress', 0.0)):.1f}% ({completed}/{total}) "
            f"| Avg Time/Image {avg_text} | ETA {eta_text}"
        )

    def set_running_state(self, running: bool) -> None:
        """Toggle buttons based on active task flow."""

        self.btn_start.setText("전송 시작")
        self.btn_stop.setText("일시정지")
        self.btn_start.setEnabled(not running)
        self.btn_stop.setEnabled(running)
        self.btn_add_folder.setEnabled(True)
        self.btn_add_subfolders.setEnabled(True)

    def set_paused_state(self, paused: bool) -> None:
        """Render a persisted user pause independently from active worker state."""

        self.btn_start.setText("전송 재개" if paused else "전송 시작")
        self.btn_start.setEnabled(True)
        self.btn_stop.setText("일시정지")
        self.btn_stop.setEnabled(False)
        self.set_runtime_options_enabled(not paused)

    def set_runtime_options_enabled(self, enabled: bool) -> None:
        """Enable/disable runtime-editable controls for session stability."""

        self._runtime_options_enabled = bool(enabled)
        self.recipe_multi_button.setEnabled(enabled)
        self.priority_combo.setEnabled(enabled)

    def set_folder_rows(self, rows: list[FolderSummary]) -> None:
        """Replace folder table rows."""

        selected_paths, current_path = self._active_folder_selection_state()
        active_rows = [row for row in rows if not row.status.is_done]
        completed_rows = [row for row in rows if row.status.is_done]
        self._is_syncing_folder_selection = True
        try:
            self.active_folder_table_model.set_rows(active_rows)
            self.completed_folder_table_model.set_rows(completed_rows)
            self._restore_active_folder_selection(selected_paths, current_path)
        finally:
            self._is_syncing_folder_selection = False
        if selected_paths:
            self._on_active_folder_selection_changed()
        if not rows:
            self.active_folder_table.clearSelection()
            self.completed_folder_table.clearSelection()

    def _active_folder_selection_state(self) -> tuple[list[str], str | None]:
        """Capture selected queue keys and the current key before a model reset."""

        selected_paths = self._selected_folder_paths_from_table(
            self.active_folder_table, self.active_folder_table_model
        )
        current_index = self.active_folder_table.currentIndex()
        current_path = (
            self.active_folder_table_model.folder_at(current_index.row())
            if current_index.isValid()
            else None
        )
        return selected_paths, current_path

    def _restore_active_folder_selection(
        self,
        selected_paths: list[str],
        current_path: str | None,
    ) -> list[str]:
        """Re-select active folders at their new sorted rows and keep them visible."""

        selection_model = self.active_folder_table.selectionModel()
        if selection_model is None or not selected_paths:
            return []
        restored_paths: list[str] = []
        indexes: dict[str, QModelIndex] = {}
        for folder_path in selected_paths:
            row = self.active_folder_table_model.row_for_folder_path(folder_path)
            if row is None:
                continue
            index = self.active_folder_table_model.index(row, 0)
            selection_model.select(
                index, QItemSelectionModel.Select | QItemSelectionModel.Rows
            )
            indexes[folder_path] = index
            restored_paths.append(folder_path)
        if not restored_paths:
            return []
        focused_path = current_path if current_path in indexes else restored_paths[0]
        focused_index = indexes[focused_path]
        selection_model.setCurrentIndex(focused_index, QItemSelectionModel.NoUpdate)
        self.active_folder_table.scrollTo(
            focused_index, QAbstractItemView.EnsureVisible
        )
        return restored_paths

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
        self._image_page_has_more = False
        self._image_page_request_pending = False
        if not tasks:
            self.image_table.clearSelection()

    def set_image_task_page(
        self,
        tasks: list[ImageTask],
        total_count: int,
        append: bool = False,
    ) -> None:
        """Set or append one bounded detail page and track whether more rows exist."""

        if append:
            self.image_table_model.append_tasks(tasks)
        else:
            self.image_table_model.set_tasks(tasks)
        self._image_page_has_more = self.image_table_model.rowCount() < max(0, int(total_count))
        self._image_page_request_pending = False
        if not tasks and not append:
            self.image_table.clearSelection()

    def _on_image_table_scrolled(self, value: int) -> None:
        scrollbar = self.image_table.verticalScrollBar()
        if (
            not self._image_page_has_more
            or self._image_page_request_pending
            or value < scrollbar.maximum()
        ):
            return
        self._image_page_request_pending = True
        self.image_page_requested.emit(self.image_table_model.rowCount())

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
        for button in (
            self.btn_move_folder_top,
            self.btn_move_folder_up,
            self.btn_move_folder_down,
            self.btn_move_folder_bottom,
            self.btn_hold_folders,
            self.btn_release_folders,
        ):
            button.setEnabled(False)

    def set_active_result_queue(self, queue_name: str | None) -> None:
        """Track currently active result queue for MQ preview dialog."""

        self._active_result_queue = queue_name

    def show_mq_preview(self, preview_data: dict[str, Any]) -> None:
        """Open modal dialog for one task's MQ preview information."""

        dialog = MQPreviewDialog(preview_data=preview_data, parent=self)
        dialog.exec()

    def show_duplicate_folders(self, rows: list[tuple[str, str]]) -> None:
        """Show paths skipped because they already exist in a folder table."""

        dialog = DuplicateFolderDialog(rows=rows, parent=self)
        dialog.exec()

    def confirm_resume_paused(self, pending_count: int) -> bool:
        result = QMessageBox.question(
            self,
            "중지된 작업 발견",
            f"사용자가 일시정지한 작업 {max(0, int(pending_count)):,}건이 있습니다.\n전송을 재개할까요?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        return result == QMessageBox.Yes

    def confirm_preflight(self, report: dict[str, Any]) -> bool:
        dialog = PreflightDialog(report, self)
        return dialog.exec() == QDialog.Accepted

    def show_run_history(self, rows: list[RunHistorySummary]) -> None:
        if self._history_dialog is not None:
            self._history_dialog.close()
        dialog = RunHistoryDialog(rows, self)
        dialog.export_requested.connect(self._request_history_export)
        dialog.finished.connect(lambda _result: setattr(self, "_history_dialog", None))
        self._history_dialog = dialog
        dialog.open()

    def _request_history_export(self, session_id: str) -> None:
        default_name = f"IPDK_plus_history_{session_id[:8]}.csv"
        destination, _selected_filter = QFileDialog.getSaveFileName(
            self,
            "실행 이력 CSV 내보내기",
            default_name,
            "CSV 파일 (*.csv)",
        )
        if destination:
            self.history_export_requested.emit(session_id, destination)

    def show_history_export_result(self, destination: str, row_count: int) -> None:
        QMessageBox.information(
            self,
            "CSV 내보내기 완료",
            f"작업 {max(0, int(row_count)):,}건을 저장했습니다.\n{destination}",
        )

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
            for button in (
                self.btn_move_folder_top,
                self.btn_move_folder_up,
                self.btn_move_folder_down,
                self.btn_move_folder_bottom,
                self.btn_hold_folders,
                self.btn_release_folders,
            ):
                button.setEnabled(bool(selected_paths))
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
            for button in (
                self.btn_move_folder_top,
                self.btn_move_folder_up,
                self.btn_move_folder_down,
                self.btn_move_folder_bottom,
                self.btn_hold_folders,
                self.btn_release_folders,
            ):
                button.setEnabled(False)
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

    def _emit_folder_move(self, operation: str) -> None:
        paths = self._selected_folder_paths_from_table(
            self.active_folder_table,
            self.active_folder_table_model,
        )
        if paths:
            self.move_folders_requested.emit(paths, operation)

    def _emit_folder_hold(self, held: bool) -> None:
        paths = self._selected_folder_paths_from_table(
            self.active_folder_table,
            self.active_folder_table_model,
        )
        if paths:
            self.hold_folders_requested.emit(paths, held)

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

        selected_keys = self._selected_folder_paths_from_table(table, model)
        if not selected_keys:
            return

        menu = QMenu(table)
        copy_action = menu.addAction("경로 복사")
        chosen = menu.exec(table.viewport().mapToGlobal(position))
        if chosen is copy_action:
            source_paths = [
                model.source_folder_at(index.row())
                for index in table.selectionModel().selectedRows()
            ]
            self._copy_folder_paths_to_clipboard(
                list(dict.fromkeys(path for path in source_paths if path))
            )

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
        """Navigate explicit paths or search favorite roots from one input."""

        query = self.path_jump_edit.text().strip()
        normalized_path = self._normalize_navigation_path(query)
        is_explicit_path = (
            os.path.isdir(normalized_path)
            or os.path.isfile(normalized_path)
            or self._looks_like_navigation_path(query)
        )
        if is_explicit_path:
            self.jump_to_path(query, show_feedback=True)
            return
        self._show_cached_folder_search()
        self._run_live_folder_search()

    @staticmethod
    def _looks_like_navigation_path(value: str) -> bool:
        """Return whether user input has explicit filesystem-path syntax."""

        stripped = value.strip()
        return bool(
            stripped.startswith(("\\\\", "/", "~", "."))
            or (len(stripped) >= 2 and stripped[1] == ":")
            or "\\" in stripped
            or "/" in stripped
        )

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
        """Keep tree alignment without overwriting the unified user input."""

        if self._is_syncing_navigation or not current.isValid():
            return

        self._center_tree_index_horizontally(current)

    def _on_directory_loaded(self, _path: str) -> None:
        """Retry pending jump after filesystem model loads directories."""

        if not self._pending_jump_target:
            return
        QTimer.singleShot(0, self._retry_pending_jump)

    def _populate_recipe_selector(self) -> None:
        """Populate the always-multi-select Recipe menu from configuration."""

        previous_paths = {
            str(action.data() or "").strip()
            for action in self._recipe_actions
            if action.isChecked() and str(action.data() or "").strip()
        }
        had_actions = bool(self._recipe_actions)
        recipe_menu = self.recipe_multi_button.menu()
        assert recipe_menu is not None
        recipe_menu.clear()
        self._recipe_actions = []

        seen_recipe_paths: set[str] = set()
        for recipe_item in self._config.recipe_config.recipes:
            recipe_path = str(recipe_item.path or "").strip()
            if not recipe_path or recipe_path in seen_recipe_paths:
                continue
            seen_recipe_paths.add(recipe_path)
            action = QAction(recipe_item.alias, recipe_menu)
            action.setCheckable(True)
            action.setData(recipe_path)
            action.setToolTip(recipe_path)
            action.triggered.connect(self._on_multi_recipe_selection_changed)
            recipe_menu.addAction(action)
            self._recipe_actions.append(action)

        default_alias = (self._config.recipe_config.default_alias or "").strip().lower()
        for action in self._recipe_actions:
            path = str(action.data() or "").strip()
            alias = action.text().strip().lower()
            action.setChecked(path in previous_paths if had_actions else alias == default_alias)

        if not had_actions and self._recipe_actions and not any(
            action.isChecked() for action in self._recipe_actions
        ):
            self._recipe_actions[0].setChecked(True)
        self._update_recipe_selection_display()

    def _on_multi_recipe_selection_changed(self, _checked: bool = False) -> None:
        """Refresh the recipe count and path preview after a check action."""

        self._update_recipe_selection_display()

    def _update_recipe_selection_display(self) -> None:
        """Show selected recipe count and paths without changing selection state."""

        selections = self.current_recipe_selections()
        if not selections:
            button_text = "Recipe 선택"
        elif len(selections) == 1:
            button_text = self.recipe_multi_button.fontMetrics().elidedText(
                selections[0][0],
                Qt.ElideRight,
                210,
            )
        else:
            button_text = f"Recipe {len(selections)}개 선택"
        self.recipe_multi_button.setText(button_text)
        tooltip = "\n".join(f"{alias}: {path}" for alias, path in selections)
        self.recipe_multi_button.setToolTip(tooltip or "선택된 Recipe가 없습니다.")

        for row in self.recipe_path_rows:
            self.recipe_paths_layout.removeWidget(row)
            row.deleteLater()
        self.recipe_path_rows = []

        self.recipe_path_empty_label.setVisible(not selections)
        for alias, path in selections:
            row = RecipePathRow(alias, path, self.recipe_paths_panel)
            row.layout_height_changed.connect(self._update_recipe_paths_panel_height)
            self.recipe_paths_layout.addWidget(row)
            self.recipe_path_rows.append(row)
        margins = self.recipe_paths_layout.contentsMargins()
        available_width = max(
            0,
            self.recipe_paths_scroll.viewport().width() - margins.left() - margins.right(),
        )
        for row in self.recipe_path_rows:
            row.set_available_width(available_width)
        self._update_recipe_paths_panel_height()

    def _update_recipe_paths_panel_height(self) -> None:
        """Keep responsive Recipe rows separated and scroll only when the list grows."""

        margins = self.recipe_paths_layout.contentsMargins()
        if self.recipe_path_rows:
            rows_height = sum(row.minimumHeight() for row in self.recipe_path_rows)
            rows_height += self.recipe_paths_layout.spacing() * (len(self.recipe_path_rows) - 1)
        else:
            rows_height = self.recipe_path_empty_label.sizeHint().height()
        content_height = max(48, margins.top() + rows_height + margins.bottom() + 2)
        self.recipe_paths_panel.setMinimumHeight(content_height)
        self.recipe_paths_scroll.setFixedHeight(min(content_height, 185))
        self.recipe_paths_layout.activate()
        self.recipe_paths_panel.updateGeometry()

    def _open_update_link(self) -> None:
        """Open the configured latest release URL in the default browser."""

        url = str(self._config.update.latest_release_url or "").strip()
        if not url:
            QMessageBox.warning(self, "업데이트 확인", "업데이트 링크가 설정되어 있지 않습니다.")
            return
        if not QDesktopServices.openUrl(QUrl(url)):
            QMessageBox.warning(self, "업데이트 확인", f"업데이트 링크를 열 수 없습니다:\n{url}")

    def _open_help_dialog(self) -> None:
        """Open or focus the searchable in-app help dialog."""

        if self._help_dialog is None:
            self._help_dialog = HelpDialog(self)
            self._help_dialog.finished.connect(self._clear_help_dialog_reference)
        self._help_dialog.show()
        self._help_dialog.raise_()
        self._help_dialog.activateWindow()

    def _clear_help_dialog_reference(self, *_args: Any) -> None:
        self._help_dialog = None

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
            self._center_tree_index_horizontally(model_index)
            self.folder_tree.setFocus(Qt.OtherFocusReason)
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

        self._center_tree_index_horizontally(self.folder_tree.currentIndex())

        if self._pending_jump_show_feedback:
            self.append_log(f"[탐색] 경로 이동 완료: {target_path}")
        self._clear_pending_jump()

    def _center_tree_index_horizontally(self, index: QModelIndex) -> None:
        """Center a tree item's hierarchy/text anchor in the horizontal viewport."""

        if not index.isValid():
            return
        self._center_tree_horizontal_anchor(self._tree_index_horizontal_anchor(index))

    def _center_tree_horizontal_anchor(self, anchor: int) -> None:
        """Center one tree content-space anchor within the available scroll range."""

        scrollbar = self.folder_tree.horizontalScrollBar()
        viewport = self.folder_tree.viewport()
        if scrollbar is None or viewport.width() <= 0:
            return

        scrollbar.setValue(
            self._calculate_centered_tree_scroll_value(
                anchor=anchor,
                viewport_width=viewport.width(),
                minimum=scrollbar.minimum(),
                maximum=scrollbar.maximum(),
            )
        )

    def _tree_index_horizontal_anchor(self, index: QModelIndex) -> int:
        """Return the content-space center of an item's branch icon and label."""

        depth = 0
        parent = index.parent()
        while parent.isValid():
            depth += 1
            parent = parent.parent()

        label = str(index.data(Qt.DisplayRole) or "")
        label_width = self.folder_tree.fontMetrics().horizontalAdvance(label)
        return self._calculate_tree_horizontal_anchor(
            depth=depth,
            indentation=self.folder_tree.indentation(),
            label_width=label_width,
        )

    @staticmethod
    def _calculate_tree_horizontal_anchor(depth: int, indentation: int, label_width: int) -> int:
        """Calculate the branch/icon/label center in tree content coordinates."""

        branch_and_icon_width = 28
        return (depth + 1) * indentation + branch_and_icon_width + label_width // 2

    @staticmethod
    def _calculate_centered_tree_scroll_value(
        anchor: int,
        viewport_width: int,
        minimum: int,
        maximum: int,
    ) -> int:
        """Clamp the scroll value that places a content anchor at viewport center."""

        target_value = anchor - max(0, viewport_width) // 2
        return max(minimum, min(maximum, target_value))

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
