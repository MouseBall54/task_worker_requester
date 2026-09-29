"""Logging setup helpers."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime, timezone
from pathlib import Path

from app.runtime_paths import resolve_logs_dir
from utils.time_utils import format_seoul_display


class SeoulTimeFormatter(logging.Formatter):
    """Render log record times in Seoul with tenth-second precision."""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:  # noqa: N802
        _ = datefmt
        return format_seoul_display(datetime.fromtimestamp(record.created, timezone.utc))


def setup_logging(level: str = "INFO", logs_dir: str | Path | None = None) -> logging.Logger:
    """Configure root logger for console and file output."""

    logger = logging.getLogger("task_worker_requester")
    logger.setLevel(level.upper())
    logger.propagate = False

    if logger.handlers:
        return logger

    formatter = SeoulTimeFormatter("%(asctime)s | %(levelname)s | %(message)s")

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    resolved_logs_dir = Path(logs_dir) if logs_dir is not None else resolve_logs_dir()

    try:
        resolved_logs_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            resolved_logs_dir / "app.log",
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
    except OSError as exc:
        logger.warning("파일 로그를 초기화하지 못해 콘솔 로그만 사용합니다: %s", exc)
        return logger

    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger
