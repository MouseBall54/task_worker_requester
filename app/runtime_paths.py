"""Runtime path helpers for source and packaged Windows execution."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import sys


APPDATA_DIR_NAME = "TaskWorkerRequester"
CONFIG_FILE_NAME = "app_config.yaml"
RECIPE_CONFIG_FILE_NAME = "recipe_config.yaml"


class RuntimePathError(RuntimeError):
    """Raised when runtime config/resource paths cannot be resolved safely."""


@dataclass(slots=True)
class RuntimeConfigPaths:
    """Resolved AppData config locations and seed sources."""

    appdata_dir: Path
    user_config_path: Path
    user_recipe_config_path: Path
    seed_config_source: Path | None
    seed_recipe_source: Path | None


def normalize_cli_path(path_value: str | Path) -> Path:
    """Normalize a CLI-supplied path into an absolute filesystem path."""

    raw = str(path_value).strip()
    if not raw:
        raise RuntimePathError("빈 config 경로는 사용할 수 없습니다.")

    expanded = Path(os.path.expandvars(raw)).expanduser()
    if expanded.is_absolute():
        return expanded.resolve()
    return (Path.cwd() / expanded).resolve()


def resolve_runtime_base_dir() -> Path:
    """Return the active runtime bundle root.

    In a PyInstaller build this points to ``sys._MEIPASS``.
    In source execution it points to the repository root.
    """

    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        return Path(str(bundle_root))
    return _development_root()


def resolve_install_dir() -> Path:
    """Return the executable directory for packaged runs, else repo root."""

    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return _development_root()


def resolve_user_appdata_dir() -> Path:
    """Return the AppData directory used for editable user config files."""

    appdata_root = str(os.environ.get("APPDATA", "")).strip()
    if appdata_root:
        return Path(appdata_root) / APPDATA_DIR_NAME
    return Path.home() / "AppData" / "Roaming" / APPDATA_DIR_NAME


def resolve_stylesheet_path() -> Path | None:
    """Return the first available QSS path for the current runtime."""

    return find_bundled_resource(Path("ui") / "styles.qss")


def ensure_user_config_seeded() -> RuntimeConfigPaths:
    """Create AppData config files from bundled templates when missing."""

    appdata_dir = resolve_user_appdata_dir()
    appdata_dir.mkdir(parents=True, exist_ok=True)

    user_config_path = appdata_dir / CONFIG_FILE_NAME
    user_recipe_config_path = appdata_dir / RECIPE_CONFIG_FILE_NAME
    seed_config_source = find_bundled_resource(Path("config") / CONFIG_FILE_NAME)
    seed_recipe_source = find_bundled_resource(Path("config") / RECIPE_CONFIG_FILE_NAME)

    if not user_config_path.exists():
        if seed_config_source is None:
            raise RuntimePathError(
                "기본 app_config.yaml 템플릿을 찾지 못해 AppData 초기 설정 파일을 만들 수 없습니다."
            )
        shutil.copy2(seed_config_source, user_config_path)

    if not user_recipe_config_path.exists() and seed_recipe_source is not None:
        shutil.copy2(seed_recipe_source, user_recipe_config_path)

    return RuntimeConfigPaths(
        appdata_dir=appdata_dir,
        user_config_path=user_config_path,
        user_recipe_config_path=user_recipe_config_path,
        seed_config_source=seed_config_source,
        seed_recipe_source=seed_recipe_source,
    )


def resolve_default_config_path(explicit_config_path: str | Path | None = None) -> Path:
    """Resolve the app config path using installer-friendly search order."""

    if explicit_config_path is not None and str(explicit_config_path).strip():
        return normalize_cli_path(explicit_config_path)

    last_seed_error: RuntimePathError | None = None
    try:
        seeded = ensure_user_config_seeded()
        if seeded.user_config_path.exists():
            return seeded.user_config_path
    except RuntimePathError as exc:
        last_seed_error = exc

    for candidate in _executable_adjacent_config_candidates():
        if candidate.exists():
            return candidate

    dev_candidate = _development_root() / "config" / CONFIG_FILE_NAME
    if dev_candidate.exists():
        return dev_candidate

    if last_seed_error is not None:
        raise last_seed_error

    raise RuntimePathError(
        "app_config.yaml 을 찾지 못했습니다. --config 로 직접 지정하거나 "
        f"{resolve_user_appdata_dir()} 아래 기본 설정 파일을 준비해주세요."
    )


def find_bundled_resource(relative_path: str | Path) -> Path | None:
    """Find one bundled runtime resource across bundle/install/source roots."""

    normalized_relative = Path(relative_path)
    for root in _candidate_roots():
        candidate = root / normalized_relative
        if candidate.exists():
            return candidate
    return None


def _candidate_roots() -> list[Path]:
    """Return deduplicated runtime roots searched for bundled resources."""

    roots: list[Path] = []
    seen: set[str] = set()

    for root in (resolve_runtime_base_dir(), resolve_install_dir(), _development_root()):
        normalized = root.resolve()
        key = str(normalized).lower()
        if key in seen:
            continue
        seen.add(key)
        roots.append(normalized)

    return roots


def _executable_adjacent_config_candidates() -> list[Path]:
    """Return config candidates next to the installed executable."""

    install_dir = resolve_install_dir()
    return [
        install_dir / "config" / CONFIG_FILE_NAME,
        install_dir / CONFIG_FILE_NAME,
    ]


def _development_root() -> Path:
    """Return the repository root during source execution."""

    return Path(__file__).resolve().parents[1]
