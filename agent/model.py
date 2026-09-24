"""Рантайм модели для ассистента (18.14): loopback, stdlib, честный отказ.

Модель у ассистента одна и та же, что у помощника в панели (`assistant.py`):
Ollama-совместимый рантайм на этом компьютере. Отдельный клиент нужен, потому
что агент не импортирует коннектор (ADR-0004), а правила те же:

  * адрес обязан быть loopback — тексты документов не уезжают с машины;
  * отказ всегда с причиной — навык говорит «модель недоступна» и отдаёт то, что
    смог сделать без неё (найденные отрывки, детерминированные факты);
  * модель не источник чисел — суммы и сроки берёт детерминированный разбор, а
    расхождение между ответом модели и найденным текстом показывается как
    предупреждение (`numbers_checked`).
"""
from __future__ import annotations

import json
import re
import socket
import urllib.error
import urllib.request
from typing import Any

from . import config
from .panel_client import loopback_ok

PING_SEC = 2.0
MAX_REPLY_CHARS = 4000
MAX_PROMPT_CHARS = 12000
# Числа в ответе модели сверяются с найденным текстом: уверенно названный
# неверный долг хуже честного «посмотрите в документе».
_NUMBER_RE = re.compile(r"\d[\d\s\u00a0]*(?:[.,]\d+)?")


def _request(url: str, timeout: float, payload: dict[str, Any] | None = None) -> tuple[bool, Any, str]:
    """POST для чата, GET без тела для списка моделей Ollama."""
    local, why = loopback_ok(url)
    if not local:
        return False, None, why
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url, data=body, method="POST" if body is not None else "GET",
        headers={"Accept": "application/json",
                 **({"Content-Type": "application/json; charset=utf-8"} if body is not None else {})})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as answer:  # noqa: S310 — только loopback
            raw = answer.read(8 * 1024 * 1024)
    except urllib.error.HTTPError as exc:
        return False, None, f"рантайм ответил {exc.code}"
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, socket.timeout):
            return False, None, f"рантайм не ответил за {timeout:.0f} с"
        return False, None, f"рантайм недоступен ({reason})"
    except (OSError, ValueError) as exc:
        return False, None, f"рантайм недоступен ({exc.__class__.__name__})"
    try:
        return True, json.loads(raw.decode("utf-8", "replace") or "{}"), ""
    except json.JSONDecodeError:
        return False, None, "рантайм ответил не JSON"


def _get(url: str, timeout: float) -> tuple[bool, Any, str]:
    return _request(url, timeout)


def _post(url: str, payload: dict[str, Any], timeout: float) -> tuple[bool, Any, str]:
    return _request(url, timeout, payload)


def status(url: str = "", name: str = "") -> dict[str, Any]:
    """Жив ли рантайм и какая модель выбрана. Ответ всегда с причиной."""
    url = str(url or config.MODEL_URL).rstrip("/")
    name = str(name or config.MODEL_NAME).strip()
    out: dict[str, Any] = {"ok": False, "url": url, "model": name, "models": [],
                           "reason": "", "loopback": True}
    local, why = loopback_ok(url)
    if not local:
        out.update(loopback=False, reason=f"Адрес рантайма должен быть этим компьютером: {why}")
        return out
    ok, payload, reason = _get(f"{url}/api/tags", PING_SEC)
    if not ok:
        out["reason"] = (f"Рантайм на {url} не отвечает ({reason}). "
                         "Запустите его на этом компьютере — например, `ollama serve`.")
        return out
    models = [str(row.get("name") or "") for row in ((payload or {}).get("models") or [])
              if isinstance(row, dict) and row.get("name")]
    out["models"] = models
    if not name:
        out["reason"] = ("Модель не выбрана: задайте PRINTFLOW_MODEL_NAME "
                         + (f"(рантайм отдаёт: {', '.join(models)})" if models
                            else "(рантайм пока не отдаёт ни одной модели)"))
        return out
    if name not in models and f"{name}:latest" not in models:
        out["reason"] = (f"Модели «{name}» у рантайма нет "
                         + (f"(есть: {', '.join(models)})" if models
                            else "(локальных моделей нет — выполните `ollama pull qwen2.5:3b`)"))
        return out
    out.update(ok=True, reason="")
    return out


def complete(prompt: str, url: str = "", name: str = "",
             timeout: float | None = None) -> dict[str, Any]:
    """Один запрос к модели. Пустой ответ и мусор — тоже отказ с причиной."""
    state = status(url, name)
    if not state["ok"]:
        return {"ok": False, "text": "", "reason": state["reason"], "model": state["model"]}
    text = str(prompt or "")[:MAX_PROMPT_CHARS]
    if not text.strip():
        return {"ok": False, "text": "", "reason": "Пустой запрос к модели",
                "model": state["model"]}
    ok, payload, reason = _post(
        f"{state['url']}/api/chat",
        {"model": state["model"], "stream": False, "options": {"temperature": 0.0},
         "messages": [{"role": "user", "content": text}]},
        float(timeout if timeout is not None else config.MODEL_TIMEOUT_SEC))
    if not ok or not isinstance(payload, dict):
        return {"ok": False, "text": "", "reason": reason or "Рантайм не ответил",
                "model": state["model"]}
    message = payload.get("message") or {}
    answer = " ".join(str(message.get("content") or payload.get("response") or "").split())
    if not answer:
        return {"ok": False, "text": "", "reason": "Модель ответила пустотой",
                "model": state["model"]}
    return {"ok": True, "text": answer[:MAX_REPLY_CHARS], "reason": "",
            "model": state["model"]}


def numbers_checked(answer: str, source: str) -> dict[str, Any]:
    """Числа ответа против чисел источника.

    Сверка грубая и сознательно грубая: если модель назвала число, которого нет
    в найденном тексте, это не «ошибка модели», а повод показать человеку
    предупреждение и источник. Цифры в ответе остаются — вычеркнуть их из prose
    нельзя, а спрятать предупреждение значит соврать владельцу.
    """
    def norm(text: str) -> set[str]:
        found = set()
        for raw in _NUMBER_RE.findall(str(text or "")):
            digits = re.sub(r"[^\d]", "", raw)
            if digits:
                found.add(digits.lstrip("0") or "0")
        return found

    answer_numbers = norm(answer)
    source_numbers = norm(source)
    extra = sorted(answer_numbers - source_numbers)
    return {"ok": not extra, "numbers": sorted(answer_numbers),
            "unchecked": extra,
            "warning": ("Числа в ответе не найдены в источнике: "
                        + ", ".join(extra[:8]) if extra else "")}


def parse_json(text: str) -> dict[str, Any]:
    """JSON из ответа модели: первый объект или словарь, а не «что прислали»."""
    raw = str(text or "")
    start = min([index for index in (raw.find("{"), raw.find("[")) if index >= 0] or [-1])
    if start < 0:
        return {}
    try:
        value = json.loads(raw[start:])
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {"items": value}
