# PyInstaller specification for the standalone Windows desktop build.
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

hiddenimports = []
for package in ("agent", "pywinauto", "rapidocr_onnxruntime", "vosk"):
    try:
        hiddenimports += collect_submodules(package)
    except Exception:
        pass

datas = []
for package in ("rapidocr_onnxruntime", "vosk"):
    try:
        datas += collect_data_files(package)
    except Exception:
        pass

a = Analysis(
    ["luma.py"],
    pathex=["."],
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
