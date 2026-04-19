# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for IPDK_plus Windows GUI packaging."""

from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path.cwd().resolve()
ICON_PATH = PROJECT_ROOT / "assets" / "IPDK_plus.ico"

datas = [
    (str(PROJECT_ROOT / "ui" / "styles.qss"), "ui"),
    (str(PROJECT_ROOT / "config" / "app_config.yaml"), "config"),
    (str(PROJECT_ROOT / "config" / "recipe_config.yaml"), "config"),
    (str(PROJECT_ROOT / "assets" / "IPDK_plus.ico"), "assets"),
    (str(PROJECT_ROOT / "assets" / "icons" / "status_sidebar_collapse.svg"), "assets/icons"),
    (str(PROJECT_ROOT / "assets" / "icons" / "status_sidebar_expand.svg"), "assets/icons"),
]


a = Analysis(
    [str(PROJECT_ROOT / "main.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="IPDK_plus",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon=str(ICON_PATH),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="IPDK_plus",
)
