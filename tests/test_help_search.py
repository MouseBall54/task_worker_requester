"""Tests for searchable help content."""

from __future__ import annotations

import unittest

from ui.help_content import HELP_TOPICS
from ui.help_search import body_match_spans, find_help_matches, first_match_index_for_topic


class HelpSearchTest(unittest.TestCase):
    """Validate help find-in-page match indexing."""

    def test_search_matches_title_keyword_and_body(self) -> None:
        title_matches = find_help_matches(HELP_TOPICS, "프로그램 개요")
        keyword_matches = find_help_matches(HELP_TOPICS, "RabbitMQ")
        body_matches = find_help_matches(HELP_TOPICS, "자동 다운로드")

        self.assertTrue(any(match.field == "title" for match in title_matches))
        self.assertTrue(any(match.field.startswith("keyword:") for match in keyword_matches))
        self.assertTrue(any(match.field == "body" for match in body_matches))

    def test_body_match_spans_returns_all_occurrences(self) -> None:
        spans = body_match_spans("RabbitMQ request RabbitMQ result", "RabbitMQ")

        self.assertEqual(spans, [(0, 8), (17, 25)])

    def test_first_match_index_for_topic_points_to_flattened_result(self) -> None:
        matches = find_help_matches(HELP_TOPICS, "result queue")
        topic_index = matches[-1].topic_index

        first_index = first_match_index_for_topic(matches, topic_index)

        self.assertIsNotNone(first_index)
        self.assertEqual(matches[first_index].topic_index, topic_index)  # type: ignore[index]

    def test_search_returns_empty_for_missing_keyword(self) -> None:
        self.assertEqual(find_help_matches(HELP_TOPICS, "definitely-not-a-help-keyword"), [])


if __name__ == "__main__":
    unittest.main()
