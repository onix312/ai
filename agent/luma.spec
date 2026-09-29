# PyInstaller specification for the standalone Windows desktop build.
import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

hiddenimports = []
for package in ("agent", "pywinauto", "rapidocr_onnxruntime", "vosk"):
    try:
        hiddenimports += collect_submodules(package)
    except Exception:
        pass

datas = []
datas.append((
    os.path.join(ROOT, "agent", "native_ui", "assets", "luma-portrait.png"),
    os.path.join("agent", "native_ui", "assets"),
))
datas.append((
    os.path.join(ROOT, "agent", "native_ui", "assets", "luma-night-bg.png"),
    os.path.join("agent", "native_ui", "assets"),
))
for package in ("rapidocr_onnxruntime", "vosk"):
    try:
        datas += collect_data_files(package)
    except Exception:
        pass

a = Analysis(
    [os.path.join(ROOT, "luma.py")],
    pathex=[ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Luma",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Luma",
)
