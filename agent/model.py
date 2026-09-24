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

18.21 — модель стала «мозгом», а не одним окном ввода:
  * `chat()` — настоящий диалог: системная роль, история, строки ответа не
    склеиваются, режим `format="json"` для планировщика (Ollama гарантирует
    синтаксис JSON, а `parse_json` достаёт объект даже из ответа с пояснением);
  * `resolve_name()` — имя модели берётся из окружения, затем из настроек
    панели (одна модель на компьютер), затем из того, что отдаёт рантайм: раньше
    без `PRINTFLOW_MODEL_NAME` агент был «без мозга» при живой Ollama;
  * `vision_ok()` + картинки в `chat()` — описание экрана только моделью,
    которая видит изображения; текстовая модель больше не «описывает» экран,
    которого не видела.
"""
from __future__ import annotations

import base64
import json
import re
import socket
import threading
import time
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


# Имя модели, найденное без переменной окружения, живёт минуту: панель могла
# сменить модель в настройках, а спрашивать её на каждый навык — лишняя сеть.
_NAME_CACHE: dict[str, Any] = {"at": 0.0, "name": "", "source": ""}
_NAME_LOCK = threading.Lock()
NAME_TTL_SEC = 60.0
# Семейства, которые разумно взять «по умолчанию», если владелец ничего не
# выбрал: небольшие инструкционные модели с русским языком.
_PREFERRED = ("qwen3", "qwen2.5", "qwen", "gemma3", "llama3.2", "mistral", "phi4")
# Семейства, которые видят картинки (на случай старой Ollama без `capabilities`).
_VISION_HINTS = ("llava", "bakllava", "moondream", "minicpm-v", "vision", "qwen2.5vl",
                 "qwen2-vl", "qwen2.5-vl", "gemma3", "granite3.2-vision", "mistral-small3.1")


def pick_default(models: list[str]) -> str:
    """Модель по умолчанию из списка рантайма. Чистая функция.

    Эмбеддинги и «видящие» модели не берём: первые не умеют отвечать, вторые
    тяжелее и нужны только для описания экрана.
    """
    usable = [name for name in models
              if name and "embed" not in name.casefold()
              and not any(hint in name.casefold() for hint in ("llava", "moondream", "minicpm-v"))]
    for family in _PREFERRED:
        for name in usable:
            if name.casefold().startswith(family):
                return name
    return usable[0] if usable else ""


def resolve_name(url: str = "", models: list[str] | None = None) -> tuple[str, str]:
    """Имя модели и откуда оно: `env`, `panel`, `runtime` или пусто.

    Порядок важен: явная настройка владельца агента, затем настройка панели
    (модель одна на компьютер — её уже выбрали в PrintFlow), затем разумный
    выбор из того, что установлено. Так «мозг» работает из коробки.
    """
    if config.MODEL_NAME:
        return config.MODEL_NAME, "env"
    with _NAME_LOCK:
        if _NAME_CACHE["name"] and time.time() - float(_NAME_CACHE["at"]) < NAME_TTL_SEC:
            return str(_NAME_CACHE["name"]), str(_NAME_CACHE["source"])
    name, source = "", ""
    try:
        from .panel_client import Client

        state = Client(config.PRINTFLOW_URL, ping=1.0).status()
        chosen = str(state.get("model") or "").strip() if state.get("alive") else ""
        if chosen and (not models or chosen in models or f"{chosen}:latest" in models):
            name, source = chosen, "panel"
    except Exception:
        name = ""
    if not name and models:
        name = pick_default(models)
        source = "runtime" if name else ""
    if name:
        with _NAME_LOCK:
            _NAME_CACHE.update(at=time.time(), name=name, source=source)
    return name, source


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
        name, source = resolve_name(url, models)
        out["model"] = name
        out["model_source"] = source
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


_THINK_RE = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)


def visible_text(text: str) -> str:
    """Ответ без рассуждений модели (`<think>…</think>`) и пустых строк."""
    cleaned = _THINK_RE.sub("", str(text or ""))
    cleaned = re.sub(r"</?think>", "", cleaned, flags=re.IGNORECASE)
    lines = [" ".join(line.split()) for line in cleaned.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def chat(messages: list[dict[str, Any]], *, system: str = "", fmt: str | dict | None = None,
         url: str = "", name: str = "", timeout: float | None = None,
         temperature: float = 0.2, images: list[bytes] | None = None,
         max_chars: int = MAX_REPLY_CHARS) -> dict[str, Any]:
    """Диалог с моделью: системная роль, история, JSON-режим, картинки.

    `messages` — список `{role, content}` (роли user/assistant); `system`
    ставится первым сообщением. `fmt="json"` включает режим Ollama, в котором
    ответ обязан быть JSON — планировщик не разбирает прозу регулярками.
    `images` прикладываются к последнему сообщению человека (base64), и только
    для модели, которая их видит (`vision_ok`).
    """
    state = status(url, name)
    if not state["ok"]:
        return {"ok": False, "text": "", "reason": state["reason"], "model": state["model"]}
    clean: list[dict[str, Any]] = []
    if system.strip():
        clean.append({"role": "system", "content": system[:MAX_PROMPT_CHARS]})
    budget = MAX_PROMPT_CHARS
    for turn in messages or []:
        if not isinstance(turn, dict):
            continue
        role = str(turn.get("role") or "")
        content = str(turn.get("content") or "").strip()
        if role not in ("user", "assistant", "tool") or not content:
            continue
        clean.append({"role": role, "content": content[:budget]})
        budget = max(400, budget - len(content))
    if not any(turn["role"] == "user" for turn in clean):
        return {"ok": False, "text": "", "reason": "Пустой запрос к модели", "model": state["model"]}
    if images:
        for turn in reversed(clean):
            if turn["role"] == "user":
                turn["images"] = [base64.b64encode(bytes(image)).decode("ascii")
                                  for image in images[:2] if image]
                break
    body: dict[str, Any] = {"model": state["model"], "stream": False, "messages": clean,
                            "options": {"temperature": float(temperature)}}
    if fmt:
        body["format"] = fmt
    ok, payload, reason = _post(
        f"{state['url']}/api/chat", body,
        float(timeout if timeout is not None else config.MODEL_TIMEOUT_SEC))
    if not ok or not isinstance(payload, dict):
        return {"ok": False, "text": "", "reason": reason or "Рантайм не ответил",
                "model": state["model"]}
    message = payload.get("message") or {}
    answer = visible_text(str(message.get("content") or payload.get("response") or ""))
    if not answer:
        return {"ok": False, "text": "", "reason": "Модель ответила пустотой",
                "model": state["model"]}
    return {"ok": True, "text": answer[:max_chars], "reason": "", "model": state["model"]}


def complete(prompt: str, url: str = "", name: str = "",
             timeout: float | None = None) -> dict[str, Any]:
    """Один запрос к модели одной строкой. Пустой ответ и мусор — отказ с причиной."""
    text = str(prompt or "")[:MAX_PROMPT_CHARS]
    if not text.strip():
        state = status(url, name)
        if not state["ok"]:
            return {"ok": False, "text": "", "reason": state["reason"], "model": state["model"]}
        return {"ok": False, "text": "", "reason": "Пустой запрос к модели",
                "model": state["model"]}
    reply = chat([{"role": "user", "content": text}], url=url, name=name,
                 timeout=timeout, temperature=0.0)
    if reply["ok"]:
        reply["text"] = " ".join(reply["text"].split())[:MAX_REPLY_CHARS]
    return reply


def vision_ok(url: str = "", name: str = "") -> tuple[bool, str]:
    """Видит ли модель картинки. Сначала спрашиваем рантайм, затем — по семейству."""
    state = status(url, name)
    if not state["ok"]:
        return False, state["reason"]
    ok, payload, _reason = _post(f"{state['url']}/api/show", {"model": state["model"]}, PING_SEC * 2)
    if ok and isinstance(payload, dict) and isinstance(payload.get("capabilities"), list):
        if "vision" in [str(item).casefold() for item in payload["capabilities"]]:
            return True, ""
        return False, (f"Модель «{state['model']}» не видит изображения. Для описания экрана "
                       "поставьте видящую модель, например `ollama pull qwen2.5vl:3b` или `gemma3:4b`")
    lowered = state["model"].casefold()
    if any(hint in lowered for hint in _VISION_HINTS):
        return True, ""
    return False, (f"Модель «{state['model']}» не видит изображения (по названию). "
                   "Для описания экрана нужна видящая модель: qwen2.5vl, gemma3, llava")


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


def _balanced(raw: str, start: int) -> str:
    """Фрагмент от открывающей скобки до парной ей, со строками и экранированием."""
    opener = raw[start]
    closer = "}" if opener == "{" else "]"
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(raw)):
        char = raw[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return raw[start:index + 1]
    return ""


def parse_json(text: str) -> dict[str, Any]:
    """JSON из ответа модели: первый целый объект или список, а не «что прислали».

    Модель любит обрамлять JSON пояснением и блоком ```json. Раньше разбор брал
    всё от первой скобки до конца, и хвост «Надеюсь, помог!» ломал ответ целиком.
    Теперь берётся ровно первый сбалансированный объект.
    """
    raw = visible_text(str(text or "")) if "<think" in str(text or "").lower() else str(text or "")
    for start, char in enumerate(raw):
        if char not in "{[":
            continue
        chunk = _balanced(raw, start)
        if not chunk:
            continue
        try:
            value = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        return value if isinstance(value, dict) else {"items": value}
    return {}
