# -*- mode: python ; coding: utf-8 -*-
# Сборка Windows-бинаря PrintFlow одной командой (из корня репозитория):
#   pip install pyinstaller
#   pyinstaller connector/pyinstaller.spec --noconfirm
# Или одной командой из корня репозитория: python pf.py build
# Результат: dist/PrintFlow/PrintFlow.exe — «двойной клик» без Python.
#
# Все пути привязаны к SPECPATH (каталог самого spec), поэтому команду
# можно запускать из любого места — не только из корня репозитория.
from pathlib import Path

SPEC = Path(SPECPATH)            # connector/
ROOT = SPEC.parent               # корень репозитория

a = Analysis(
    [str(SPEC / 'printflow_connector.py')],
    pathex=[str(SPEC)],
    binaries=[],
    # Папка сайта целиком (включая materials/ и brand/) попадает внутрь
    # бинаря; в frozen-режиме config.py берёт её из sys._MEIPASS.
    datas=[(str(ROOT / 'site'), 'site')],
    hiddenimports=['paho.mqtt.client', 'webview', 'webview.platforms.winforms', 'webview.platforms.cocoa', 'webview.platforms.qt', 'webview.platforms.gtk'],
    hookspath=[],
    runtime_hooks=[],
    excludes=['tkinter', 'unittest', 'pydoc', 'test'],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='PrintFlow',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,          # окно консоли остаётся: там адреса для телефона и лог
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
    upx=False,
    name='PrintFlow',
)
