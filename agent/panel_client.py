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
from datetime import date, timedelta
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any

DEFAULT_URL = "http://127.0.0.1:8765"
# Loopback-запросы идут мимо системного прокси (как в `assistant._local_open` с 18.19):
# с HTTP_PROXY в окружении каждый пинг уходил в прокси и мог висеть до таймаута.
_LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

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
        with _LOCAL_OPENER.open(request, timeout=timeout) as answer:  # noqa: S310 — только loopback
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


def _request_bytes(url: str, timeout: float = TIMEOUT_SEC,
                   limit: int = 8 * 1024 * 1024) -> tuple[bool, bytes, str]:
    """Read a small binary payload from PrintFlow over loopback only."""
    local, why = loopback_ok(url)
    if not local:
        return False, b"", why
    request = urllib.request.Request(
        url, headers={"Accept": "image/jpeg", "User-Agent": "Luma-PanelClient/1"},
        method="GET",
    )
    try:
        with _LOCAL_OPENER.open(request, timeout=timeout) as answer:
            content_type = str(answer.headers.get("Content-Type") or "").casefold()
            raw = answer.read(int(limit) + 1)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read(4096).decode("utf-8", "replace")
            parsed = json.loads(detail or "{}")
            if isinstance(parsed, dict):
                detail = str(parsed.get("reason") or parsed.get("error") or "")
        except Exception:
            pass
        return False, b"", detail or f"панель ответила {exc.code}"
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, socket.timeout):
            return False, b"", f"панель не ответила за {timeout:.0f} с"
        return False, b"", f"панель недоступна ({reason})"
    except (OSError, ValueError) as exc:
        return False, b"", f"панель недоступна ({exc.__class__.__name__})"
    if len(raw) > int(limit):
        return False, b"", "кадр камеры слишком большой"
    if not raw:
        return False, b"", "камера вернула пустой кадр"
    if "image/jpeg" not in content_type and not raw.startswith(b"\xff\xd8\xff"):
        return False, b"", "панель вернула не JPEG"
    return True, raw, ""


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
        """Каталог Nozza для Luma: действия панели плюс v19 domain contracts."""
        state = self.status()
        if not state["alive"]:
            return {"ok": False, "actions": [], "domains": {}, "reason": state["reason"]}
        rows = state["actions"]
        domains: dict[str, list[str]] = {}
        for row in rows:
            domain = str(row.get("domain") or "global").strip() or "global"
            domains.setdefault(domain, []).append(str(row.get("id") or ""))
        return {"ok": True, "actions": rows, "domains": domains, "reason": "",
                "contract_version": 2,
                "count": len(rows),
                "confirmed": sum(1 for row in rows if row.get("confirm")),
                "reads": sum(1 for row in rows if not row.get("confirm"))}

    # --- исполнение -------------------------------------------------------
    def run_action(self, action: dict[str, Any], params: dict[str, Any],
                   confirmed: bool = False) -> dict[str, Any]:
        """Выполнить действие панели и детерминированно проверить readback.

        URL по-прежнему берётся только из серверного каталога. Структурные
        параметры разворачиваются лишь для двух исторических маршрутов, которые
        принимают объект верхнего уровня: order_save(draft) и settings_save(patch).
        """
        action_id = str(action.get("id") or "")
        method = str(action.get("method") or "GET").upper()
        path = str(action.get("path") or "")
        if not path.startswith("/api/"):
            return {"ok": False, "reason": f"Действие не содержит маршрута панели: «{path}»"}

        values = dict(params or {})
        query = {key: value for key, value in values.items()
                 if not isinstance(value, (dict, list))}
        body = {key: value for key, value in values.items()
                if isinstance(value, (dict, list))}
        if method == "POST":
            body.update(query)
            if action_id == "order_save" and isinstance(values.get("draft"), dict):
                body = dict(values["draft"])
            elif action_id == "settings_save" and isinstance(values.get("patch"), dict):
                body = dict(values["patch"])
            if action_id == "order_fulfill" and confirmed:
                body.setdefault("handoff_confirmed", True)
            # Физические маршруты используют confirmed; остальные его игнорируют.
            body.setdefault("confirmed", bool(confirmed))
            url = f"{self.url}{path}"
        else:
            url = f"{self.url}{path}"
            if query:
                url += "?" + urllib.parse.urlencode(query)

        ok, payload, reason = _request(url, payload=body if method == "POST" else None,
                                       timeout=self.timeout)
        if not ok:
            return {"ok": False, "reason": reason, "action": action_id or path}

        verification = self.verify_action(action, values,
                                          payload if isinstance(payload, dict) else {})
        return {"ok": True, "reason": "", "action": action_id or path,
                "method": method, "path": path, "confirmed": bool(confirmed),
                "result": payload, "verification": verification,
                "verified": bool(verification.get("verified")),
                "verification_state": str(verification.get("state") or "")}

    def verify_action(self, action: dict[str, Any], params: dict[str, Any],
                      result: dict[str, Any]) -> dict[str, Any]:
        """Попросить Nozza проверить действие по авторитетному состоянию PrintFlow."""
        action_id = str(action.get("id") or "").strip()
        ok, payload, reason = _request(
            f"{self.url}/api/assistant/verify",
            payload={"action": action_id, "params": dict(params or {}),
                     "result": dict(result or {})},
            timeout=min(self.timeout, 8.0),
        )
        if not ok or not isinstance(payload, dict):
            return {"ok": False, "verified": False, "state": "pending",
                    "action": action_id, "reason": reason or "readback недоступен",
                    "evidence": {}}
        return payload

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

    def sales_last7(self) -> dict[str, Any]:
        """Read the panel's sales ledger for the last seven calendar days."""
        start = (date.today() - timedelta(days=6)).isoformat()
        end = (date.today() + timedelta(days=1)).isoformat()
        ok, payload, reason = _request(
            f"{self.url}/api/report/sales?period=last7&limit=1",
            timeout=self.timeout,
        )
        if not ok or not isinstance(payload, dict):
            return {"ok": False, "reason": reason or "отчёт продаж недоступен"}
        if payload.get("start") == start and payload.get("end") == end:
            return {**payload, "ok": True}

        # Older running panels treat unknown periods as a month. Derive the
        # seven-day result from their existing ledger until they restart.
        rows: list[dict[str, Any]] = []
        for offset in (0, 1):
            ok, month, reason = _request(
                f"{self.url}/api/report/sales?period=month&offset={offset}&limit=10000",
                timeout=self.timeout,
            )
            if not ok or not isinstance(month, dict):
                return {"ok": False, "reason": reason or "отчёт продаж недоступен"}
            rows.extend(row for row in month.get("rows", [])
                        if isinstance(row, dict) and start <= str(row.get("at") or "")[:10] < end)
            if (date.today() - timedelta(days=6)).month == date.today().month:
                break
        products: dict[str, dict[str, Any]] = {}
        for row in rows:
            name = str(row.get("name") or "Товар")
            item = products.setdefault(name, {"name": name, "amount": 0.0})
            item["amount"] += float(row.get("amount") or 0)
        return {"ok": True, "count": len(rows),
                "total_qty": sum(float(row.get("qty") or 0) for row in rows),
                "total_amount": sum(float(row.get("amount") or 0) for row in rows),
                "total_profit": sum(float(row.get("profit") or 0) for row in rows),
                "products": sorted(products.values(), key=lambda item: -item["amount"])}

    def chat(self, text: str, session: str = "voice", source: str = "agent") -> dict[str, Any]:
        """Разговор с мозгом панели (18.21): вопрос цеха отвечает панель, не агент."""
        ok, payload, reason = _request(f"{self.url}/api/assistant/chat",
                                       payload={"text": str(text), "session": str(session),
                                                "source": str(source), "delegate": False,
                                                "contract_version": 1,
                                                "request_id": uuid.uuid4().hex},
                                       timeout=self.timeout)
        if not ok or not isinstance(payload, dict):
            return {"ok": False, "reason": reason or "панель не ответила"}
        return payload

    def context(self) -> dict[str, Any]:
        """Сводка цеха одной строкой (станки, долги, имя владельца) — без записи в диалог панели."""
        ok, payload, reason = _request(f"{self.url}/api/assistant/context", timeout=min(self.timeout, 4.0))
        if not ok or not isinstance(payload, dict):
            return {"ok": False, "reason": reason or "панель не ответила"}
        return payload

    def camera_frame(self, printer_id: str = "") -> dict[str, Any]:
        """Fresh JPEG from the selected/active PrintFlow printer camera."""
        pid = str(printer_id or "").strip()
        printer: dict[str, Any] = {}
        if not pid:
            context = self.context()
            if not context.get("ok"):
                return {"ok": False, "reason": context.get("reason") or "PrintFlow недоступен",
                        "printer_id": "", "image": b"", "mime": ""}
            rows = [row for row in list(context.get("printers") or []) if isinstance(row, dict)]
            active_states = {"RUNNING", "PRINTING", "PAUSED"}
            printer = next(
                (row for row in rows if str(row.get("state") or "").upper() in active_states),
                rows[0] if rows else {},
            )
            pid = str(printer.get("id") or "").strip()
            if not pid:
                return {"ok": False, "reason": "В PrintFlow нет настроенного принтера",
                        "printer_id": "", "image": b"", "mime": ""}
        query = urllib.parse.urlencode({"printer_id": pid})
        camera_url = f"{self.url}/api/printer/camera.jpg?{query}"
        ok, image, reason = _request_bytes(
            camera_url, timeout=min(self.timeout, 8.0),
        )
        return {
            "ok": bool(ok),
            "reason": "" if ok else (reason or "Кадр камеры недоступен"),
            "printer_id": pid,
            "printer_name": str(printer.get("name") or pid),
            "image": image if ok else b"",
            "mime": "image/jpeg" if ok else "",
            "url": camera_url,
        }

    def clear_dialog(self, session: str) -> dict[str, Any]:
        """Забыть контекст разговора агента в панели («его», «второй») — вместе с лентой окна."""
        ok, payload, reason = _request(f"{self.url}/api/assistant/dialog",
                                       payload={"op": "clear", "session": str(session)},
                                       timeout=min(self.timeout, 4.0))
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
