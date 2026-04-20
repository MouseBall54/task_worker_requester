"""Entry point for RabbitMQ task tracker GUI."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import ctypes
import traceback


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

    try:
        from app.bootstrap import run_app
    except Exception as exc:  # pragma: no cover - defensive packaged-run fallback
        _show_bootstrap_import_error(exc)
        return 1

    return run_app(selected_config)


def _show_bootstrap_import_error(exc: Exception) -> None:
    """Display a native Windows error dialog when bootstrap import fails."""

    message = (
        "IPDK_plus 실행에 필요한 Qt 런타임 로딩에 실패했습니다.\n\n"
        "오류: app.bootstrap import 실패\n"
        f"상세: {exc}\n\n"
        "조치:\n"
        "1) 설치 프로그램에서 Microsoft Visual C++ Redistributable(x64)가 정상 설치되었는지 확인\n"
        "2) 설치를 다시 실행하고 복구 또는 재설치를 진행\n"
        "3) 관리자 권한으로 설치를 실행\n"
    )

    try:
        ctypes.windll.user32.MessageBoxW(0, message, "IPDK_plus 실행 오류", 0x10)
    except Exception:
        print(message)
        traceback.print_exc()


if __name__ == "__main__":
    raise SystemExit(main())
