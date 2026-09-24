"""Автоустановка зависимостей ассистента (18.18): проверка и установка систем.

Зачем отдельный модуль, если есть requirements.txt.

requirements.txt ставит всё подряд, а ассистент обязан стартовать и без зависимостей:
окно открывается страницей, навыки недоступны с причиной, а не падают. Поэтому
здесь честная проверка (`check`) и установка только того, чего нет (`install`),
плюс скачивание моделей речи (vosk) и проверка внешних программ (tesseract,
Ollama). Всё через stdlib + pip, без облаков кроме скачивания моделей по
желанию владельца (с подтверждением).

Использование:
  python -m agent.install --check          — что не хватает
  python -m agent.install --install        — поставить pip-пакеты из requirements.txt
  python -m agent.install --full           — pip + модели + проверка tesseract/ollama
  python -m agent.install --models         — только модели речи
"""
from __future__ import annotations

import importlib.util
import pathlib
import shutil
import subprocess
import sys
import urllib.request
import json

IS_WINDOWS = sys.platform.startswith("win")

# Пакеты для полноценного ассистента. Разделены на группы, чтобы владелец видел,
# что именно он ставит, а не «всё сразу».
REQUIRED = [
    # экран
    "mss==9.0.1",
    "pillow==10.4.0",
    # окно и трей
    "pywebview==4.4.1",
    "pystray==0.19.5",
]

VOICE = [
    "vosk==0.3.45",
    "sounddevice==0.4.7",
]

OCR = [
    "rapidocr-onnxruntime==1.3.24",
]

OPTIONAL = [
    "faster-whisper==1.0.3",
]

ALL_PIP = REQUIRED + VOICE + OCR

# Модели речи: маленькие, на CPU, без CUDA.
# Скачиваются только по явному --models / --full, с подтверждением в UI.
VOSK_MODELS = {
    "vosk-model-small-ru-0.22": "https://alphacephei.com/vosk/models/vosk-model-small-ru-0.22.zip",
}

def _has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False

def check() -> dict:
    """Что есть и чего нет — для панели и для `pf.py doctor`."""
    caps = {}
    # pip пакеты
    mods = {
        "mss": _has_module("mss"),
        "PIL": _has_module("PIL"),
        "webview": _has_module("webview"),
        "pystray": _has_module("pystray"),
        "vosk": _has_module("vosk"),
        "sounddevice": _has_module("sounddevice"),
        "rapidocr_onnxruntime": _has_module("rapidocr_onnxruntime"),
        "faster_whisper": _has_module("faster_whisper"),
    }
    caps["pip"] = mods
    caps["missing_pip"] = [k for k, v in mods.items() if not v]

    # внешние программы
    caps["tesseract"] = bool(shutil.which("tesseract"))
    caps["ollama"] = bool(shutil.which("ollama"))
    caps["python"] = sys.version
    caps["platform"] = sys.platform
    caps["windows"] = IS_WINDOWS

    # модели
    models_path = pathlib.Path.home() / ".printflow" / "models"
    caps["models_dir"] = str(models_path)
    caps["vosk_models"] = []
    try:
        if models_path.exists():
            for child in models_path.iterdir():
                if child.is_dir():
                    caps["vosk_models"].append(child.name)
    except OSError:
        pass

    caps["needs_install"] = bool(caps["missing_pip"] or not caps["vosk_models"])
    return caps

def _pip_install(packages: list[str]) -> tuple[bool, str]:
    if not packages:
        return True, "Нечего ставить"
    cmd = [sys.executable, "-m", "pip", "install", "--upgrade"] + packages
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if proc.returncode == 0:
            return True, proc.stdout[-2000:]
        return False, proc.stderr[-2000:] or proc.stdout[-2000:]
    except Exception as exc:
        return False, f"pip не запустился: {exc}"

def install(packages: list[str] | None = None) -> dict:
    """Поставить pip-пакеты. Возвращает отчёт."""
    if packages is None:
        # ставим только то, чего нет
        status = check()
        missing = status.get("missing_pip") or []
        mapping = {
            "mss": "mss==9.0.1",
            "PIL": "pillow==10.4.0",
            "webview": "pywebview==4.4.1",
            "pystray": "pystray==0.19.5",
            "vosk": "vosk==0.3.45",
            "sounddevice": "sounddevice==0.4.7",
            "rapidocr_onnxruntime": "rapidocr-onnxruntime==1.3.24",
            "faster_whisper": "faster-whisper==1.0.3",
        }
        to_install = [mapping[k] for k in missing if k in mapping]
        if not to_install:
            return {"ok": True, "installed": [], "reason": "Всё уже установлено", "missing": missing}
        ok, out = _pip_install(to_install)
        return {"ok": ok, "installed": to_install, "output": out, "reason": "" if ok else out, "missing": missing}
    else:
        ok, out = _pip_install(packages)
        return {"ok": ok, "installed": packages, "output": out, "reason": "" if ok else out}

