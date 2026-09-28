"""Read-only desktop observations, with bounded UIA and OCR workers."""
from __future__ import annotations

import json
import subprocess
import sys
from collections import deque

from . import pc


def _worker(mode: str, payload: bytes = b"", hwnd: int = 0, limit: int = 80) -> tuple[dict, str]:
    try:
        result = subprocess.run(
            [sys.executable, "-m", "agent.perception", mode, str(hwnd), str(limit)],
            input=payload, capture_output=True, timeout=6 if mode == "uia" else 12,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {}, f"{mode.upper()} не ответил вовремя"
    except OSError as exc:
        return {}, f"Не удалось запустить {mode.upper()}: {exc}"
    if result.returncode:
        return {}, result.stderr.decode("utf-8", "replace")[-300:] or f"{mode.upper()} недоступен"
    try:
        return json.loads(result.stdout.splitlines()[-1]), ""
    except (ValueError, UnicodeError, IndexError):
        return {}, f"{mode.upper()} вернул неверный ответ"


def observe(title: str = "", limit: int = 80, screenshot: bool = False,
            ocr: bool = True) -> dict:
    """Prefer OS window state and UIA; use OCR only if controls have no text."""
    limit = max(1, min(150, int(limit)))
    window, reason = pc.find_window(title)
    if window is None:
        return {"ok": False, "reason": reason or "Окно не найдено"}
    result = {"ok": True, "window": window, "controls": [], "focused": None,
              "sources": ["win32"], "reason": "", "warnings": []}
    if window.get("minimized"):
        result["warnings"].append("Окно свёрнуто: его элементы и изображение могут быть недоступны")
    else:
        from .capabilities import detect
        if detect().get("uia"):
            data, error = _worker("uia", hwnd=window["hwnd"], limit=limit)
            if data:
                result["controls"] = data.get("controls", [])
                result["focused"] = data.get("focused")
                result["sources"].append("uia")
            elif error:
                result["warnings"].append(error)
        if not result["controls"]:
            controls, error = pc.controls(window["title"], limit)
            result["controls"] = controls
            if controls:
                result["sources"].append("win32_controls")
            if error:
                result["warnings"].append(error)
    if ocr and not any(c.get("text") for c in result["controls"]):
        matches, error = read_screen_text()
        if matches:
            result["ocr"] = matches
            result["sources"].append("ocr")
        elif error:
            result["warnings"].append(error)
    if screenshot:
        from .winapi import grab_screen
        image, error = grab_screen()
        result["screenshot"] = {"available": bool(image), "bytes": len(image),
                                "url": "/screen" if image else ""}
        if error:
            result["warnings"].append(error)
    return result


def read_screen_text() -> tuple[list[dict], str]:
    from .capabilities import detect
    if not detect().get("ocr"):
        return [], "OCR не установлен"
    try:
        import mss
        import mss.tools
        with mss.mss() as shot:
            monitor = shot.monitors[0]
            image = shot.grab(monitor)
            png = mss.tools.to_png(image.rgb, image.size)
        data, reason = _worker("ocr", payload=png)
        if reason:
            return [], reason
        rows = data.get("matches", [])
        for row in rows:
            row["rect"] = {key: value + monitor["left" if key in ("left", "right") else "top"]
                           for key, value in row["rect"].items()}
        return rows, ""
    except (ImportError, OSError) as exc:
        return [], f"Не удалось снять экран для OCR: {exc}"


def find_text(text: str) -> tuple[dict, str]:
    needle = str(text or "").strip().casefold()
    if not needle:
        return {}, "Пустой запрос"
    window, _ = pc.find_window("")
    windows, _ = pc.windows(100)
    for row in windows:
        if needle in row["title"].casefold():
            return {"found": True, "method": "window_title", "title": row["title"]}, ""
    if window:
        from .capabilities import detect
        if detect().get("uia"):
            data, _ = _worker("uia", hwnd=window["hwnd"])
            for control in data.get("controls", []):
                if needle in control.get("text", "").casefold():
                    return {"found": True, "method": "uia", "window": window["title"],
                            **control}, ""
        controls, _ = pc.controls(window["title"])
        for control in controls:
            if needle in control.get("text", "").casefold():
                return {"found": True, "method": "win32_controls", "window": window["title"],
                        **control}, ""
    matches, reason = read_screen_text()
    for match in matches:
        if needle in match["text"].casefold():
            return {"found": True, "method": "ocr", **match}, ""
    return {}, reason if reason and not window else f"Текст «{text}» не найден"


def _rect(rect) -> dict:
    return {"left": rect.left, "top": rect.top, "right": rect.right, "bottom": rect.bottom}


def _uia(hwnd: int, limit: int) -> dict:
    from pywinauto import Desktop
    root = Desktop(backend="uia").window(handle=hwnd)
    queue = deque([(root, 0)])
    controls = []
    focused = None
    while queue and len(controls) < limit:
        item, depth = queue.popleft()
        try:
            info = item.element_info
            row = {"text": str(info.name or "")[:500], "type": str(info.control_type or ""),
                   "rect": _rect(item.rectangle()), "depth": depth}
            if item.has_keyboard_focus():
                focused = row
            if row["text"] or row["type"] in ("Button", "Edit", "ComboBox"):
                controls.append(row)
            if depth < 6:
                queue.extend((child, depth + 1) for child in item.children()[:limit])
        except (OSError, RuntimeError, AttributeError):
            continue
    return {"controls": controls, "focused": focused}


def _ocr(payload: bytes) -> dict:
    import io
    from PIL import Image
    import numpy as np
    from rapidocr_onnxruntime import RapidOCR
    image = np.asarray(Image.open(io.BytesIO(payload)).convert("RGB"))
    rows, _ = RapidOCR()(image)
    matches = []
    for box, value, confidence in (rows or []):
        xs, ys = zip(*box)
        matches.append({"text": str(value)[:500], "confidence": round(float(confidence), 3),
                        "rect": {"left": int(min(xs)), "top": int(min(ys)),
                                 "right": int(max(xs)), "bottom": int(max(ys))}})
    return {"matches": matches[:300]}


if __name__ == "__main__":
    try:
        mode = sys.argv[1]
        result = _uia(int(sys.argv[2]), int(sys.argv[3])) if mode == "uia" else _ocr(sys.stdin.buffer.read())
        sys.stdout.write(json.dumps(result, ensure_ascii=False))
    except Exception as exc:
        sys.stderr.write(f"{type(exc).__name__}: {exc}")
        raise SystemExit(1)
