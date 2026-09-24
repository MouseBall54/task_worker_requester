"""Live RabbitMQ consumer status and worker-node registry dialog."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QThread
from PySide6.QtGui import QColor, QCloseEvent
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
    QHeaderView,
)

from config.models import RabbitMQConfig
from config.worker_node_settings import (
    WorkerNode,
    WorkerNodeSettingsError,
    default_management_api_url,
    load_worker_node_settings,
    save_worker_node_settings,
)
from services.workers.consumer_monitor_worker import ConsumerMonitorWorker
from utils.time_utils import now_seoul


class WorkerStatusDialog(QDialog):
    """Show registered worker IPs and edit their names while monitoring."""

    def __init__(
        self,
        settings_path: str | Path,
        rabbitmq: RabbitMQConfig,
        request_queue: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings_path = Path(settings_path)
        self._rabbitmq = rabbitmq
        self._request_queue = request_queue
        self._last_snapshot: dict[str, object] = {}
        settings = load_worker_node_settings(self._settings_path)

        self.setObjectName("workerStatusDialog")
        self.setWindowTitle("Worker Consumer 현황")
        self.resize(780, 540)

        layout = QVBoxLayout(self)
        header = QLabel(
            f"요청 Queue: {request_queue}    ·    VHost: {rabbitmq.virtual_host}", self
        )
        header.setObjectName("workerStatusHeader")
        layout.addWidget(header)

        self._default_management_url = default_management_api_url(rabbitmq.host)
        self.management_url_edit = QLineEdit(
            settings.management_api_url or self._default_management_url, self
        )
        self.management_url_edit.setPlaceholderText("http://RabbitMQ 서버:15672")
        layout.addWidget(QLabel("RabbitMQ Management API 주소", self))
        layout.addWidget(self.management_url_edit)

        self.node_table = QTableWidget(0, 4, self)
        self.node_table.setObjectName("workerNodeTable")
        self.node_table.setHorizontalHeaderLabels(["명칭", "IP 주소", "Consumer 수", "상태"])
        self.node_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.node_table.setSelectionMode(QTableWidget.ExtendedSelection)
        self.node_table.setAlternatingRowColors(True)
        self.node_table.verticalHeader().setVisible(False)
        self.node_table.verticalHeader().setDefaultSectionSize(38)
        header = self.node_table.horizontalHeader()
        header.setMinimumSectionSize(72)
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.Interactive)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.node_table.setColumnWidth(1, 170)
        for node in settings.nodes:
            self._append_row(node.name, node.address)
        layout.addWidget(self.node_table, stretch=1)

        edit_row = QHBoxLayout()
        add_button = QPushButton("장비 추가", self)
        remove_button = QPushButton("선택 삭제", self)
        self.refresh_button = QPushButton("지금 새로고침", self)
        self.save_button = QPushButton("설정 저장", self)
        add_button.clicked.connect(self._add_row)
        remove_button.clicked.connect(self._remove_rows)
        self.refresh_button.clicked.connect(self._request_refresh)
        self.save_button.clicked.connect(self._save_settings)
        edit_row.addWidget(add_button)
        edit_row.addWidget(remove_button)
        edit_row.addStretch(1)
        edit_row.addWidget(self.refresh_button)
        edit_row.addWidget(self.save_button)
        layout.addLayout(edit_row)

        footer = QHBoxLayout()
        self.status_label = QLabel("RabbitMQ 현황 조회 중...", self)
        self.status_label.setObjectName("workerStatusFooter")
        footer.addWidget(self.status_label, stretch=1)
        close_button = QPushButton("닫기", self)
        close_button.clicked.connect(self.close)
        footer.addWidget(close_button)
        layout.addLayout(footer)

        self._thread = QThread(self)
        self._worker = ConsumerMonitorWorker(
            str(self._settings_path), rabbitmq, request_queue, interval_seconds=5
        )
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.snapshot_ready.connect(self._apply_snapshot)
        self._worker.finished.connect(self._thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.start()

    def _append_row(self, name: str = "", address: str = "") -> None:
        row = self.node_table.rowCount()
        self.node_table.insertRow(row)
        self.node_table.setItem(row, 0, QTableWidgetItem(name))
        self.node_table.setItem(row, 1, QTableWidgetItem(address))
        self._set_status_cells(row, "확인 중", "-")

    def _add_row(self) -> None:
        self._append_row()
        item = self.node_table.item(self.node_table.rowCount() - 1, 0)
        self.node_table.setCurrentItem(item)
        self.node_table.scrollToItem(item)
        self.node_table.editItem(item)

    def _remove_rows(self) -> None:
        rows = sorted({index.row() for index in self.node_table.selectionModel().selectedRows()}, reverse=True)
        for row in rows:
            self.node_table.removeRow(row)

    def _request_refresh(self) -> None:
        self.status_label.setText("현황을 새로 조회하고 있습니다...")
        self._worker.request_refresh()

    def _save_settings(self) -> None:
        nodes: list[WorkerNode] = []
        for row in range(self.node_table.rowCount()):
            name_item = self.node_table.item(row, 0)
            address_item = self.node_table.item(row, 1)
            if name_item is not None and name_item.data(Qt.UserRole) == "unregistered":
                continue
            name = name_item.text().strip() if name_item is not None else ""
            address = address_item.text().strip() if address_item is not None else ""
            if not name and not address:
                continue
            nodes.append(WorkerNode(address, name))
        try:
            management_api_url = self.management_url_edit.text().strip().rstrip("/")
            if management_api_url.lower() == self._default_management_url.lower():
                management_api_url = ""
            settings = save_worker_node_settings(
                self._settings_path,
                management_api_url,
                nodes,
            )
        except WorkerNodeSettingsError as exc:
            QMessageBox.warning(self, "Worker 설정 확인", str(exc))
            return
        self.node_table.setRowCount(0)
        for node in settings.nodes:
            self._append_row(node.name, node.address)
        self.status_label.setText(f"설정 저장 완료 · {self._settings_path}")
        self._render_snapshot(self._last_snapshot)

    def _apply_snapshot(self, snapshot: object) -> None:
        if not isinstance(snapshot, dict):
            return
        self._last_snapshot = snapshot
        self._render_snapshot(snapshot)

    def _render_snapshot(self, snapshot: dict[str, object]) -> None:
        error = str(snapshot.get("error", ""))
        counts = snapshot.get("counts", {})
        unregistered = snapshot.get("unregistered", {})
        if not isinstance(counts, dict) or not isinstance(unregistered, dict):
            return

        if error:
            for row in range(self.node_table.rowCount()):
                self._set_status_cells(row, "조회 오류", "-")
            self.status_label.setText(f"조회 실패 · {error}")
            return

        for row in range(self.node_table.rowCount() - 1, -1, -1):
            name_item = self.node_table.item(row, 0)
            if name_item is not None and name_item.text() == "미등록 장비":
                self.node_table.removeRow(row)

        configured_addresses: set[str] = set()
        for row in range(self.node_table.rowCount()):
            address_item = self.node_table.item(row, 1)
            address = address_item.text().strip() if address_item is not None else ""
            if not address:
                self._set_status_cells(row, "IP 입력 필요", "-")
                continue
            configured_addresses.add(address)
            count = int(counts.get(address, 0))
            self._set_status_cells(row, "온라인" if count else "오프라인", str(count))

        for address, raw_count in unregistered.items():
            normalized_address = str(address)
            if normalized_address in configured_addresses:
                continue
            row = self._find_unregistered_row(normalized_address)
            if row is None:
                row = self.node_table.rowCount()
                self.node_table.insertRow(row)
                name_item = QTableWidgetItem("미등록 장비")
                name_item.setFlags(name_item.flags() & ~Qt.ItemIsEditable)
                name_item.setData(Qt.UserRole, "unregistered")
                address_item = QTableWidgetItem(normalized_address)
                address_item.setFlags(address_item.flags() & ~Qt.ItemIsEditable)
                self.node_table.setItem(row, 0, name_item)
                self.node_table.setItem(row, 1, address_item)
            self._set_status_cells(row, "온라인 · 미등록", str(raw_count))

        self.status_label.setText(
            f"마지막 갱신 {now_seoul().strftime('%H:%M:%S')} · 5초 간격"
        )

    def _find_unregistered_row(self, address: str) -> int | None:
        for row in range(self.node_table.rowCount()):
            name_item = self.node_table.item(row, 0)
            address_item = self.node_table.item(row, 1)
            if (
                name_item is not None
                and name_item.text() == "미등록 장비"
                and address_item is not None
                and address_item.text() == address
            ):
                return row
        return None

    def _set_status_cells(self, row: int, status: str, count: str) -> None:
        count_item = QTableWidgetItem(count)
        count_item.setTextAlignment(Qt.AlignCenter)
        count_item.setFlags(count_item.flags() & ~Qt.ItemIsEditable)
        status_item = QTableWidgetItem(status)
        status_item.setTextAlignment(Qt.AlignCenter)
        status_item.setFlags(status_item.flags() & ~Qt.ItemIsEditable)
        if status == "온라인" or status.startswith("온라인 ·"):
            status_item.setForeground(QColor("#4ade80"))
        elif status == "오프라인":
            status_item.setForeground(QColor("#f87171"))
        elif status == "조회 오류":
            status_item.setForeground(QColor("#fbbf24"))
        self.node_table.setItem(row, 2, count_item)
        self.node_table.setItem(row, 3, status_item)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if self._thread.isRunning():
            self._worker.stop()
            self._thread.quit()
            self._thread.wait(7000)
        super().closeEvent(event)
