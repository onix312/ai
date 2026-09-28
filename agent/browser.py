"""Browser Provider 1.0: read-only structured Chromium context over local CDP.

The provider never accepts arbitrary JavaScript from callers. It connects only to
loopback DevTools endpoints and evaluates a fixed inspection script that returns
visible text, links, buttons, form metadata and current selection. Form values
are intentionally not returned.
"""
from __future__ import annotations

import importlib.util
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from . import config

MAX_TABS = 50
MAX_TEXT = 30000
MAX_LINKS = 120
MAX_BUTTONS = 80
MAX_FORMS = 30

_INSPECT_JS = r"""
(() => {
  const maxText = %d;
  const maxLinks = %d;
  const maxButtons = %d;
  const maxForms = %d;
  const visible = (el) => {
    if (!el || !el.getBoundingClientRect) return false;
    const s = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return s.visibility !== 'hidden' && s.display !== 'none' &&
           Number(s.opacity || 1) > 0 && r.width > 0 && r.height > 0;
  };
  const clean = (value, limit=500) =>
    String(value || '').replace(/\s+/g, ' ').trim().slice(0, limit);
  const labelFor = (el) => {
    try {
      if (el.labels && el.labels.length) return clean(el.labels[0].innerText || el.labels[0].textContent);
      const wrapped = el.closest('label');
      if (wrapped) return clean(wrapped.innerText || wrapped.textContent);
      if (el.id) {
        const explicit = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
        if (explicit) return clean(explicit.innerText || explicit.textContent);
      }
    } catch (_) {}
    return '';
  };

  const links = Array.from(document.querySelectorAll('a[href]'))
    .filter(visible)
    .slice(0, maxLinks)
    .map((el) => ({
      text: clean(el.innerText || el.textContent),
      href: /^https?:/i.test(el.href || '') ? clean(el.href, 1600) : ''
    }))
    .filter((row) => row.text || row.href);

  const buttons = Array.from(document.querySelectorAll(
      'button, input[type="button"], input[type="submit"], input[type="reset"], [role="button"]'))
    .filter(visible)
    .slice(0, maxButtons)
    .map((el) => ({
      text: clean(el.innerText || el.value || el.getAttribute('aria-label') || el.title),
      type: clean(el.getAttribute('type') || el.getAttribute('role') || el.tagName.toLowerCase(), 80),
      disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true'
    }))
    .filter((row) => row.text);

  const forms = Array.from(document.forms)
    .filter(visible)
    .slice(0, maxForms)
    .map((form, index) => ({
      index,
      action: /^https?:/i.test(form.action || '') ? clean(form.action, 1600) : '',
      method: clean(form.method || 'get', 20).toLowerCase(),
      fields: Array.from(form.elements)
        .filter((el) => visible(el) && !['hidden', 'submit', 'button', 'reset', 'image'].includes(
          String(el.type || '').toLowerCase()))
        .slice(0, 50)
        .map((el) => {
          const type = clean(el.type || el.tagName.toLowerCase(), 80).toLowerCase();
          const autocomplete = clean(el.autocomplete, 120).toLowerCase();
          const sensitive = type === 'password' || /password|cc-|card|cvc|cvv|one-time-code/.test(autocomplete);
          return {
            tag: clean(el.tagName, 30).toLowerCase(),
            type,
            name: clean(el.name, 160),
            id: clean(el.id, 160),
            label: labelFor(el),
            placeholder: sensitive ? '' : clean(el.placeholder, 300),
            autocomplete,
            required: !!el.required,
            disabled: !!el.disabled,
            sensitive,
            filled: sensitive ? null : !!String(el.value || '').trim()
          };
        })
    }));

  let selected = '';
  try { selected = clean(String(window.getSelection ? window.getSelection() : ''), 5000); } catch (_) {}

  return {
    title: clean(document.title, 500),
    url: String(location.href || '').slice(0, 2000),
    text: clean(document.body ? document.body.innerText : '', maxText),
    selection: selected,
    links,
    buttons,
    forms
  };
})()
""" % (MAX_TEXT, MAX_LINKS, MAX_BUTTONS, MAX_FORMS)


