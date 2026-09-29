"""Tests for installer-friendly logging path behavior."""

from __future__ import annotations

import logging
import os
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from utils.logging_setup import setup_logging


class LoggingSetupTest(unittest.TestCase):
    """Ensure file logging uses per-user AppData rather than current directory."""

    def tearDown(self) -> None:
        self._reset_logger()

    def test_setup_logging_writes_app_log_under_appdata_logs(self) -> None:
        with TemporaryDirectory() as appdata_root, TemporaryDirectory() as install_dir:
            original_cwd = Path.cwd()
            os.chdir(install_dir)
            try:
                with patch.dict(os.environ, {"APPDATA": appdata_root}, clear=False):
                    logger = setup_logging("INFO")
                    logger.info("installer-log-test")

                expected_log = Path(appdata_root) / "IPDK_plus" / "logs" / "app.log"
                self.assertTrue(expected_log.exists())
                log_text = expected_log.read_text(encoding="utf-8")
                self.assertIn("installer-log-test", log_text)
                self.assertIsNotNone(
                    re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d \| INFO", log_text)
                )
                self.assertFalse((Path(install_dir) / "logs" / "app.log").exists())
            finally:
                self._reset_logger()
                os.chdir(original_cwd)

    def test_setup_logging_keeps_stream_logging_when_file_handler_fails(self) -> None:
        with TemporaryDirectory() as appdata_root:
            with patch.dict(os.environ, {"APPDATA": appdata_root}, clear=False):
                with patch(
                    "utils.logging_setup.RotatingFileHandler",
                    side_effect=PermissionError("denied"),
                ):
                    logger = setup_logging("INFO")

            self.assertTrue(any(isinstance(handler, logging.StreamHandler) for handler in logger.handlers))
            self.assertFalse(any(type(handler) is logging.FileHandler for handler in logger.handlers))

    @staticmethod
    def _reset_logger() -> None:
        """Close and remove handlers so each test gets a clean logger."""

        logger = logging.getLogger("task_worker_requester")
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)
        logger.propagate = False


if __name__ == "__main__":
    unittest.main()
