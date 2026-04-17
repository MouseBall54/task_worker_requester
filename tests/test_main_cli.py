"""Tests for CLI config argument parsing."""

from __future__ import annotations

import unittest

from main import build_arg_parser


class MainCliTest(unittest.TestCase):
    """Validate config argument compatibility and precedence."""

    def test_parser_accepts_explicit_config_option(self) -> None:
        parser = build_arg_parser()
        args = parser.parse_args(["--config", "D:/custom/app.yaml"])

        self.assertEqual(args.config_path, "D:/custom/app.yaml")
        self.assertIsNone(args.legacy_config)

    def test_parser_keeps_legacy_positional_config_for_compatibility(self) -> None:
        parser = build_arg_parser()
        args = parser.parse_args(["config/app_config.yaml"])

        self.assertEqual(args.legacy_config, "config/app_config.yaml")
        self.assertIsNone(args.config_path)


if __name__ == "__main__":
    unittest.main()
