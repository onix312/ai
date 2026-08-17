# -*- mode: python ; coding: utf-8 -*-
# Сборка Windows-бинаря PrintFlow (5.0) одной командой:
#   pip install pyinstaller
#   pyinstaller connector/pyinstaller.spec --noconfirm
# Результат: dist/PrintFlow/PrintFlow.exe — «двойной клик» без Python.
a = Analysis(
    ['printflow_connector.py'],
    pathex=['.'],
    binaries=[],
    datas=[('site', 'site')],
    hiddenimports=[],
    hookspath=[],
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
    name='PrintFlow',
    console=True,
    icon=None,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='PrintFlow')
