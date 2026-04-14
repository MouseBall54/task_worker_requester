"""Entry point for RabbitMQ task tracker GUI."""

from __future__ import annotations

import sys

from app.bootstrap import run_app


if __name__ == "__main__":
    config_path = "config/app_config.yaml"
    if len(sys.argv) > 1:
        config_path = sys.argv[1]
    raise SystemExit(run_app(config_path))
