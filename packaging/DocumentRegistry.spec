# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import importlib.metadata as importlib_metadata

import paddlex
from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
    copy_metadata,
)

PACKAGING_DIR = Path(SPECPATH)
ROOT = PACKAGING_DIR.parent
ASSETS = ROOT / "assets"
ICON = ASSETS / "app_icon.ico"

# Main portable application (onedir). OCR resources stay external and are copied
# by build.ps1 so recognition data can be updated without rebuilding the EXE.
main_datas = []
if ASSETS.is_dir():
    main_datas.append((str(ASSETS), "assets"))

# PaddleOCR 3.x resolves the OCR pipeline through PaddleX configuration files
# at runtime. PyInstaller does not discover those YAML/JSON resources or all
# registry-loaded inference modules automatically, so a frozen build can import
# paddleocr successfully yet fail on first OCR with:
#   The pipeline (OCR) does not exist!
# Bundle package data/metadata and dynamic inference modules explicitly.
main_datas += collect_data_files("paddleocr", include_py_files=False)
main_datas += collect_data_files("paddlex", include_py_files=False)
main_datas += copy_metadata("paddleocr")
main_datas += copy_metadata("paddlex")

# Follow PaddleOCR's official PyInstaller packaging guidance for PaddleX 3.x:
# PaddleX decides which optional OCR dependencies are available by inspecting
# installed distribution metadata at runtime. A frozen app therefore needs the
# metadata for every installed PaddleX dependency, not just paddlex itself.
installed_dist_names = {
    dist.metadata.get("Name")
    for dist in importlib_metadata.distributions()
    if dist.metadata.get("Name")
}
try:
    paddlex_dep_names = set(paddlex.utils.deps.BASE_DEP_SPECS.keys())
except Exception:
    paddlex_dep_names = set()
for dep_name in sorted(installed_dist_names & paddlex_dep_names):
    try:
        main_datas += copy_metadata(dep_name)
    except Exception:
        # Some editable/local distributions may not expose copyable metadata.
        # Analysis will still discover their Python modules normally.
        pass

# Paddle/PaddleX load native runtime libraries dynamically. PyInstaller cannot
# reliably infer every DLL from PaddleOCR imports, so include Paddle's dynamic
# libraries explicitly (equivalent to the official --collect-binaries paddle).
main_binaries = collect_dynamic_libs("paddle")

main_hiddenimports = ["onnxruntime"]
if (ROOT / "app" / "update_config_local.py").is_file():
    main_hiddenimports.append("app.update_config_local")
main_hiddenimports += collect_submodules("paddleocr._pipelines")
main_hiddenimports += collect_submodules("paddlex.inference")

main_a = Analysis(
    [str(ROOT / "app" / "main.py")],
    pathex=[str(ROOT)],
    binaries=main_binaries,
    datas=main_datas,
    hiddenimports=main_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
main_pyz = PYZ(main_a.pure)

main_exe = EXE(
    main_pyz,
    main_a.scripts,
    [],
    exclude_binaries=True,
    name="DocumentRegistry",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ICON) if ICON.is_file() else None,
)

main_coll = COLLECT(
    main_exe,
    main_a.binaries,
    main_a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="DocumentRegistry",
)

# Small standalone updater. It uses only the Python standard library and is built
# as onefile so it can keep running after DocumentRegistry.exe and its _internal
# directory are replaced.
updater_a = Analysis(
    [str(ROOT / "app" / "updater_main.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["PySide6", "paddleocr", "onnxruntime", "numpy", "openpyxl"],
    noarchive=False,
)
updater_pyz = PYZ(updater_a.pure)

updater_exe = EXE(
    updater_pyz,
    updater_a.scripts,
    updater_a.binaries,
    updater_a.datas,
    [],
    name="DocumentRegistryUpdater",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ICON) if ICON.is_file() else None,
)