class BrowserError(RuntimeError):
    pass


def _loopback_url(url: str, schemes: tuple[str, ...]) -> str:
    clean = str(url or "").strip()
    parsed = urllib.parse.urlsplit(clean)
    if parsed.scheme not in schemes:
        raise BrowserError("Browser Provider принимает только локальный DevTools URL")
    host = (parsed.hostname or "").casefold()
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise BrowserError("Browser Provider подключается только к loopback DevTools")
    if parsed.username or parsed.password:
        raise BrowserError("DevTools URL с логином/паролем запрещён")
    return clean


def _http_json(path: str, timeout: float = 0.8) -> Any:
    base = _loopback_url(config.BROWSER_CDP_URL, ("http", "https")).rstrip("/")
    url = base + path
    request = urllib.request.Request(url, headers={"User-Agent": "NOZZA-BrowserProvider/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(2_000_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raise BrowserError(f"DevTools HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise BrowserError("Chromium DevTools не отвечает на локальном адресе") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BrowserError("DevTools вернул некорректный JSON") from exc


def _websocket_available() -> bool:
    return importlib.util.find_spec("websocket") is not None


def probe() -> tuple[bool, str]:
    if not _websocket_available():
        return False, "Нет websocket-client — Browser Provider недоступен"
    try:
        data = _http_json("/json/version", timeout=0.45)
    except BrowserError as exc:
        return False, str(exc) + "; включите локальный Chromium DevTools endpoint"
    if not isinstance(data, dict):
        return False, "Chromium DevTools ответил неожиданным форматом"
    return True, ""


def tabs(limit: int = 30) -> dict[str, Any]:
    limit = max(1, min(MAX_TABS, int(limit or 30)))
    try:
        data = _http_json("/json/list")
    except BrowserError as exc:
        return {"ok": False, "tabs": [], "reason": str(exc)}
    rows = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict) or item.get("type") != "page":
            continue
        url = str(item.get("url") or "")
        if url.startswith(("devtools://", "chrome-extension://")):
            continue
        rows.append({
            "id": str(item.get("id") or ""),
            "title": str(item.get("title") or "")[:500],
            "url": url[:2000],
            "websocket": str(item.get("webSocketDebuggerUrl") or ""),
        })
        if len(rows) >= limit:
            break
    _mark_active(rows)
    return {"ok": True, "tabs": [{k: v for k, v in row.items() if k != "websocket"} for row in rows],
            "count": len(rows), "reason": ""}


def _raw_tabs() -> list[dict[str, Any]]:
    data = _http_json("/json/list")
    rows: list[dict[str, Any]] = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict) or item.get("type") != "page":
            continue
        ws = str(item.get("webSocketDebuggerUrl") or "")
        if not ws:
            continue
        try:
            _loopback_url(ws, ("ws", "wss"))
        except BrowserError:
            continue
        rows.append({
            "id": str(item.get("id") or ""),
            "title": str(item.get("title") or "")[:500],
            "url": str(item.get("url") or "")[:2000],
            "websocket": ws,
        })
    _mark_active(rows)
    return rows


def _mark_active(rows: list[dict[str, Any]]) -> None:
    active_title = ""
    try:
        from . import winapi
        active_title, _ = winapi.active_window()
    except Exception:
        active_title = ""
    low = active_title.casefold()
    for row in rows:
        title = str(row.get("title") or "")
        row["active"] = bool(title and (title.casefold() in low or low.startswith(title.casefold())))


def _target(target_id: str = "") -> dict[str, Any]:
    rows = _raw_tabs()
    if not rows:
        raise BrowserError("В DevTools нет доступных вкладок")
    key = str(target_id or "").strip()
    if key:
        found = next((row for row in rows if row["id"] == key), None)
        if found is None:
            raise BrowserError("Вкладка не найдена или уже закрыта")
        return found
    return next((row for row in rows if row.get("active")), rows[0])


