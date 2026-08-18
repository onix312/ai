#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PrintFlow — Bambu Studio post-processing script 8.0

Копирует 3MF в Watch Folder и уведомляет PrintFlow.

Установка:
  1. Скопируйте этот файл в папку Bambu Studio (Windows: %APPDATA%\\BambuStudio\\scripts)
  2. В Bambu Studio: Print Settings → Post-processing scripts → добавьте путь к этому файлу
  3. Настройте Watch Folder в PrintFlow: Настройки → Производство → Watch Folder

Переменная окружения SLIC3R_PP_OUTPUT_NAME содержит путь к итоговому 3MF.
"""
import os
import shutil
import pathlib
import urllib.request
import json

def main():
    src = os.environ.get("SLIC3R_PP_OUTPUT_NAME", "")
    if not src:
        # Bambu Studio старых версий использует другую переменную
        src = os.environ.get("BAMBU_PP_OUTPUT_NAME", "")
    if not src:
        return
    src_path = pathlib.Path(src)
    if not src_path.exists():
        return
    # целевая папка — по умолчанию ~/PrintFlow-Inbox, можно переопределить env
    watch = os.environ.get("PRINTFLOW_WATCH", str(pathlib.Path.home() / "PrintFlow-Inbox"))
    watch_path = pathlib.Path(watch)
    try:
        watch_path.mkdir(parents=True, exist_ok=True)
        dst = watch_path / src_path.name
        shutil.copy2(src_path, dst)
        print(f"PrintFlow: copied to {dst}")
    except Exception as e:
        print(f"PrintFlow copy failed: {e}")
        return
    # уведомляем PrintFlow по сети
    for url in [
        os.environ.get("PRINTFLOW_URL", "http://127.0.0.1:8080/api/slicer/push"),
        "http://localhost:8080/api/slicer/push",
        "http://printflow.local:8080/api/slicer/push",
    ]:
        try:
            data = json.dumps({"file": str(dst), "source": "bambu_post"}).encode()
            req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=2) as r:
                if r.status == 200:
                    print(f"PrintFlow: notified {url}")
                    break
        except Exception:
            continue

if __name__ == "__main__":
    main()