def install_requirements(requirements_path: str | None = None) -> dict:
    """Поставить из agent/requirements.txt."""
    req = pathlib.Path(requirements_path or pathlib.Path(__file__).parent / "requirements.txt")
    if not req.exists():
        return {"ok": False, "reason": f"Файл {req} не найден"}
    cmd = [sys.executable, "-m", "pip", "install", "-r", str(req)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if proc.returncode == 0:
            return {"ok": True, "reason": "", "output": proc.stdout[-2000:]}
        return {"ok": False, "reason": proc.stderr[-2000:] or proc.stdout[-2000:]}
    except Exception as exc:
        return {"ok": False, "reason": f"pip не запустился: {exc}"}

def download_vosk_model(name: str = "vosk-model-small-ru-0.22") -> dict:
    """Скачать маленькую модель vosk в ~/.printflow/models/. Только по запросу."""
    url = VOSK_MODELS.get(name)
    if not url:
        return {"ok": False, "reason": f"Модель {name} не известна, есть: {', '.join(VOSK_MODELS)}"}
    dest_dir = pathlib.Path.home() / ".printflow" / "models"
    dest_dir.mkdir(parents=True, exist_ok=True)
    zip_path = dest_dir / f"{name}.zip"
    try:
        print(f"Скачиваю {name} из {url} в {zip_path} ...")
        urllib.request.urlretrieve(url, str(zip_path))
        # распаковка
        import zipfile
        with zipfile.ZipFile(str(zip_path), "r") as zf:
            zf.extractall(str(dest_dir))
        zip_path.unlink(missing_ok=True)
        return {"ok": True, "model": name, "dir": str(dest_dir / name), "reason": ""}
    except Exception as exc:
        return {"ok": False, "reason": f"Скачать не удалось: {exc}"}

def full_setup() -> dict:
    """Полная установка: pip + модели + проверка внешних."""
    report: dict = {"steps": []}
    # 1. pip
    pip_res = install()
    report["steps"].append({"step": "pip", **pip_res})
    # 2. модели (только если vosk есть)
    if _has_module("vosk"):
        # проверим есть ли уже модель
        st = check()
        if not st.get("vosk_models"):
            mdl_res = download_vosk_model()
            report["steps"].append({"step": "vosk_model", **mdl_res})
        else:
            report["steps"].append({"step": "vosk_model", "ok": True, "reason": f"Уже есть: {st['vosk_models']}"})
    # 3. внешние
    st = check()
    report["steps"].append({"step": "external", "tesseract": st["tesseract"], "ollama": st["ollama"], "ok": True})
    report["ok"] = all(s.get("ok") for s in report["steps"])
    report["check"] = check()
    return report

def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Автоустановка ассистента NOZZA")
    parser.add_argument("--check", action="store_true", help="Показать что не хватает")
    parser.add_argument("--install", action="store_true", help="Поставить pip-пакеты которых нет")
    parser.add_argument("--requirements", action="store_true", help="Поставить из requirements.txt")
    parser.add_argument("--models", action="store_true", help="Скачать модель vosk small ru")
    parser.add_argument("--full", action="store_true", help="Всё: pip + модели + проверка")
    args = parser.parse_args()

    if args.check or not any([args.install, args.requirements, args.models, args.full]):
        st = check()
        print(json.dumps(st, ensure_ascii=False, indent=2))
        if st["missing_pip"]:
            print(f"\nНе хватает pip-пакетов: {', '.join(st['missing_pip'])}")
            print("Запустите: python -m agent.install --install")
        if not st["vosk_models"]:
            print("Нет моделей речи в ~/.printflow/models — запустите --models")
        if not st["tesseract"]:
            print("Нет tesseract в PATH — OCR не будет работать (опционально)")
        if not st["ollama"]:
            print("Нет ollama в PATH — локальная модель текста не запустится (опционально, нужен для panel.ask)")
        return 0

    if args.install:
        res = install()
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 1

    if args.requirements:
        res = install_requirements()
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 1

    if args.models:
        res = download_vosk_model()
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 1

    if args.full:
        res = full_setup()
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 1

    return 0

if __name__ == "__main__":
    sys.exit(main())
