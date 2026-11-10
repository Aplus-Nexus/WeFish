# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
import sys

ROOT = Path(SPECPATH).resolve()
HIDDEN_IMPORTS = [
    "qrcode",
    "qrcode.image.pil",
    "PIL",
    "keyring.backends.SecretService",
]
if sys.platform == "darwin":
    HIDDEN_IMPORTS.append("keyring.backends.macOS")
elif sys.platform == "win32":
    HIDDEN_IMPORTS.append("keyring.backends.Windows")

a = Analysis(
    [str(ROOT / "wefish_service.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[],
    hiddenimports=HIDDEN_IMPORTS,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["PySide6"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="WeFishService",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="WeFishService",
)