def _command(target: dict[str, Any], method: str, params: dict[str, Any]) -> dict[str, Any]:
    if not _websocket_available():
        raise BrowserError("Нет websocket-client — Browser Provider недоступен")
    ws_url = _loopback_url(str(target.get("websocket") or ""), ("ws", "wss"))
    try:
        import websocket  # type: ignore
        ws = websocket.create_connection(
            ws_url, timeout=2.0, suppress_origin=True, enable_multithread=False)
    except Exception as exc:
        raise BrowserError(f"Не удалось открыть локальный DevTools WebSocket: {exc.__class__.__name__}") from exc
    try:
        message_id = 1
        ws.send(json.dumps({"id": message_id, "method": method, "params": params},
                           ensure_ascii=False))
        for _ in range(80):
            raw = ws.recv()
            payload = json.loads(raw)
            if payload.get("id") != message_id:
                continue
            if payload.get("error"):
                error = payload["error"]
                raise BrowserError(str(error.get("message") or "DevTools command failed"))
            result = payload.get("result")
            return result if isinstance(result, dict) else {}
        raise BrowserError("DevTools не вернул ответ вовремя")
    except BrowserError:
        raise
    except Exception as exc:
        raise BrowserError(f"DevTools WebSocket оборвался: {exc.__class__.__name__}") from exc
    finally:
        try:
            ws.close()
        except Exception:
            pass


def _evaluate(target: dict[str, Any], expression: str) -> Any:
    result = _command(target, "Runtime.evaluate", {
        "expression": expression,
        "returnByValue": True,
        "awaitPromise": True,
        "userGesture": False,
        "silent": True,
    })
    if result.get("exceptionDetails"):
        raise BrowserError("Страница не дала прочитать DOM")
    value = (result.get("result") or {}).get("value")
    return value


def page(target_id: str = "", max_chars: int = MAX_TEXT) -> dict[str, Any]:
    try:
        target = _target(target_id)
        data = _evaluate(target, _INSPECT_JS)
    except BrowserError as exc:
        return {"ok": False, "reason": str(exc)}
    if not isinstance(data, dict):
        return {"ok": False, "reason": "Страница вернула неожиданные данные"}
    max_chars = max(500, min(MAX_TEXT, int(max_chars or MAX_TEXT)))
    text = str(data.get("text") or "")[:max_chars]
    return {
        "ok": True,
        "target_id": target["id"],
        "title": str(data.get("title") or target.get("title") or "")[:500],
        "url": str(data.get("url") or target.get("url") or "")[:2000],
        "text": text,
        "selection": str(data.get("selection") or "")[:5000],
        "links": list(data.get("links") or [])[:MAX_LINKS],
        "buttons": list(data.get("buttons") or [])[:MAX_BUTTONS],
        "forms": list(data.get("forms") or [])[:MAX_FORMS],
        "reason": "",
    }


def selection(target_id: str = "") -> dict[str, Any]:
    result = page(target_id=target_id, max_chars=500)
    if not result.get("ok"):
        return result
    return {
        "ok": True,
        "target_id": result["target_id"],
        "title": result["title"],
        "url": result["url"],
        "selection": result.get("selection") or "",
        "reason": "" if result.get("selection") else "На странице ничего не выделено",
    }


def find(query: str, target_id: str = "", limit: int = 8) -> dict[str, Any]:
    needle = " ".join(str(query or "").split())
    if not needle:
        return {"ok": False, "reason": "Пустой запрос поиска", "matches": []}
    result = page(target_id=target_id, max_chars=MAX_TEXT)
    if not result.get("ok"):
        return {**result, "matches": []}
    text = str(result.get("text") or "")
    low = text.casefold()
    q = needle.casefold()
    limit = max(1, min(30, int(limit or 8)))
    matches: list[str] = []
    start = 0
    while len(matches) < limit:
        index = low.find(q, start)
        if index < 0:
            break
        left = max(0, index - 140)
        right = min(len(text), index + len(needle) + 220)
        snippet = re.sub(r"\s+", " ", text[left:right]).strip()
        if snippet and snippet not in matches:
            matches.append(snippet)
        start = index + max(1, len(q))
    return {
        "ok": True,
        "query": needle,
        "target_id": result["target_id"],
        "title": result["title"],
        "url": result["url"],
        "matches": matches,
        "count": len(matches),
        "reason": "" if matches else f"На текущей странице «{needle}» не найдено",
    }
