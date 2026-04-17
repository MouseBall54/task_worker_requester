"""Tests for result queue IPv4 resolution helpers."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from config.models import RabbitMQConfig
from services.broker.result_queue import resolve_local_ipv4, resolve_result_queue_name


class ResultQueueHelperTest(unittest.TestCase):
    """Validate IPv4 and queue-name resolution rules."""

    def setUp(self) -> None:
        self.config = RabbitMQConfig(
            host="127.0.0.1",
            port=5672,
            username="guest",
            password="guest",
            result_queue_base="task.result.client",
        )

    @patch("services.broker.result_queue._resolve_routed_ipv4", return_value="192.168.0.10")
    def test_resolve_local_ipv4_prefers_routed_ipv4(self, _mock_routed) -> None:
        self.assertEqual(resolve_local_ipv4(self.config), "192.168.0.10")

    @patch("services.broker.result_queue._resolve_first_non_loopback_ipv4", return_value="10.0.0.25")
    @patch("services.broker.result_queue._resolve_routed_ipv4", return_value=None)
    def test_resolve_local_ipv4_falls_back_to_hostname_ipv4(self, _mock_routed, _mock_fallback) -> None:
        self.assertEqual(resolve_local_ipv4(self.config), "10.0.0.25")

    @patch("services.broker.result_queue._resolve_first_non_loopback_ipv4", return_value=None)
    @patch("services.broker.result_queue._resolve_routed_ipv4", return_value=None)
    def test_resolve_local_ipv4_raises_when_no_ipv4_found(self, _mock_routed, _mock_fallback) -> None:
        with self.assertRaises(RuntimeError):
            resolve_local_ipv4(self.config)

    def test_resolve_result_queue_name_appends_ipv4_suffix(self) -> None:
        queue_name = resolve_result_queue_name("task.result.client", "192.168.0.10")
        self.assertEqual(queue_name, "task.result.client_192.168.0.10")


if __name__ == "__main__":
    unittest.main()
