"""Searchable help dialog for IPDK_plus."""

from __future__ import annotations

import html

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ui.help_content import HELP_TOPICS, HelpTopic
from ui.help_search import (
    HelpSearchMatch,
    body_match_spans,
    find_help_matches,
    first_match_index_for_topic,
)


class HelpDialog(QDialog):
    """In-app help with topic navigation and find-in-page style search."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("helpDialog")
        self.setWindowTitle("IPDK_plus 도움말")
        self.resize(1040, 720)

        self._matches: list[HelpSearchMatch] = []
        self._current_match_index = -1
        self._syncing_topic_selection = False

        root_layout = QHBoxLayout(self)
        root_layout.setContentsMargins(14, 14, 14, 14)
        root_layout.setSpacing(12)

        self.topic_list = QListWidget(self)
        self.topic_list.setObjectName("helpTopicList")
        self.topic_list.setMinimumWidth(260)
        self.topic_list.setMaximumWidth(320)
        for index, topic in enumerate(HELP_TOPICS):
            item = QListWidgetItem(topic.title)
            item.setData(Qt.UserRole, index)
            self.topic_list.addItem(item)
        self.topic_list.currentItemChanged.connect(self._on_topic_changed)
        root_layout.addWidget(self.topic_list)

        right_panel = QWidget(self)
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(10)

        search_row = QHBoxLayout()
        search_row.setSpacing(8)
        self.search_edit = QLineEdit(self)
        self.search_edit.setObjectName("helpSearchEdit")
        self.search_edit.setPlaceholderText("도움말 검색")
        self.search_edit.textChanged.connect(self._on_search_changed)
        self.search_edit.installEventFilter(self)
        search_row.addWidget(self.search_edit, stretch=1)

        self.prev_button = QPushButton("이전", self)
        self.prev_button.clicked.connect(self._go_previous_match)
        search_row.addWidget(self.prev_button)

        self.next_button = QPushButton("다음", self)
        self.next_button.clicked.connect(self._go_next_match)
        search_row.addWidget(self.next_button)

        self.match_count_label = QLabel("검색어 입력", self)
        self.match_count_label.setObjectName("helpMatchLabel")
        self.match_count_label.setMinimumWidth(92)
        self.match_count_label.setAlignment(Qt.AlignCenter)
        search_row.addWidget(self.match_count_label)
        right_layout.addLayout(search_row)

        self.body_view = QTextBrowser(self)
        self.body_view.setObjectName("helpBodyView")
        self.body_view.setOpenExternalLinks(True)
        right_layout.addWidget(self.body_view, stretch=1)

        root_layout.addWidget(right_panel, stretch=1)

        self.topic_list.setCurrentRow(0)
        self._update_match_controls()
        self.search_edit.setFocus()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        """Support Enter/Shift+Enter navigation in the search field."""

        if watched is self.search_edit and event.type() == QEvent.KeyPress:
            key = event.key()  # type: ignore[attr-defined]
            if key in (Qt.Key_Return, Qt.Key_Enter):
                if event.modifiers() & Qt.ShiftModifier:  # type: ignore[attr-defined]
                    self._go_previous_match()
                else:
                    self._go_next_match()
                return True
        return super().eventFilter(watched, event)

    def _on_search_changed(self, query: str) -> None:
        self._matches = find_help_matches(HELP_TOPICS, query)
        self._current_match_index = 0 if self._matches else -1
        if self._matches:
            self._select_topic(self._matches[0].topic_index)
        self._render_current_topic()
        self._update_match_controls()

    def _on_topic_changed(self, current: QListWidgetItem | None, _previous: QListWidgetItem | None) -> None:
        if current is None or self._syncing_topic_selection:
            return
        topic_index = int(current.data(Qt.UserRole))
        if self._matches:
            first_match_index = first_match_index_for_topic(self._matches, topic_index)
            if first_match_index is not None:
                self._current_match_index = first_match_index
        self._render_topic(topic_index)
        self._update_match_controls()

    def _go_next_match(self) -> None:
        if not self._matches:
            return
        self._current_match_index = (self._current_match_index + 1) % len(self._matches)
        self._select_topic(self._matches[self._current_match_index].topic_index)
        self._render_current_topic()
        self._update_match_controls()

    def _go_previous_match(self) -> None:
        if not self._matches:
            return
        self._current_match_index = (self._current_match_index - 1) % len(self._matches)
        self._select_topic(self._matches[self._current_match_index].topic_index)
        self._render_current_topic()
        self._update_match_controls()

    def _select_topic(self, topic_index: int) -> None:
        if self.topic_list.currentRow() == topic_index:
            return
        self._syncing_topic_selection = True
        try:
            self.topic_list.setCurrentRow(topic_index)
        finally:
            self._syncing_topic_selection = False

    def _render_current_topic(self) -> None:
        current_item = self.topic_list.currentItem()
        topic_index = int(current_item.data(Qt.UserRole)) if current_item is not None else 0
        if self._matches and 0 <= self._current_match_index < len(self._matches):
            topic_index = self._matches[self._current_match_index].topic_index
        self._render_topic(topic_index)

    def _render_topic(self, topic_index: int) -> None:
        active_match = None
        if self._matches and 0 <= self._current_match_index < len(self._matches):
            candidate = self._matches[self._current_match_index]
            if candidate.topic_index == topic_index:
                active_match = candidate

        query = self.search_edit.text()
        self.body_view.setHtml(_topic_to_html(HELP_TOPICS[topic_index], query, active_match))
        if active_match is not None:
            self.body_view.scrollToAnchor("active-help-match")
        else:
            self.body_view.verticalScrollBar().setValue(0)

    def _update_match_controls(self) -> None:
        query = self.search_edit.text().strip()
        has_matches = bool(self._matches)
        self.prev_button.setEnabled(has_matches)
        self.next_button.setEnabled(has_matches)
        if not query:
            self.match_count_label.setText("검색어 입력")
        elif not has_matches:
            self.match_count_label.setText("검색 결과 없음")
        else:
            self.match_count_label.setText(f"{self._current_match_index + 1} / {len(self._matches)}")


def _topic_to_html(topic: HelpTopic, query: str, active_match: HelpSearchMatch | None) -> str:
    active_title = _active_span_for_field(active_match, "title")
    highlighted_title = _highlight_text(topic.title, query, active_title)

    keyword_parts = []
    for index, keyword in enumerate(topic.keywords):
        active_keyword = _active_span_for_field(active_match, f"keyword:{index}")
        keyword_parts.append(_highlight_text(keyword, query, active_keyword))
    keywords_html = ", ".join(keyword_parts)

    active_body = _active_span_for_field(active_match, "body")
    body_html = _highlight_body(topic.body, query, active_body)

    return f"""
    <html>
    <head>
      <style>
        body {{
            color: #edf4fb;
            background: #172033;
            font-family: "Segoe UI Variable Text", "Segoe UI", "Malgun Gothic", "Noto Sans KR";
            font-size: 13px;
            line-height: 1.58;
        }}
        h1 {{
            color: #7dd3fc;
            font-size: 22px;
            margin: 0 0 8px 0;
        }}
        .keywords {{
            color: #b7c5d8;
            border-bottom: 1px solid #34445f;
            margin-bottom: 16px;
            padding-bottom: 10px;
        }}
        .body {{
            white-space: normal;
        }}
        .help-match {{
            color: #0f172a;
            background-color: #fde68a;
            border-radius: 3px;
            padding: 0 2px;
        }}
        .active-help-match {{
            color: #0f172a;
            background-color: #f97316;
            border-radius: 3px;
            padding: 0 2px;
        }}
      </style>
    </head>
    <body>
      <h1>{highlighted_title}</h1>
      <div class="keywords">검색어: {keywords_html}</div>
      <div class="body">{body_html}</div>
    </body>
    </html>
    """


def _active_span_for_field(active_match: HelpSearchMatch | None, field: str) -> tuple[int, int] | None:
    if active_match is None or active_match.field != field:
        return None
    return (active_match.start, active_match.end)


def _highlight_body(body: str, query: str, active_span: tuple[int, int] | None) -> str:
    spans = body_match_spans(body, query)
    return _highlight_spans(body, spans, active_span)


def _highlight_text(text: str, query: str, active_span: tuple[int, int] | None) -> str:
    spans = body_match_spans(text, query)
    return _highlight_spans(text, spans, active_span)


def _highlight_spans(
    text: str,
    spans: list[tuple[int, int]],
    active_span: tuple[int, int] | None,
) -> str:
    if not spans:
        return _escape_text(text)

    output: list[str] = []
    cursor = 0
    for start, end in spans:
        output.append(_escape_text(text[cursor:start]))
        is_active = active_span == (start, end)
        css_class = "active-help-match" if is_active else "help-match"
        anchor = '<a id="active-help-match"></a>' if is_active else ""
        output.append(f'{anchor}<span class="{css_class}">{_escape_text(text[start:end])}</span>')
        cursor = end
    output.append(_escape_text(text[cursor:]))
    return "".join(output)


def _escape_text(text: str) -> str:
    return html.escape(str(text or "")).replace("\n", "<br>")
