"""Consistent Seoul timestamps with tenth-second precision."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any


SEOUL_TIMEZONE = timezone(timedelta(hours=9), name="Asia/Seoul")


def truncate_to_tenth(value: datetime) -> datetime:
    """Drop precision below one tenth of a second."""

    return value.replace(microsecond=(value.microsecond // 100_000) * 100_000)


def now_seoul() -> datetime:
    """Return the current Seoul time at tenth-second precision."""

    return truncate_to_tenth(datetime.now(SEOUL_TIMEZONE))


def parse_datetime(value: Any) -> datetime | None:
    """Parse an ISO timestamp, treating legacy naive values as UTC."""

    if value is None or not str(value).strip():
        return None
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def to_seoul(value: datetime) -> datetime:
    """Convert a timestamp to Seoul and remove sub-tenth precision."""

    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return truncate_to_tenth(value.astimezone(SEOUL_TIMEZONE))


def format_seoul_iso(value: datetime | str | None = None) -> str:
    """Format an ISO timestamp as ``YYYY-MM-DDTHH:MM:SS.t+09:00``."""

    if value is None:
        resolved = now_seoul()
    elif isinstance(value, datetime):
        resolved = to_seoul(value)
    else:
        parsed = parse_datetime(value)
        if parsed is None:
            return ""
        resolved = to_seoul(parsed)
    return _format(resolved, "T", include_offset=True)


def format_seoul_display(value: datetime | str | None) -> str:
    """Format a timestamp for UI and exported human-readable data."""

    if value is None or value == "":
        return ""
    try:
        parsed = value if isinstance(value, datetime) else parse_datetime(value)
    except ValueError:
        return str(value)
    if parsed is None:
        return ""
    resolved = to_seoul(parsed)
    return _format(resolved, " ", include_offset=False)


def _format(value: datetime, separator: str, *, include_offset: bool) -> str:
    tenth = value.microsecond // 100_000
    text = f"{value.strftime(f'%Y-%m-%d{separator}%H:%M:%S')}.{tenth}"
    if not include_offset:
        return text
    offset = value.strftime("%z")
    return f"{text}{offset[:3]}:{offset[3:]}"
