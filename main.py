"""Entry point for RabbitMQ task tracker GUI."""

from __future__ import annotations

import argparse
from collections.abc import Sequence


def build_arg_parser() -> argparse.ArgumentParser:
    """Create the CLI parser used by source and packaged execution."""

    parser = argparse.ArgumentParser(description="IPDK_plus")
    parser.add_argument(
        "legacy_config",
        nargs="?",
        help="Legacy positional config path. Prefer --config going forward.",
    )
    parser.add_argument(
        "--config",
        dest="config_path",
        help="Path to app_config.yaml. When omitted, AppData default config is used.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse CLI arguments and launch the Qt application."""

    parser = build_arg_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    selected_config = args.config_path or args.legacy_config
    from app.bootstrap import run_app

    return run_app(selected_config)


if __name__ == "__main__":
    raise SystemExit(main())
