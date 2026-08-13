"""Tests for Seoul-standard timestamp generation and display."""

from __future__ import annotations

from datetime import datetime, timezone
import unittest

from utils.time_utils import format_seoul_display, format_seoul_iso, now_seoul


class TimeUtilsTest(unittest.TestCase):
    def test_utc_timestamp_is_rendered_in_seoul_to_one_decimal_place(self) -> None:
        value = datetime(2026, 8, 13, 1, 2, 3, 987_654, tzinfo=timezone.utc)

        self.assertEqual(format_seoul_iso(value), "2026-08-13T10:02:03.9+09:00")
        self.assertEqual(format_seoul_display(value), "2026-08-13 10:02:03.9")

    def test_legacy_naive_timestamp_is_treated_as_utc(self) -> None:
        self.assertEqual(
            format_seoul_display("2026-08-13T01:02:03.456789"),
            "2026-08-13 10:02:03.4",
        )

    def test_current_time_uses_seoul_and_tenth_second_precision(self) -> None:
        value = now_seoul()

        self.assertEqual(value.utcoffset().total_seconds(), 9 * 3600)
        self.assertEqual(value.microsecond % 100_000, 0)
        self.assertRegex(format_seoul_iso(value), r"\.\d\+09:00$")


if __name__ == "__main__":
    unittest.main()
