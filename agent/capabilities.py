"""Что агент умеет прямо сейчас — честно, без обещаний.

Панель помощника и `pf.py doctor` показывают этот список, поэтому отсутствующая
зависимость видна владельцу сразу: «нет OCR» вместо кнопки, которая молча ничего
не делает. Проверки идут один раз при старте и по запросу `/capabilities`.

С 18.14 список делится на две части. `detect()` — статика: платформа и
библиотеки, их видно без сети. `dynamic()` — живые соседи: отвечает ли панель
PrintFlow и рантайм модели. Разделение не косметическое: агент обязан стартовать
с выключенной панелью и не тормозить `/health` на сетевых пингах.
"""
from __future__ import annotations

import importlib.util
import shutil
import sys

WINDOWS = sys.platform.startswith("win")


def _has(module: str) -> bool:
    return importlib.util.find_spec(module) is not None


_DETECT_CACHE: dict | None = None


def detect() -> dict:
    """Карта возможностей: что включено и почему.

    С 18.23 результат кэшируется процессом: статика (платформа, библиотеки,
    движок озвучки) не меняется, пока жив агент, а `loadStatus` окна зовёт
    `/capabilities` каждые 30 секунд — раньше каждый вызов заново сканировал
    модули и папки моделей. Живые соседи (панель, модель) проверяет `dynamic()`.
    """
    global _DETECT_CACHE
    if _DETECT_CACHE is None:
        _DETECT_CACHE = _detect_now()
    return dict(_DETECT_CACHE)


def _detect_now() -> dict:
    """Свежая карта возможностей — один раз при старте агента."""
    screen_ok = bool(WINDOWS and (_has("mss") or _has("PIL")))
    screen_reason = "" if screen_ok else ("Нет mss или Pillow — снимок экрана недоступен" if WINDOWS else "Снимок экрана работает только в Windows")
    capabilities = {
        "windows": WINDOWS,
        "windows_reason": "" if WINDOWS else "Управление окнами работает только в Windows",
        "active_window": WINDOWS,
        "click": WINDOWS,
        "type": WINDOWS,
        "screen": screen_ok,
        "screen_reason": screen_reason,
        "ocr": bool(_has("rapidocr_onnxruntime") or _has("pytesseract")),
        "ocr_reason": "" if (_has("rapidocr_onnxruntime") or _has("pytesseract")) else "Нет OCR-библиотеки: агент показывает снимок человеку и не угадывает текст",
        "tesseract": bool(shutil.which("tesseract")),
        "speech": bool(_has("vosk") or _has("faster_whisper")),
        "speech_reason": "" if (_has("vosk") or _has("faster_whisper")) else "Нет vosk или faster-whisper — распознавание речи недоступно",
        "microphone": bool(_has("sounddevice") or _has("pyaudio")),
        "microphone_reason": "" if (_has("sounddevice") or _has("pyaudio")) else "Нет sounddevice или pyaudio — микрофон недоступен",
        # 18.14: способности ассистента, которые не зависят от Windows.
        "files": True,
        "files_reason": "",
        "sqlite": _has("sqlite3"),
        "sqlite_reason": "" if _has("sqlite3") else "Нет sqlite3 — память ассистента (журнал, индекс) недоступна",
        "tray": bool(_has("pystray")),
        "tray_reason": "" if _has("pystray") else "Нет pystray — иконки в трее не будет, окно ассистента открывается страницей",
        "window": bool(_has("webview")),
        "window_reason": "" if _has("webview") else ("Нет pywebview — окно ассистента откроется в браузере по адресу, который напечатает агент"),
        # 18.15: Авито и ТГ — сеть и своя база, без внешних зависимостей.
        "network": True,
        "network_reason": "",
        "avito": True,
        "avito_reason": "",
        "tg": True,
        "tg_reason": "",
        "speech_model": "",
        # 18.17: полноценный ассистент ПК — система, буфер, таймеры, предпочтения
        "system": WINDOWS,
        "system_reason": "" if WINDOWS else "Системные действия работают только в Windows",
        "autostart": WINDOWS,
        "autostart_reason": "" if WINDOWS else "Автозагрузка настраивается только в Windows",
        "process_list": True,
        "process_list_reason": "",
        "audio_device": WINDOWS,
        "audio_device_reason": "" if WINDOWS else "Переключение звука — только Windows",
        # Буфер обмена читается через Win32 (`clipboard.py`): в Linux навык
        # честно недоступен, а не «доступен и падает» (до 18.21 было True везде).
        "clipboard": WINDOWS,
        "clipboard_reason": "" if WINDOWS else "Буфер обмена читается только в Windows",
        "region_shot": screen_ok,
        "region_shot_reason": screen_reason,
        "focus_timer": True,
        "focus_timer_reason": "",
        "file_watch": True,
        "file_watch_reason": "",
        "preferences": True,
        "preferences_reason": "",
        "whitelist": True,
        "whitelist_reason": "",
        "macro": True,
        "macro_reason": "",
        "quick_open": True,
        "quick_open_reason": "",
    }
    if capabilities["speech"]:
        capabilities["speech_model"] = _speech_model()
    capabilities["wake_word"] = bool(capabilities["speech"] and capabilities["microphone"])
    # 18.21: способности голоса, которых требуют навыки `voice.*`. До этого их
    # не вычислял никто, и навыки голоса были недоступны всегда — с причиной
    # «Нет способности speech_in», которая ничего не объясняла.
    capabilities["speech_in"] = bool(capabilities["speech"] and capabilities["microphone"])
    capabilities["speech_in_reason"] = (
        "" if capabilities["speech_in"]
        else "Нужны модель речи и микрофон: " + "; ".join(
            reason for reason in (capabilities["speech_reason"], capabilities["microphone_reason"]) if reason))
    from . import pc

    engine = pc.speech_engine()
    capabilities["speech_out"] = bool(engine)
    capabilities["speech_out_engine"] = engine
    capabilities["speech_out_reason"] = "" if engine else (
        "Нет движка озвучки: в Windows нужен PowerShell, в Linux — espeak-ng")
    return capabilities


