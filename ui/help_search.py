"""Search helpers for the in-app help dialog."""

from __future__ import annotations

from dataclasses import dataclass

from ui.help_content import HelpTopic


@dataclass(frozen=True, slots=True)
class HelpSearchMatch:
    """One query match in a help topic."""

    topic_index: int
    field: str
    start: int
    end: int


def normalize_query(query: str) -> str:
    """Normalize a raw search query for case-insensitive matching."""

    return " ".join(str(query or "").casefold().split())


def find_help_matches(topics: tuple[HelpTopic, ...], query: str) -> list[HelpSearchMatch]:
    """Return title, keyword, and body matches for the given query."""

    normalized = normalize_query(query)
    if not normalized:
        return []

    matches: list[HelpSearchMatch] = []
    for topic_index, topic in enumerate(topics):
        matches.extend(_find_field_matches(topic_index, "title", topic.title, normalized))
        for keyword_index, keyword in enumerate(topic.keywords):
            field = f"keyword:{keyword_index}"
            matches.extend(_find_field_matches(topic_index, field, keyword, normalized))
        matches.extend(_find_field_matches(topic_index, "body", topic.body, normalized))
    return matches


def body_match_spans(body: str, query: str) -> list[tuple[int, int]]:
    """Return non-overlapping body spans for highlight rendering."""

    normalized = normalize_query(query)
    if not normalized:
        return []
    return [(match.start, match.end) for match in _find_field_matches(0, "body", body, normalized)]


def topic_has_match(matches: list[HelpSearchMatch], topic_index: int) -> bool:
    """Return whether a topic has at least one search match."""

    return any(match.topic_index == topic_index for match in matches)


def first_match_index_for_topic(matches: list[HelpSearchMatch], topic_index: int) -> int | None:
    """Return the first flattened match index for a topic."""

    for index, match in enumerate(matches):
        if match.topic_index == topic_index:
            return index
    return None


def _find_field_matches(topic_index: int, field: str, text: str, normalized_query: str) -> list[HelpSearchMatch]:
    lowered = str(text or "").casefold()
    matches: list[HelpSearchMatch] = []
    start = 0
    while True:
        found = lowered.find(normalized_query, start)
        if found < 0:
            break
        end = found + len(normalized_query)
        matches.append(HelpSearchMatch(topic_index=topic_index, field=field, start=found, end=end))
        start = end
    return matches
