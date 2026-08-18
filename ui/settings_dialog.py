"""Dialogs for editing the runtime app and recipe YAML settings."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from config.config_loader import ConfigLoader
from config.settings_editor import (
    SettingsEditorError,
    update_app_config,
    update_recipe_config,
)


class AppConfigSettingsDialog(QDialog):
    """Edit the supported RabbitMQ and publish settings."""

    settings_saved = Signal(str)

    def __init__(self, config_path: str | Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._config_path = Path(config_path)
        config = ConfigLoader.load(self._config_path)
        self.setObjectName("appConfigSettingsDialog")
        self.setWindowTitle("MQ 연결 설정")
        self.resize(760, 720)

        layout = QVBoxLayout(self)
        path_label = QLabel(f"적용 파일: {self._config_path}", self)
        path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        path_label.setWordWrap(True)
        layout.addWidget(path_label)

        self.settings_tabs = QTabWidget(self)
        self.settings_tabs.setObjectName("settingsTabs")
        self.settings_tabs.addTab(self._build_rabbitmq_tab(config.rabbitmq), "RabbitMQ")
        self.settings_tabs.addTab(self._build_publish_tab(config.publish), "전송/조회")
        layout.addWidget(self.settings_tabs, stretch=1)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel, parent=self)
        buttons.button(QDialogButtonBox.Save).setText("저장")
        buttons.button(QDialogButtonBox.Cancel).setText("취소")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _build_rabbitmq_tab(self, rabbitmq) -> QWidget:  # noqa: ANN001
        tab = QWidget(self)
        tab.setObjectName("settingsTabPage")
        form = QFormLayout(tab)
        self.host_edit = QLineEdit(rabbitmq.host, tab)
        self.port_spin = _integer_spin(rabbitmq.port, 1, 65535, tab)
        self.username_edit = QLineEdit(rabbitmq.username, tab)
        self.password_edit = QLineEdit(rabbitmq.password, tab)
        self.password_edit.setEchoMode(QLineEdit.Password)
        password_row = QWidget(tab)
        password_layout = QHBoxLayout(password_row)
        password_layout.setContentsMargins(0, 0, 0, 0)
        password_layout.addWidget(self.password_edit, stretch=1)
        show_password = QCheckBox("표시", password_row)
        show_password.toggled.connect(
            lambda checked: self.password_edit.setEchoMode(
                QLineEdit.Normal if checked else QLineEdit.Password
            )
        )
        password_layout.addWidget(show_password)
        self.virtual_host_edit = QLineEdit(rabbitmq.virtual_host, tab)
        self.request_exchange_edit = QLineEdit(rabbitmq.request_exchange, tab)
        self.request_routing_key_edit = QLineEdit(rabbitmq.request_routing_key, tab)
        self.request_queue_edit = QLineEdit(rabbitmq.request_queue, tab)
        self.result_queue_base_edit = QLineEdit(rabbitmq.result_queue_base, tab)
        form.addRow("host", self.host_edit)
        form.addRow("port", self.port_spin)
        form.addRow("username", self.username_edit)
        form.addRow("password", password_row)
        form.addRow("virtual_host", self.virtual_host_edit)
        form.addRow("request_exchange", self.request_exchange_edit)
        form.addRow("request_routing_key", self.request_routing_key_edit)
        form.addRow("request_queue", self.request_queue_edit)
        form.addRow("result_queue_base", self.result_queue_base_edit)
        return tab

    def _build_publish_tab(self, publish) -> QWidget:  # noqa: ANN001
        tab = QWidget(self)
        tab.setObjectName("settingsTabPage")
        form = QFormLayout(tab)
        self.default_action_edit = QLineEdit(publish.default_action, tab)
        self.default_priority_spin = _integer_spin(publish.default_priority, 0, 255, tab)
        self.polling_interval_spin = _integer_spin(publish.polling_interval_seconds, 1, 86400, tab)
        self.timeout_spin = _integer_spin(publish.timeout_seconds, 1, 2_147_483_647, tab)
        self.max_messages_spin = _integer_spin(publish.max_messages_per_poll, 1, 1_000_000, tab)
        self.max_retries_spin = _integer_spin(publish.max_publish_retries, 1, 100, tab)
        self.retry_backoff_spin = QDoubleSpinBox(tab)
        self.retry_backoff_spin.setRange(0.1, 3600.0)
        self.retry_backoff_spin.setDecimals(1)
        self.retry_backoff_spin.setSingleStep(0.1)
        self.retry_backoff_spin.setValue(float(publish.publish_retry_backoff_seconds))
        self.initial_open_spin = _integer_spin(publish.initial_open_folders, 1, 1000, tab)
        self.max_active_spin = _integer_spin(publish.max_active_open_folders, 1, 1000, tab)
        form.addRow("default_action", self.default_action_edit)
        form.addRow("default_priority", self.default_priority_spin)
        form.addRow("polling_interval_seconds", self.polling_interval_spin)
        form.addRow("timeout_seconds", self.timeout_spin)
        form.addRow("max_messages_per_poll", self.max_messages_spin)
        form.addRow("max_publish_retries", self.max_retries_spin)
        form.addRow("publish_retry_backoff_seconds", self.retry_backoff_spin)
        form.addRow("initial_open_folders", self.initial_open_spin)
        form.addRow("max_active_open_folders", self.max_active_spin)
        return tab

    def _save(self) -> None:
        rabbitmq_values = {
            "host": self.host_edit.text().strip(),
            "port": self.port_spin.value(),
            "username": self.username_edit.text().strip(),
            "password": self.password_edit.text(),
            "virtual_host": self.virtual_host_edit.text().strip(),
            "request_exchange": self.request_exchange_edit.text().strip(),
            "request_routing_key": self.request_routing_key_edit.text().strip(),
            "request_queue": self.request_queue_edit.text().strip(),
            "result_queue_base": self.result_queue_base_edit.text().strip(),
        }
        publish_values = {
            "default_action": self.default_action_edit.text().strip(),
            "default_priority": self.default_priority_spin.value(),
            "polling_interval_seconds": self.polling_interval_spin.value(),
            "timeout_seconds": self.timeout_spin.value(),
            "max_messages_per_poll": self.max_messages_spin.value(),
            "max_publish_retries": self.max_retries_spin.value(),
            "publish_retry_backoff_seconds": self.retry_backoff_spin.value(),
            "initial_open_folders": self.initial_open_spin.value(),
            "max_active_open_folders": self.max_active_spin.value(),
        }
        try:
            update_app_config(self._config_path, rabbitmq_values, publish_values)
        except SettingsEditorError as exc:
            QMessageBox.critical(self, "설정 저장 실패", str(exc))
            return
        self.settings_saved.emit(str(self._config_path))
        QMessageBox.information(
            self,
            "설정 저장 완료",
            f"실제 런타임 설정 파일에 저장했습니다.\n\n{self._config_path}\n\n프로그램을 다시 시작하면 적용됩니다.",
        )
        self.accept()


class RecipeConfigSettingsDialog(QDialog):
    """Edit recipe aliases and paths in the active recipe YAML file."""

    settings_saved = Signal(str)

    def __init__(
        self,
        app_config_path: str | Path,
        recipe_config_path: str | Path,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._app_config_path = Path(app_config_path)
        self._recipe_config_path = Path(recipe_config_path)
        config = ConfigLoader.load(self._app_config_path)
        self.setObjectName("recipeConfigSettingsDialog")
        self.setWindowTitle("Recipe 설정")
        self.resize(900, 600)

        layout = QVBoxLayout(self)
        path_label = QLabel(f"적용 파일: {self._recipe_config_path}", self)
        path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        path_label.setWordWrap(True)
        layout.addWidget(path_label)
        default_row = QFormLayout()
        self.default_alias_edit = QLineEdit(config.recipe_config.default_alias or "", self)
        default_row.addRow("기본 alias", self.default_alias_edit)
        layout.addLayout(default_row)

        self.recipe_table = QTableWidget(0, 2, self)
        self.recipe_table.setObjectName("recipeSettingsTable")
        self.recipe_table.setHorizontalHeaderLabels(["Alias", "Recipe 경로"])
        self.recipe_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.recipe_table.setSelectionMode(QTableWidget.ExtendedSelection)
        self.recipe_table.verticalHeader().setVisible(False)
        self.recipe_table.verticalHeader().setDefaultSectionSize(38)
        self.recipe_table.verticalHeader().setMinimumSectionSize(34)
        self.recipe_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.recipe_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        for recipe in config.recipe_config.recipes:
            self._append_recipe(recipe.alias, recipe.path)
        layout.addWidget(self.recipe_table, stretch=1)

        edit_row = QHBoxLayout()
        add_button = QPushButton("Recipe 추가", self)
        remove_button = QPushButton("선택 Recipe 삭제", self)
        add_button.clicked.connect(self._add_recipe)
        remove_button.clicked.connect(self._remove_selected_recipes)
        edit_row.addWidget(add_button)
        edit_row.addWidget(remove_button)
        edit_row.addStretch(1)
        layout.addLayout(edit_row)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel, parent=self)
        buttons.button(QDialogButtonBox.Save).setText("저장")
        buttons.button(QDialogButtonBox.Cancel).setText("취소")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _append_recipe(self, alias: str, path: str) -> None:
        row = self.recipe_table.rowCount()
        self.recipe_table.insertRow(row)
        self.recipe_table.setItem(row, 0, QTableWidgetItem(alias))
        self.recipe_table.setItem(row, 1, QTableWidgetItem(path))
        self.recipe_table.setCurrentCell(row, 0)

    def _add_recipe(self) -> None:
        self._append_recipe("", "")
        self.recipe_table.editItem(
            self.recipe_table.item(self.recipe_table.currentRow(), 0)
        )

    def _remove_selected_recipes(self) -> None:
        rows = {index.row() for index in self.recipe_table.selectionModel().selectedRows()}
        if not rows and self.recipe_table.currentRow() >= 0:
            rows.add(self.recipe_table.currentRow())
        for row in sorted(rows, reverse=True):
            self.recipe_table.removeRow(row)

    def _save(self) -> None:
        recipes = [
            (
                self.recipe_table.item(row, 0).text() if self.recipe_table.item(row, 0) else "",
                self.recipe_table.item(row, 1).text() if self.recipe_table.item(row, 1) else "",
            )
            for row in range(self.recipe_table.rowCount())
        ]
        try:
            update_recipe_config(
                self._app_config_path,
                self._recipe_config_path,
                self.default_alias_edit.text(),
                recipes,
            )
        except SettingsEditorError as exc:
            QMessageBox.critical(self, "Recipe 설정 저장 실패", str(exc))
            return
        self.settings_saved.emit(str(self._recipe_config_path))
        QMessageBox.information(
            self,
            "Recipe 설정 저장 완료",
            f"실제 런타임 Recipe 설정 파일에 저장했습니다.\n\n{self._recipe_config_path}\n\n프로그램을 다시 시작하면 적용됩니다.",
        )
        self.accept()


def _integer_spin(value: int, minimum: int, maximum: int, parent: QWidget) -> QSpinBox:
    widget = QSpinBox(parent)
    widget.setRange(minimum, maximum)
    widget.setValue(int(value))
    return widget