def _speech_model() -> str:
    """Какая модель речи найдена на диске. Ищем по обычным местам."""
    import pathlib

    candidates = [pathlib.Path("models"), pathlib.Path.home() / ".printflow" / "models",
                  pathlib.Path(__file__).resolve().parent / "models"]
    for folder in candidates:
        try:
            for child in sorted(folder.glob("*")):
                if child.is_dir() and (child / "am").is_dir():
                    return f"vosk:{child.name}"
                if child.suffix in (".bin", ".onnx") and "whisper" in child.name.lower():
                    return f"whisper:{child.name}"
        except OSError:
            continue
    return ""


def dynamic(panel_url: str = "", model_url: str = "") -> dict:
    """Живые способности: панель PrintFlow и рантайм модели (короткие пинги).

    Причина отсутствия обязательна и конкретна: навык `agent.why` показывает её
    владельцу дословно, поэтому «недоступно» без объяснения здесь не бывает.
    """
    from . import model
    from .panel_client import Client

    out: dict = {}
    client = Client(panel_url) if panel_url else None
    if client is None:
        out.update(panel=False, panel_reason="Адрес панели не задан (PRINTFLOW_URL)")
    else:
        state = client.status()
        out.update(panel=bool(state["alive"]),
                   panel_reason="" if state["alive"] else str(state["reason"]))
    state = model.status(model_url)
    out.update(model=bool(state["ok"]),
               model_reason="" if state["ok"] else str(state["reason"]))
    return out


def missing(capabilities: dict) -> list[str]:
    """Причины, по которым агент не умеет того, что обещает панель."""
    return [str(capabilities[key]) for key in ("windows_reason", "screen_reason",
                                               "ocr_reason", "speech_reason",
                                               "microphone_reason", "files_reason",
                                               "sqlite_reason", "tray_reason",
                                               "window_reason", "panel_reason",
                                               "model_reason", "system_reason",
                                               "autostart_reason", "audio_device_reason",
                                               "clipboard_reason", "region_shot_reason",
                                               "focus_timer_reason", "file_watch_reason",
                                               "preferences_reason", "whitelist_reason",
                                               "macro_reason", "quick_open_reason",
                                               "speech_in_reason", "speech_out_reason")
            if capabilities.get(key)]
