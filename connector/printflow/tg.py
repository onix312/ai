"""Общий транспорт Telegram (идеи 73, 74, 76).

Было. `telegram_bot.py` (3024 строки) и `client_bot.py` (2948 строк) содержали
14 одноимённых приватных методов: `_call`, `_loop`, `_claim_update`,
`_finish_update`, `_reply`, `_edit_or_reply`, `_download_file`,
`_attach_photo`, `_dispatch`, `_handle`, `_handle_callback`, `_settings`,
`_catalog_rows`, `__init__`. Две независимые реализации одного и того же
long polling. При этом надёжная очередь отправки (outbox) была только у
клиентского бота: внутренний терял уведомления при обрыве сети.

Стало. Транспорт, журнал обновлений и outbox живут здесь, а боты остались
наборами команд и текстов. Обе прежние реализации переведены на эти классы,
их публичное поведение не изменилось (тесты ботов не правились).

Что внутри:
  * `Transport` — HTTP к api.telegram.org: вызов метода, long polling,
    скачивание файла. Токен берётся колбэком, поэтому один класс работает
    и с ботом сотрудников, и с покупательским;
  * `UpdateLedger` — «занять/завершить» update в SQLite. Защищает от двойной
    обработки при двух запущенных копиях и от потери update при падении:
    зависшая обработка старше `stale_after` перехватывается;
  * `Outbox` — постоянная очередь исходящих. Сначала запись в базу, потом
    HTTP: если процесс умер между шагами, доставка повторится. Ключ
    дедупликации не даёт отправить одно уведомление дважды;
  * `parse_command` — общая грамматика команд (идея 76): «пауза P1»,
    «стоп», «готов 1043» разбираются одним парсером для обоих ботов.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

API = "https://api.telegram.org/bot{token}/{method}"
STALE_AFTER_SECONDS = 300
MAX_TEXT = 3800


# ================================================================== транспорт
class Transport:
    """Минимальный клиент Bot API. Без внешних зависимостей.

    Ответ Telegram различается (Н12):
      * 429 — нас троттлят, в `parameters.retry_after` сказано сколько ждать;
      * 5xx — Telegram лежит, запрос имеет смысл повторить;
      * 401/403 — токен отозван или бот не запущен: повторять бесполезно,
        бот обязан остановиться и сказать об этом владельцу, а не молча
        крутить polling часами;
      * сетевая ошибка — отдельный счётчик, это не вина Telegram.

    Раньше всё это схлопывалось в `{}`, и «умерший токен» выглядел как
    «Telegram не отвечает»: бот работал, сообщений не было, причин не было.
    """

    # После стольких ошибок авторизации подряд считаем токен мёртвым.
    AUTH_FAIL_LIMIT = 3

    def __init__(self, token_provider: Callable[[], str], name: str = "bot") -> None:
        self.token_provider = token_provider
        self.name = name
        self.last_call = 0.0
        self.errors = 0
        self.calls = 0
        self.throttled = 0          # сколько раз получили 429
        self.server_errors = 0      # 5xx
        self.auth_failures = 0      # 401/403 подряд
        self.network_errors = 0
        self.throttle_until = 0.0   # время, до которого Telegram просил подождать
        self.fatal = ""             # непустая строка = боту нечего делать дальше

    @property
    def token(self) -> str:
        return str(self.token_provider() or "")

    def call(self, method: str, params: dict | None = None, timeout: int = 35) -> dict:
        """Вызвать метод API. Ошибку не бросаем: бот обязан выжить."""
        token = self.token
        if not token:
            return {}
        url = API.format(token=token, method=method)
        data = urllib.parse.urlencode(params or {}).encode()
        self.calls += 1
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data),
                                        timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8", "ignore"))
                self.last_call = time.time()
                self.auth_failures = 0
                return payload
        except urllib.error.HTTPError as exc:
            self.errors += 1
            return self._classify_http_error(exc, method)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            self.errors += 1
            self.network_errors += 1
            return {"ok": False, "error_code": 0, "network_error": str(exc)}
        except Exception as exc:
            self.errors += 1
            return {"ok": False, "error_code": 0, "error": str(exc)}

    def _classify_http_error(self, exc: "urllib.error.HTTPError", method: str) -> dict:
        """Разобрать ответ Telegram с ошибкой: троттлинг, сбой или мёртвый токен."""
        code = int(getattr(exc, "code", 0) or 0)
        body = {}
        try:
            raw = exc.read().decode("utf-8", "ignore")
            body = json.loads(raw) if raw.strip() else {}
        except Exception:
            body = {}
        description = str(body.get("description") or getattr(exc, "reason", "") or "")
        if code == 429:
            retry_after = int(((body.get("parameters") or {}).get("retry_after")) or 3)
            self.throttled += 1
            self.throttle_until = time.time() + max(1, retry_after)
            return {"ok": False, "error_code": 429, "retry_after": retry_after,
                    "description": description}
        if 500 <= code < 600:
            self.server_errors += 1
            return {"ok": False, "error_code": code, "retryable": True,
                    "description": description}
        if code in (401, 403, 404):
            self.auth_failures += 1
            if self.auth_failures >= self.AUTH_FAIL_LIMIT and not self.fatal:
                self.fatal = (f"Telegram отклоняет токен бота «{self.name}» "
                              f"(HTTP {code}): {description or 'нет описания'}")
            return {"ok": False, "error_code": code, "retryable": False,
                    "description": description}
        return {"ok": False, "error_code": code, "description": description}

    def wait_if_throttled(self, cap: float = 30.0) -> float:
        """Подождать, если Telegram просил паузу. Возвращает сколько проспали."""
        delay = self.throttle_until - time.time()
        if delay <= 0:
            return 0.0
        delay = min(delay, cap)
        time.sleep(delay)
        return delay

    def healthy(self) -> bool:
        """Можно ли продолжать опрос: токен жив и нет фатальной причины."""
        return not self.fatal

    def get_updates(self, offset: int, timeout: int = 25,
                    allowed: tuple[str, ...] = ("message", "callback_query")) -> dict:
        return self.call("getUpdates", {
            "offset": offset, "timeout": timeout,
            "allowed_updates": json.dumps(list(allowed)),
        })

    def send_message(self, chat: Any, text: str, buttons: dict | None = None,
                     timeout: int = 15) -> dict:
        params: dict = {"chat_id": chat, "text": str(text)[:MAX_TEXT],
                        "disable_web_page_preview": "true"}
        if buttons:
            params["reply_markup"] = json.dumps(buttons, ensure_ascii=False)
        return self.call("sendMessage", params, timeout=timeout)

    def edit_message(self, chat: Any, message_id: Any, text: str,
                     buttons: dict | None = None) -> dict:
        params: dict = {"chat_id": chat, "message_id": message_id,
                        "text": str(text)[:MAX_TEXT],
                        "disable_web_page_preview": "true"}
        if buttons:
            params["reply_markup"] = json.dumps(buttons, ensure_ascii=False)
        return self.call("editMessageText", params, timeout=15)

    def answer_callback(self, callback_id: str, text: str = "") -> dict:
        return self.call("answerCallbackQuery",
                         {"callback_query_id": callback_id, "text": text[:200]},
                         timeout=15)

    def download_file(self, file_id: str, timeout: int = 60,
                      call: Callable[..., dict] | None = None) -> bytes | None:
        """Скачать вложение по file_id. Пусто, если не получилось.

        `call` — подменяемый вызов Bot API: боты передают свой `_call`,
        чтобы тесты без сети ловили getFile так же, как sendMessage.
        """
        if not file_id:
            return None
        invoke = call or self.call
        meta = invoke("getFile", {"file_id": file_id}, timeout=20)
        path = str(((meta or {}).get("result") or {}).get("file_path") or "")
        if not path:
            return None
        url = f"https://api.telegram.org/file/bot{self.token}/{path}"
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                return response.read()
        except Exception:
            return None

    def stats(self) -> dict:
        return {"name": self.name, "calls": self.calls, "errors": self.errors,
                "last_call": self.last_call, "throttled": self.throttled,
                "server_errors": self.server_errors,
                "network_errors": self.network_errors,
                "auth_failures": self.auth_failures, "fatal": self.fatal}


# ============================================================= журнал update
class UpdateLedger:
    """Занять и завершить update в таблице журнала.

    Возврат `claim`:
      * `True`  — update наш, обрабатываем;
      * `False` — его обрабатывает кто-то другой прямо сейчас;
      * `None`  — уже обработан, пропускаем.
    """

    def __init__(self, db, table: str, stale_after: int = STALE_AFTER_SECONDS) -> None:
        self.db = db
        self.table = table
        self.stale_after = stale_after

    def claim(self, update: dict) -> bool | None:
        from .config import now_iso
        update_id = str(update.get("update_id") or "").strip()
        if not update_id:
            return True
        cursor = self.db.execute(
            f"INSERT OR IGNORE INTO {self.table}(update_id,state,received_at)"
            " VALUES(?,'processing',?)", (update_id, now_iso()))
        if cursor.rowcount:
            return True
        existing = self.db.one(
            f"SELECT state,received_at FROM {self.table} WHERE update_id=?",
            (update_id,)) or {}
        state = str(existing.get("state") or "")
        if state == "done":
            return None
        stale = state == "failed"
        if state == "processing":
            stale = self._is_stale(existing.get("received_at"))
        if not stale:
            return False
        stamp = now_iso()
        if state == "processing":
            cursor = self.db.execute(
                f"UPDATE {self.table} SET state='processing',received_at=?,error=''"
                " WHERE update_id=? AND state='processing' AND received_at=?",
                (stamp, update_id, existing.get("received_at")))
        else:
            cursor = self.db.execute(
                f"UPDATE {self.table} SET state='processing',received_at=?,error=''"
                " WHERE update_id=? AND state='failed'", (stamp, update_id))
        return bool(cursor.rowcount)

    def _is_stale(self, received_at: Any) -> bool:
        try:
            received = datetime.fromisoformat(str(received_at or ""))
            if received.tzinfo is None:
                received = received.astimezone()
            age = (datetime.now().astimezone() - received).total_seconds()
            return age >= self.stale_after
        except (TypeError, ValueError, OverflowError):
            return False

    def finish(self, update_id: str, ok: bool = True, error: str = "") -> None:
        from .config import now_iso
        if not update_id:
            return
        self.db.execute(
            f"UPDATE {self.table} SET state=?,processed_at=?,error=? WHERE update_id=?",
            ("done" if ok else "failed", now_iso(), str(error or "")[:500], update_id))

    def reset_stuck(self) -> int:
        """Вернуть в очередь зависшие 'sending'/'processing' (после рестарта)."""
        cursor = self.db.execute(
            f"UPDATE {self.table} SET state='failed'"
            " WHERE state='processing' AND datetime(replace(received_at,'T',' '))"
            " <= datetime('now','-300 seconds')")
        return int(cursor.rowcount or 0)


# ==================================================================== outbox
class Outbox:
    """Постоянная очередь исходящих сообщений (идея 74).

    Порядок принципиален: сначала запись в SQLite, потом HTTP. Тогда падение
    процесса между шагами не теряет сообщение, а `dedupe_key` не даёт
    отправить одно и то же уведомление дважды.
    """

    def __init__(self, db, sender: Callable[..., dict],
                 table: str = "client_bot_outbox",
                 photo_sender: Callable[[dict, bytes], dict] | None = None,
                 token_provider: Callable[[], str] | None = None) -> None:
        """`sender(method, payload, timeout=…)` — вызов Bot API.

        Передаётся колбэком, а не объектом транспорта: боты подменяют
        `_call` в тестах, и очередь обязана ходить через ту же подмену.
        """
        self.db = db
        self.sender = sender
        self.photo_sender = photo_sender
        self.token_provider = token_provider or (lambda: "")
        self.table = table
        self.sent = 0
        self.retries = 0

    def add(self, chat: Any, method: str, payload: dict, dedupe_key: str = "",
            file_path: str = "") -> dict | None:
        from .accounting import uid
        from .config import now_iso
        key = str(dedupe_key or "").strip()[:240]
        if key:
            existing = self.db.one(
                f"SELECT * FROM {self.table} WHERE dedupe_key=?", (key,))
            if existing:
                return existing
        stamp = now_iso()
        ident = uid("out")
        # Пустой ключ пишем как NULL: UNIQUE допускает много NULL, но не
        # несколько пустых строк — иначе обычные сообщения начали бы теряться.
        self.db.execute(
            f"INSERT OR IGNORE INTO {self.table}"
            "(id,dedupe_key,chat_id,method,payload,file_path,state,attempts,available_at,created_at)"
            " VALUES(?,?,?,?,?,?,'pending',0,?,?)",
            (ident, key or None, str(chat), method,
             json.dumps(payload, ensure_ascii=False), str(file_path or ""), stamp, stamp))
        return (self.db.one(f"SELECT * FROM {self.table} WHERE id=?", (ident,))
                or (self.db.one(f"SELECT * FROM {self.table} WHERE dedupe_key=?", (key,))
                    if key else None))

    def send(self, row: dict) -> bool:
        """Доставить одну запись. `True` — доставлено (или уже было)."""
        from .config import now_iso
        try:
            payload = json.loads(row.get("payload") or "{}")
        except (TypeError, ValueError):
            payload = {}
        claim = self.db.execute(
            f"UPDATE {self.table} SET state='sending',available_at=?"
            " WHERE id=? AND state='pending'", (now_iso(), row.get("id")))
        if not claim.rowcount:
            current = self.db.one(f"SELECT state FROM {self.table} WHERE id=?",
                                  (row.get("id"),)) or {}
            return current.get("state") == "sent"
        if row.get("method") == "sendPhoto":
            result = self._send_photo(payload, str(row.get("file_path") or ""))
        else:
            result = self.sender(str(row.get("method") or "sendMessage"),
                                 payload, timeout=15)
        if result.get("ok"):
            self.db.execute(
                f"UPDATE {self.table} SET state='sent',sent_at=?,last_error='' WHERE id=?",
                (now_iso(), row["id"]))
            self.sent += 1
            self._cleanup_file(str(row.get("file_path") or ""))
            return True
        attempts = int(row.get("attempts") or 0) + 1
        self.retries += 1
        delay = min(300, 2 ** min(attempts, 8))
        available = datetime.fromtimestamp(time.time() + delay).astimezone().isoformat()
        self.db.execute(
            f"UPDATE {self.table} SET state='pending',attempts=?,available_at=?,"
            " last_error=? WHERE id=?",
            (attempts, available, "Telegram не подтвердил доставку", row["id"]))
        return False

    def _send_photo(self, payload: dict, file_path: str) -> dict:
        raw = b""
        if file_path:
            try:
                raw = Path(file_path).read_bytes()
            except OSError:
                raw = b""
        if not raw:
            return self.sender("sendPhoto", payload, timeout=20)
        if self.photo_sender is not None:
            return self.photo_sender(payload, raw)
        return self._multipart_photo(payload, raw)

    def _multipart_photo(self, payload: dict, raw: bytes) -> dict:
        """sendPhoto с файлом: multipart/form-data, только stdlib."""
        import mimetypes
        import uuid
        boundary = uuid.uuid4().hex
        parts: list[bytes] = []
        for key, value in payload.items():
            if key == "photo":
                continue
            parts.append(f"--{boundary}\r\n".encode())
            parts.append(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode())
            parts.append(f"{value}\r\n".encode())
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(b'Content-Disposition: form-data; name="photo"; filename="photo.jpg"\r\n')
        parts.append(b"Content-Type: " +
                     (mimetypes.guess_type("photo.jpg")[0] or "image/jpeg").encode() + b"\r\n\r\n")
        parts.append(raw)
        parts.append(f"\r\n--{boundary}--\r\n".encode())
        body = b"".join(parts)
        url = API.format(token=self.token_provider(), method="sendPhoto")
        request = urllib.request.Request(url, data=body, headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode("utf-8", "ignore"))
        except Exception:
            return {}

    @staticmethod
    def _cleanup_file(file_path: str) -> None:
        if not file_path:
            return
        try:
            Path(file_path).unlink(missing_ok=True)
        except OSError:
            pass

    def drain(self, batch: int = 30) -> int:
        """Доставить готовые к отправке записи. Возвращает число доставленных."""
        self.db.execute(
            f"UPDATE {self.table} SET state='pending'"
            " WHERE state='sending' AND datetime(replace(available_at,'T',' '))"
            " <= datetime('now','-300 seconds')")
        rows = self.db.query(
            f"SELECT * FROM {self.table} WHERE state='pending'"
            " AND (available_at IS NULL OR available_at='' OR"
            " datetime(replace(available_at,'T',' '))<=datetime('now'))"
            " ORDER BY datetime(replace(created_at,'T',' ')), id LIMIT ?",
            (max(1, int(batch)),))
        delivered = 0
        for row in rows:
            if self.send(row):
                delivered += 1
        return delivered

    def stats(self) -> dict:
        pending = self.db.one(
            f"SELECT COUNT(*) AS n FROM {self.table} WHERE state!='sent'") or {}
        return {"sent": self.sent, "retries": self.retries,
                "pending": int(pending.get("n") or 0)}


def check_token(token: str, timeout: int = 10) -> dict:
    """Проверить токен бота через getMe (Н13).

    Возвращает `{"ok": bool, "username": str, "error": str}`. Пустой токен —
    это «бот выключен», а не ошибка: проверяем только непустые значения.
    """
    token = str(token or "").strip()
    if not token:
        return {"ok": True, "skipped": True, "username": "", "error": ""}
    transport = Transport(lambda: token, "probe")
    answer = transport.call("getMe", {}, timeout=timeout)
    result = (answer or {}).get("result") or {}
    if answer.get("ok") and result:
        return {"ok": True, "username": str(result.get("username") or ""),
                "first_name": str(result.get("first_name") or ""),
                "error": ""}
    code = int(answer.get("error_code") or 0)
    if code in (401, 403, 404):
        return {"ok": False, "username": "",
                "error": "Telegram не принял токен: проверьте, что он скопирован"
                         " целиком и бот не удалён"}
    if code == 429:
        return {"ok": False, "username": "",
                "error": "Telegram ограничил частоту запросов — повторите позже"}
    return {"ok": False, "username": "",
            "error": "Не удалось связаться с Telegram (сеть или сбой на их стороне)"}


# =================================================================== команды
# Общая грамматика команд (идея 76). Оба бота понимали свой набор фраз,
# написанный вручную в двух диспетчерах; теперь разбор один.
COMMAND_ALIASES: dict[str, tuple[str, ...]] = {
    "pause": ("пауза", "pause", "стоп печать", "приостанови", "паузу"),
    "resume": ("продолжи", "resume", "возобнови", "дальше"),
    "stop": ("стоп", "stop", "останови", "отмени печать"),
    "status": ("статус", "status", "состояние", "что с принтером"),
    "queue": ("очередь", "queue", "план"),
    "ready": ("готов", "ready", "выдано", "отдал"),
    "help": ("помощь", "help", "команды", "start"),
    "money": ("деньги", "money", "касса", "итоги"),
    "filament": ("пластик", "filament", "ams", "катушка"),
    "doctor": ("доктор", "doctor", "диагностика", "health"),
}

_COMMAND_RE = re.compile(r"^\s*([а-яa-z0-9_\-/]+)\s*(.*)$", re.IGNORECASE | re.DOTALL)


def parse_command(text: str) -> dict:
    """Разобрать свободную фразу в `{action, args, raw}`.

    `action` пустой, если фраза не похожа на команду — тогда вызывающий код
    обрабатывает её как обычный текст (заявка, вопрос, ответ оператора).
    """
    raw = str(text or "").strip()
    if not raw:
        return {"action": "", "args": "", "raw": ""}
    match = _COMMAND_RE.match(raw)
    if not match:
        return {"action": "", "args": "", "raw": raw}
    head = match.group(1).lower().lstrip("/")
    tail = match.group(2).strip()
    for action, words in COMMAND_ALIASES.items():
        if head in words or head == action:
            return {"action": action, "args": tail, "raw": raw}
    # «готов 1043» и «пауза P1» — глагол может идти вторым словом.
    lowered = raw.lower()
    for action, words in COMMAND_ALIASES.items():
        for word in words:
            if lowered.startswith(word + " "):
                return {"action": action, "args": raw[len(word):].strip(), "raw": raw}
    return {"action": "", "args": "", "raw": raw}


def keyboard(*rows: list[tuple[str, str]]) -> dict:
    """Inline-клавиатура из пар (текст, callback). Общий вид для обоих ботов."""
    return {"inline_keyboard": [[{"text": text, "callback_data": data}
                                 for text, data in row] for row in rows if row]}


def money_text(value: Any, currency: str = "₽") -> str:
    """Деньги для Telegram: без копеек, с неразрывным пробелом."""
    from .accounting import num
    return f"{num(value):,.0f}".replace(",", " ") + " " + currency
