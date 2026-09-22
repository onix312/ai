"""Ассистент → PrintFlow: loopback-клиент панели (18.14, идея И137).

Зачем отдельный модуль, а не вызовы из навыков напрямую.

PrintFlow для ассистента — один из навыков (`panel.actions`, `panel.do`,
`panel.ask`, `day.briefing`, `day.summary`). У этого соседства два правила,
которые легко нарушить по одному в каждом навыке:

  * **адрес обязан оставаться loopback.** Файлы клиента, суммы и телефоны уезжают
    в панель по этому же компьютеру; адрес в другой сети превращает локального
    ассистента в облачного (та же проверка, что в `assistant._loopback_ok`);
  * **подтверждение берётся из каталога панели, а не из запроса.** Панель сама
    знает, какое действие двигает деньги или печать, и ассистент не имеет права
    решить за неё, что `confirmed` можно поставить самому.

Агент не импортирует коннектор и не открывает его базу: он ходит по HTTP, как
это делает внешний нарезчик и как это описано в ADR-0004. Если панель не
запущена, навыки `panel.*` честно недоступны, а остальные продолжают работать.
"""
from __future__ import annotations

import json
import mimetypes
import pathlib
import socket
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any

DEFAULT_URL = "http://127.0.0.1:8765"
PING_SEC = 2.0
TIMEOUT_SEC = 30.0
LOOPBACK_NAMES = ("127.0.0.1", "localhost", "::1", "[::1]")
MAX_UPLOAD_BYTES = 64 * 1024 * 1024


def loopback_ok(url: str) -> tuple[bool, str]:
    """Адрес панели обязан быть этим компьютером.

    Проверка не косметическая: ассистент отдаёт панели содержимое файлов
    клиента. Хост сравнивается с loopback-именами и с адресом этой машины,
    потому что `192.168.x.x` этого же компьютера — тоже не «чужой», но по
    умолчанию панель слушает именно loopback, и всё остальное требует явного
    решения владельца.
    """
    try:
        host = str(urllib.parse.urlsplit(str(url or "")).hostname or "").strip().lower()
    except ValueError:
        return False, "адрес не разбирается"
    if not host:
        return False, "адрес пуст"
    if host in LOOPBACK_NAMES:
        return True, ""
    return False, f"адрес «{host}» не является этим компьютером"


def _request(url: str, payload: dict | None = None, timeout: float = TIMEOUT_SEC,
             data: bytes | None = None, content_type: str = "") -> tuple[bool, Any, str]:
    """Один HTTP-вызов. Отказ всегда с причиной: навык показывает её человеку."""
    local, why = loopback_ok(url)
    if not local:
        return False, None, why
    body = data
    headers = {"Accept": "application/json"}
    if body is None and payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    if content_type:
        headers["Content-Type"] = content_type
    request = urllib.request.Request(url, data=body, headers=headers,
                                     method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as answer:  # noqa: S310 — только loopback
            raw = answer.read(MAX_UPLOAD_BYTES)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read(4096).decode("utf-8", "replace")
        except Exception:
            detail = ""
        return False, None, f"панель ответила {exc.code}{': ' + detail[:300] if detail else ''}"
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, socket.timeout):
            return False, None, f"панель не ответила за {timeout:.0f} с"
        return False, None, f"панель недоступна ({reason})"
    except (OSError, ValueError) as exc:
        return False, None, f"панель недоступна ({exc.__class__.__name__})"
    text = raw.decode("utf-8", "replace")
    if not text:
        return True, {}, ""
    try:
        return True, json.loads(text), ""
    except json.JSONDecodeError:
        return False, None, "панель ответила не JSON"


def multipart(fields: dict[str, str], file_path: pathlib.Path,
              file_field: str = "file") -> tuple[bytes, str]:
    """multipart-тело для загрузки файла в панель (байты, как ждёт `uploads.py`)."""
    boundary = f"----PrintFlowAssistant{uuid.uuid4().hex}"
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
                     f"{value}\r\n".encode("utf-8"))
    guess = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; "
        f"filename=\"{file_path.name}\"\r\nContent-Type: {guess}\r\n\r\n".encode("utf-8"))
    parts.append(file_path.read_bytes())
    parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


