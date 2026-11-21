# -*- mode: python ; coding: utf-8 -*-
# Build in the native macOS arm64 venv: python -m PyInstaller --noconfirm --clean scripts/uu-fishing.spec
import platform
from pathlib import Path
from runpy import run_path
import sys

from PyInstaller.utils.hooks import collect_data_files

if sys.platform != "darwin" or platform.machine() != "arm64":
    raise SystemExit("Build UU Fishing.app on macOS using native arm64 Python (not Rosetta).")

root = Path(SPECPATH).resolve().parent
version = run_path(str(root / "uu_fishing" / "__init__.py"))["__version__"]
app_name = "UU Fishing"

a = Analysis(
    [str(root / "scripts" / "macos_app.py")],
    pathex=[str(root)],
    binaries=[],
    datas=[
        (str(root / "config.toml"), "."),
        (str(root / "README.md"), "."),
        (str(root / "third_party"), "third_party"),
        # SoundCard's CFFI backend reads its CoreAudio header at import time.
        *collect_data_files("soundcard"),
    ],
    hiddenimports=["soundcard", "soundcard.coreaudio", "_cffi_backend"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=app_name,
    console=False,
    argv_emulation=False,
    target_arch="arm64",
    codesign_identity=None,  # PyInstaller uses ad-hoc signing on macOS.
    upx=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    name=app_name,
    upx=False,
)
app = BUNDLE(
    coll,
    name=f"{app_name}.app",
    bundle_identifier="org.uu-fishing.desktop",
    version=version,
    info_plist={
        "CFBundleDisplayName": app_name,
        "CFBundleVersion": version,
        "LSUIElement": True,
        "NSMicrophoneUsageDescription": "UU Fishing uses the configured BlackHole virtual input after you click Start.",
    },
)
