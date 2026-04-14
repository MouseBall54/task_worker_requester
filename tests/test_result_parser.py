"""Unit tests for result parser."""

from __future__ import annotations

import unittest

from services.result_parser import parse_task_result


class ResultParserTest(unittest.TestCase):
    """Validate PASS detection and fallback id extraction."""

    def test_parse_pass_result(self) -> None:
        parsed = parse_task_result(
            {
                "request_id": "abc",
                "result": ["PASS", "foo"],
                "status": "DONE",
                "error": None,
            }
        )
        self.assertEqual(parsed.request_id, "abc")
        self.assertTrue(parsed.is_success)

    def test_parse_with_correlation_id_fallback(self) -> None:
        parsed = parse_task_result(
            {"result": ["FAIL"], "status": "DONE"},
            correlation_id="corr-1",
        )
        self.assertEqual(parsed.request_id, "corr-1")
        self.assertFalse(parsed.is_success)

    def test_parse_without_request_id_raises(self) -> None:
        with self.assertRaises(ValueError):
            parse_task_result({"result": ["PASS"]})


if __name__ == "__main__":
    unittest.main()