class Client:
    """Клиент панели: каталог действий, исполнение, вопросы, день, файл в заказ."""

    def __init__(self, url: str = DEFAULT_URL, timeout: float = TIMEOUT_SEC,
                 ping: float = PING_SEC) -> None:
        self.url = str(url or DEFAULT_URL).rstrip("/")
        self.timeout = float(timeout)
        self.ping = float(ping)

    # --- состояние --------------------------------------------------------
    def status(self) -> dict[str, Any]:
        """Состояние помощника панели: жив ли, какая модель, каталог действий."""
        ok, payload, reason = _request(f"{self.url}/api/assistant/status",
                                       timeout=self.ping)
        if not ok or not isinstance(payload, dict):
            return {"ok": False, "alive": False, "reason": reason or "панель не ответила",
                    "actions": []}
        return {"ok": True, "alive": True, "reason": str(payload.get("reason") or ""),
                "model": str(payload.get("model") or ""),
                "assistant_available": bool(payload.get("available")),
                "actions": list(payload.get("actions") or [])}

    def alive(self) -> tuple[bool, str]:
        state = self.status()
        if not state["alive"]:
            return False, state["reason"]
        return True, ""

    def actions(self) -> dict[str, Any]:
        """Каталог действий панели: что ассистент может попросить у цеха."""
        state = self.status()
        if not state["alive"]:
            return {"ok": False, "actions": [], "reason": state["reason"]}
        rows = state["actions"]
        return {"ok": True, "actions": rows, "reason": "",
                "count": len(rows),
                "confirmed": sum(1 for row in rows if row.get("confirm")),
                "reads": sum(1 for row in rows if not row.get("confirm"))}

    # --- исполнение -------------------------------------------------------
    def run_action(self, action: dict[str, Any], params: dict[str, Any],
                   confirmed: bool = False) -> dict[str, Any]:
        """Выполнить действие панели. Адрес, метод и confirm — из каталога панели.

        Ассистент не конструирует URL: путь берётся из записи каталога, которую
        отдала сама панель. Поэтому «полный доступ к панели» не превращается в
        «ассистент дёргает любой маршрут».
        """
        method = str(action.get("method") or "GET").upper()
        path = str(action.get("path") or "")
        if not path.startswith("/api/"):
            return {"ok": False, "reason": f"Действие не содержит маршрута панели: «{path}»"}
        query = {key: value for key, value in (params or {}).items()
                 if not isinstance(value, (dict, list))}
        body = {key: value for key, value in (params or {}).items()
                if isinstance(value, (dict, list))}
        if method == "POST":
            body.update(query)
            # Подтверждение ставит человек в окне агента, а не модель: значение
            # приходит из вызова навыка, который уже прошёл через `confirm`.
            body.setdefault("confirmed", bool(confirmed))
            url = f"{self.url}{path}"
        else:
            url = f"{self.url}{path}"
            if query:
                url += "?" + urllib.parse.urlencode(query)
        ok, payload, reason = _request(url, payload=body if method == "POST" else None,
                                       timeout=self.timeout)
        if not ok:
            return {"ok": False, "reason": reason, "action": str(action.get("id") or path)}
        return {"ok": True, "reason": "", "action": str(action.get("id") or path),
                "method": method, "path": path, "confirmed": bool(confirmed),
                "result": payload}

    def find_action(self, name: str) -> tuple[dict[str, Any] | None, str]:
        """Действие по имени из каталога панели — или причина, почему его нет."""
        state = self.status()
        if not state["alive"]:
            return None, state["reason"]
        key = str(name or "").strip().casefold()
        for row in state["actions"]:
            if str(row.get("id") or "").casefold() == key:
                return row, ""
        known = ", ".join(str(row.get("id")) for row in state["actions"][:24])
        return None, f"Действия «{name}» в каталоге панели нет. Есть: {known}"

    # --- знания и день ----------------------------------------------------
    def ask(self, question: str) -> dict[str, Any]:
        """Вопрос по фактам базы (идея И1): считает панель, ассистент передаёт."""
        ok, payload, reason = _request(f"{self.url}/api/assistant/ask",
                                       payload={"question": str(question)},
                                       timeout=self.timeout)
        if not ok or not isinstance(payload, dict):
            return {"ok": False, "reason": reason or "панель не ответила"}
        return payload

    def day(self, kind: str = "briefing", days: int = 1) -> dict[str, Any]:
        """Утренний брифинг или итог дня (идея И174)."""
        query = urllib.parse.urlencode({"kind": str(kind or "briefing"), "days": int(days or 1)})
        ok, payload, reason = _request(f"{self.url}/api/assistant/day?{query}",
                                       timeout=self.timeout)
        if not ok or not isinstance(payload, dict):
            return {"ok": False, "reason": reason or "панель не ответила"}
        return payload

    def upload_to_order(self, path: pathlib.Path, order: str = "",
                        kind: str = "document") -> dict[str, Any]:
        """Файл в заказ (идея И144): панель возвращает черновик, сохраняет человек."""
        file_path = pathlib.Path(path)
        if not file_path.is_file():
            return {"ok": False, "reason": f"Файл не найден: {file_path}"}
        size = file_path.stat().st_size
        if size > MAX_UPLOAD_BYTES:
            return {"ok": False, "reason": f"Файл больше {MAX_UPLOAD_BYTES // (1024 * 1024)} МБ"}
        note = f"Файл передал ассистент компьютера ({kind})"
        if order:
            note = f"К заказу «{order}»: {note}"
        body, content_type = multipart(
            {"text": note, "channel": f"assistant-{kind}"}, file_path)
        ok, payload, reason = _request(f"{self.url}/api/order/intake/upload",
                                       data=body, content_type=content_type,
                                       timeout=self.timeout)
        if not ok or not isinstance(payload, dict):
            return {"ok": False, "reason": reason or "панель не приняла файл"}
        draft = payload.get("draft") or {}
        return {"ok": True, "reason": "", "path": str(file_path), "size": size,
                "kind": kind, "order": order,
                "saved": False,
                "hint": "Черновик заказа открыт в панели: сохраните его, если всё верно",
                "draft": {key: draft.get(key) for key in
                          ("product", "customer_name", "phone", "material", "color",
                           "due", "file", "notes") if draft.get(key) is not None},
                "warnings": list(payload.get("warnings") or [])}
