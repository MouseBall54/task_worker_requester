"""Runtime path helpers for source and packaged Windows execution."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import os
from pathlib import Path
import shutil
import sys


APPDATA_DIR_NAME = "IPDK_plus"
LEGACY_APPDATA_DIR_NAME = "TaskWorkerRequester"
CONFIG_FILE_NAME = "app_config.yaml"
RECIPE_CONFIG_FILE_NAME = "recipe_config.yaml"
APP_ICON_PNG_NAME = "IPDK_plus.png"
APP_ICON_ICO_NAME = "IPDK_plus.ico"
SEED_REFRESH_MARKER_NAME = ".refresh_seed_config"
SEED_FINGERPRINT_FILE_NAME = ".seed_fingerprint"


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

    return _resolve_appdata_dir(APPDATA_DIR_NAME)


def resolve_legacy_user_appdata_dir() -> Path:
    """Return the legacy AppData directory used before the IPDK_plus rename."""

    return _resolve_appdata_dir(LEGACY_APPDATA_DIR_NAME)


def resolve_stylesheet_path() -> Path | None:
    """Return the first available QSS path for the current runtime."""

    return find_bundled_resource(Path("ui") / "styles.qss")


def resolve_app_icon_path() -> Path | None:
    """Return the bundled application icon path for runtime UI usage."""

    return find_first_bundled_resource(
        [
            Path("assets") / APP_ICON_PNG_NAME,
            Path("assets") / APP_ICON_ICO_NAME,
        ]
    )


def resolve_ui_icon_path(icon_filename: str) -> Path | None:
    """Return one bundled UI icon from ``assets/icons`` by file name."""

    safe_name = Path(str(icon_filename).strip()).name
    if not safe_name:
        return None
    return find_bundled_resource(Path("assets") / "icons" / safe_name)


def resolve_logs_dir() -> Path:
    """Return the writable per-user log directory used by the application."""

    return resolve_user_appdata_dir() / "logs"


def ensure_user_config_seeded() -> RuntimeConfigPaths:
    """Create AppData config files from bundled templates when missing."""

    migrated_dir = migrate_legacy_appdata_dir()
    appdata_dir = resolve_user_appdata_dir()
    appdata_dir.mkdir(parents=True, exist_ok=True)

    user_config_path = appdata_dir / CONFIG_FILE_NAME
    user_recipe_config_path = appdata_dir / RECIPE_CONFIG_FILE_NAME
    seed_config_source = find_bundled_resource(Path("config") / CONFIG_FILE_NAME)
    seed_recipe_source = find_bundled_resource(Path("config") / RECIPE_CONFIG_FILE_NAME)
    seed_fingerprint = _calculate_seed_fingerprint(seed_config_source, seed_recipe_source)
    refresh_seed = _consume_seed_refresh_marker(appdata_dir) or (
        migrated_dir is None
        and _seed_fingerprint_changed(
            appdata_dir,
            seed_fingerprint,
        )
    )

    if refresh_seed:
        if seed_config_source is None:
            raise RuntimePathError(
                "기본 app_config.yaml 템플릿을 찾지 못해 AppData 설정 파일을 갱신할 수 없습니다."
            )
        _replace_seeded_file_with_backup(user_config_path, seed_config_source)
        if seed_recipe_source is not None:
            _replace_seeded_file_with_backup(user_recipe_config_path, seed_recipe_source)
    elif not user_config_path.exists():
        if seed_config_source is None:
            raise RuntimePathError(
                "기본 app_config.yaml 템플릿을 찾지 못해 AppData 초기 설정 파일을 만들 수 없습니다."
            )
        shutil.copy2(seed_config_source, user_config_path)

    if not user_recipe_config_path.exists() and seed_recipe_source is not None:
        shutil.copy2(seed_recipe_source, user_recipe_config_path)

    _write_seed_fingerprint(appdata_dir, seed_fingerprint)

    return RuntimeConfigPaths(
        appdata_dir=appdata_dir,
        user_config_path=user_config_path,
        user_recipe_config_path=user_recipe_config_path,
        seed_config_source=seed_config_source,
        seed_recipe_source=seed_recipe_source,
    )


def migrate_legacy_appdata_dir() -> Path | None:
    """Copy the old TaskWorkerRequester AppData payload into IPDK_plus once.

    Migration is intentionally conservative:
    - If the new directory already exists and contains files, it wins.
    - If the legacy directory is missing, nothing happens.
    - Otherwise legacy contents are copied into the new location.
    """

    target_dir = resolve_user_appdata_dir()
    legacy_dir = resolve_legacy_user_appdata_dir()

    if not legacy_dir.exists():
        return None
    if _directory_has_entries(target_dir):
        return None

    target_dir.mkdir(parents=True, exist_ok=True)
    for source in legacy_dir.iterdir():
        destination = target_dir / source.name
        if destination.exists():
            continue
        if source.is_dir():
            shutil.copytree(source, destination)
        else:
            shutil.copy2(source, destination)
    return target_dir


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


def find_first_bundled_resource(relative_paths: list[str | Path]) -> Path | None:
    """Find the first available bundled resource from an ordered list."""

    for relative_path in relative_paths:
        found = find_bundled_resource(relative_path)
        if found is not None:
            return found
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


def _resolve_appdata_dir(dir_name: str) -> Path:
    """Build an AppData path for the given application directory name."""

    appdata_root = str(os.environ.get("APPDATA", "")).strip()
    if appdata_root:
        return Path(appdata_root) / dir_name
    return Path.home() / "AppData" / "Roaming" / dir_name


def _directory_has_entries(path: Path) -> bool:
    """Return whether a directory exists and contains at least one entry."""

    if not path.exists() or not path.is_dir():
        return False
    try:
        next(path.iterdir())
    except StopIteration:
        return False
    return True


def _consume_seed_refresh_marker(appdata_dir: Path) -> bool:
    """Return whether installer requested a bundled config refresh."""

    marker_path = appdata_dir / SEED_REFRESH_MARKER_NAME
    if not marker_path.exists():
        return False
    try:
        marker_path.unlink()
    except OSError as exc:
        raise RuntimePathError(f"설정 갱신 marker를 삭제하지 못했습니다: {marker_path}") from exc
    return True


def _replace_seeded_file_with_backup(target_path: Path, seed_path: Path) -> None:
    """Replace an AppData config file from bundled seed, keeping a backup."""

    if target_path.exists():
        try:
            if target_path.read_bytes() == seed_path.read_bytes():
                return
        except OSError:
            pass

        backup_path = _next_backup_path(target_path)
        shutil.copy2(target_path, backup_path)

    shutil.copy2(seed_path, target_path)


def _next_backup_path(target_path: Path) -> Path:
    """Build a non-conflicting backup path next to a refreshed config file."""

    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    for index in range(0, 100):
        suffix = f".bak.{timestamp}" if index == 0 else f".bak.{timestamp}.{index}"
        candidate = target_path.with_name(f"{target_path.name}{suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimePathError(f"설정 백업 파일 이름을 만들 수 없습니다: {target_path}")


def _calculate_seed_fingerprint(seed_config_source: Path | None, seed_recipe_source: Path | None) -> str | None:
    """Calculate a stable fingerprint for bundled seed config files."""

    if seed_config_source is None:
        return None

    digest = hashlib.sha256()
    for label, seed_path in (
        (CONFIG_FILE_NAME, seed_config_source),
        (RECIPE_CONFIG_FILE_NAME, seed_recipe_source),
    ):
        digest.update(label.encode("utf-8"))
        digest.update(b"\0")
        if seed_path is None:
            digest.update(b"<missing>")
            continue
        try:
            digest.update(seed_path.read_bytes())
        except OSError as exc:
            raise RuntimePathError(f"기본 설정 fingerprint를 계산하지 못했습니다: {seed_path}") from exc
        digest.update(b"\0")
    return digest.hexdigest()


def _seed_fingerprint_changed(appdata_dir: Path, seed_fingerprint: str | None) -> bool:
    """Return whether bundled seed files differ from the last applied seed."""

    if seed_fingerprint is None:
        return False

    fingerprint_path = appdata_dir / SEED_FINGERPRINT_FILE_NAME
    if not fingerprint_path.exists():
        return _directory_has_entries(appdata_dir)

    try:
        stored_fingerprint = fingerprint_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimePathError(f"설정 fingerprint를 읽지 못했습니다: {fingerprint_path}") from exc
    return stored_fingerprint != seed_fingerprint


def _write_seed_fingerprint(appdata_dir: Path, seed_fingerprint: str | None) -> None:
    """Persist the bundled seed fingerprint after seeding or refresh."""

    if seed_fingerprint is None:
        return

    fingerprint_path = appdata_dir / SEED_FINGERPRINT_FILE_NAME
    try:
        fingerprint_path.write_text(seed_fingerprint + "\n", encoding="utf-8")
    except OSError as exc:
        raise RuntimePathError(f"설정 fingerprint를 저장하지 못했습니다: {fingerprint_path}") from exc
