"""Клиентский Telegram-бот NOZZA: витрина и заказы в кармане покупателя.

Отдельный бот с собственным токеном (client_bot_token) — внутренний бот
сотрудников и публичный бот покупателей не пересекаются. Работает на long
polling, как и внутренний: ни белого IP, ни проброса портов.

Что умеет покупатель:
• «каталог» — витрина с ценами и остатками, страницами и карточками товаров
  (у позиций с фото карточка приходит картинкой);
• «заказ 3» или кнопка — заказ по номеру позиции, заказ попадает в панель
  как новая заявка (канал Telegram) и получает номер;
• «индивидуальный …» — заявка на печать по задаче: текст уходит в панель;
• фото — заявка с референсом: снимок скачивается, крепится к заказу и
  уходит мастеру; подпись к фото работает как текст команды;
• «мои заказы» / «статус 1001» — статусы своих заказов (тоже кнопкой),
  к своей карточке — кнопки «Оплатить» и «Статус онлайн»;
• «телефон +7…» или кнопка «Отправить номер» — привязать телефон, чтобы
  видеть заказы с полки и Авито;
• «вопрос-ответ» — материалы, сроки и как заказать;
• «оператор» / кнопка «Связаться с оператором» — покупатель зовёт человека;
• уведомления: статус изменился — бот напишет сам; после выдачи спросит
  отзыв, а «готовый» заказ мягко напомнит о себе, если его не забрали.

Вопросы мимо команд не теряются: мастер получает уведомление и отвечает
из внутреннего бота командой «кответ <chat_id> <текст>». Ответ покупателя
на сообщение оператора не считается «не понял»: он уходит в inbox мастеру
как продолжение диалога, а не как новая команда.

Диалоги пишутся в client_bot_log и видны на вкладке «Клиент-бот» в панели.
Локальные шаблоны ответов мастер редактирует там же; выбранный шаблон
подставляется в composer inbox перед отправкой.
Бот никогда не показывает цены чужих заказов, себестоимость и внутренние
данные — только имя изделия, статус и срок.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re as _re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from .accounting import num, uid
from .config import now_iso

API = "https://api.telegram.org/bot{token}/{method}"

HELP = """NOZZA — 3D-печать на заказ. Что я умею:

• каталог — что есть на полке, с ценами и кнопками
• заказ 3 — заказать позицию №3 из каталога
• корзина — проверить выбранные позиции и оформить заявку
• индивидуальный <задача> — печать по вашему заданию (можно фото)
• мои заказы — статусы ваших заказов
• статус 1001 — статус заказа по номеру
• телефон +7… — привязать телефон к заказам с полки
• оператор — связаться с мастерской, ответит человек
• вопрос-ответ — материалы, сроки, как заказать
• помощь — это сообщение

Как считается индивидуальный заказ: пришлите задачу, размеры или фото —
мастер посчитает цену и срок, и вы получите ответ здесь же. Кнопки под
сообщениями работают так же, как команды."""

# Частые вопросы: только подтверждённые формулировки из контекста «О нас» —
# без обещаний свойств материалов и сроков, которых нет.
FAQ_TEXT = """❓ Частые вопросы

Материалы. Базовые — PLA и PETG, выбор зависит от задачи: PLA — для сухого
интерьера, PETG — для более функциональных и влажных задач.

Сроки. Обычный ориентир после согласования — 1–3 дня; точный срок зависит
от модели, размера, материала и очереди.

Цена. Считаем до печати и не меняем задним числом. Расчёт бесплатный:
пришлите фото, размеры и назначение — мастер ответит прямо здесь.

Как заказать. Напишите «индивидуальный» + задача или выберите готовую
вещь в «каталоге». Для заказов с полки пригодится «телефон +7…».

Чего мы не делаем. Не выдаём печать за гарантированно безопасное решение
для еды, детей, медицины и ответственных узлов без отдельной проверки."""

SHARE_TEXT = "Каталог NOZZA — 3D-печать под задачу"
PAGE_SIZE = 8  # позиций каталога на странице (кнопки-карточки, одна в ряд)

WELCOME = ("Здравствуйте! Это бот мастерской NOZZA — «Там, где рождается форма».\n\n"
           "Здесь можно посмотреть готовые вещи на полке, заказать печать по своей "
           "задаче и следить за статусом заказа.\n\nНапишите «помощь» — покажу команды.")


def _money(value: float) -> str:
    return f"{round(num(value)):,}".replace(",", " ") + " ₽"


def _keyboard(*rows: list[tuple[str, str]]) -> dict:
    return {"inline_keyboard": [
        [{"text": text, "callback_data": data} for text, data in row] for row in rows]}


class ClientBot:
    """Фоновый слушатель клиентского бота. Активен при включённой настройке."""

    def __init__(self, manager):
        self.manager = manager
        self.db = manager.db
        self._stop = threading.Event()
        # Offset хранится в SQLite, а не только в RAM: после перезапуска уже
        # подтверждённые Telegram updates не повторяются, необработанные остаются
        # в очереди Telegram.
        try:
            self._offset = max(0, int(num(self.db.setting("client_bot_update_offset", 0))))
        except (TypeError, ValueError):
            self._offset = 0
        self.last_poll = 0.0
        self._current_update_id = ""
        # заказы, о которых уже сообщили в этом чате: {(chat, order_id): status}
        self._notified: dict[tuple[str, str], str] = {}
        # анти-спам уведомлений мастеру: {chat: время последней}
        self._master_alerts: dict[str, float] = {}
        # чаты, где ждём описание проблемы после оценки «не очень»:
        # {chat: (order_id, до какого времени)}
        self._await_problem: dict[str, tuple[str, float]] = {}
        self._bot_username = ""      # @username бота, для кнопки «Поделиться»
        self._bot_user_id_cache = ""  # числовой id бота (getMe), для проверки ответов
        self._thread = threading.Thread(target=self._loop, name="pf-client-bot",
                                        daemon=True)
        self._thread.start()

    # ------------------------------------------------------------- транспорт
    def shutdown(self) -> None:
        self._stop.set()

    def _settings(self) -> dict:
        return self.db.settings(include_secrets=True)

    def templates(self) -> list[dict]:
        """Шаблоны ответов оператора из локальной настройки.

        Текст хранится в SQLite вместе с остальными настройками, но токены и
        другие секреты туда не попадают. Поля нормализуются перед выдачей в
        панель, чтобы старый/ручной JSON не ломал интерфейс.
        """
        raw = self._settings().get("client_bot_templates", [])
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (TypeError, ValueError):
                raw = []
        if not isinstance(raw, list):
            return []
        result = []
        for item in raw[:30]:
            if not isinstance(item, dict):
                continue
            ident = str(item.get("id") or "").strip()[:80]
            name = str(item.get("name") or "").strip()[:100]
            message = str(item.get("text") or "").strip()[:1500]
            if ident and name and message:
                result.append({"id": ident, "name": name, "text": message,
                               "enabled": bool(item.get("enabled", True))})
        return result

    def save_template(self, template_id: str = "", name: str = "", text: str = "") -> dict:
        """Создать/изменить шаблон ответа; возвращает нормализованную запись."""
        name = str(name or "").strip()[:100]
        text = str(text or "").strip()[:1500]
        if not name:
            raise ValueError("Укажите название шаблона")
        if not text:
            raise ValueError("Введите текст шаблона")
        with self.db.transaction():
            templates = self.templates()
            ident = str(template_id or "").strip()[:80]
            if ident:
                found = next((item for item in templates if item["id"] == ident), None)
                if not found:
                    raise ValueError("Шаблон не найден")
                found.update({"name": name, "text": text, "enabled": True})
                saved = found
            else:
                ident = uid("tpl")
                saved = {"id": ident, "name": name, "text": text, "enabled": True}
                templates.append(saved)
            self.db.set_settings({"client_bot_templates": templates[:30]})
        return saved

    def delete_template(self, template_id: str) -> None:
        ident = str(template_id or "").strip()
        if not ident:
            raise ValueError("Укажите шаблон")
        with self.db.transaction():
            templates = [item for item in self.templates() if item["id"] != ident]
            self.db.set_settings({"client_bot_templates": templates})

    def _call(self, method: str, params: dict, timeout: int = 35) -> dict:
        token = self._settings().get("client_bot_token", "")
        if not token:
            return {}
        url = API.format(token=token, method=method)
        data = urllib.parse.urlencode(params).encode()
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data),
                                        timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8", "ignore"))
        except Exception:
            return {}

    def _outbox_add(self, chat: str, method: str, payload: dict,
                    dedupe_key: str = "", file_path: str = "") -> dict | None:
        """Поставить исходящее сообщение в постоянную очередь.

        Важное свойство: сначала создаётся запись в SQLite, затем выполняется
        HTTP-вызов Telegram. При падении процесса между этими шагами worker на
        следующем запуске повторит доставку, а уникальный dedupe_key не даст
        отправить одно и то же уведомление дважды.
        """
        key = str(dedupe_key or "").strip()[:240]
        if key:
            existing = self.db.one(
                "SELECT * FROM client_bot_outbox WHERE dedupe_key=?", (key,))
            if existing:
                return existing
        stamp = now_iso()
        ident = uid("cbout")
        # Пустой ключ должен быть NULL: UNIQUE допускает много NULL, но не
        # допускает несколько пустых строк. Обычные сообщения без dedupe_key
        # иначе начали бы теряться после первой отправки.
        db_key = key or None
        self.db.execute(
            "INSERT OR IGNORE INTO client_bot_outbox"
            "(id,dedupe_key,chat_id,method,payload,file_path,state,attempts,available_at,created_at)"
            " VALUES(?,?,?,?,? ,?, 'pending',0,?,?)",
            (ident, db_key, str(chat), method,
             json.dumps(payload, ensure_ascii=False), str(file_path or ""), stamp, stamp),
        )
        return self.db.one("SELECT * FROM client_bot_outbox WHERE id=?", (ident,)) \
            or (self.db.one("SELECT * FROM client_bot_outbox WHERE dedupe_key=?", (key,)) if key else None)

    def _outbox_send(self, row: dict) -> bool:
        """Синхронно попробовать доставить одну запись очереди."""
        try:
            payload = json.loads(row.get("payload") or "{}")
        except (TypeError, ValueError):
            payload = {}
        claim = self.db.execute(
            "UPDATE client_bot_outbox SET state='sending',available_at=? WHERE id=? AND state='pending'",
            (now_iso(), row.get("id")))
        if not claim.rowcount:
            current = self.db.one("SELECT state FROM client_bot_outbox WHERE id=?",
                                  (row.get("id"),)) or {}
            return current.get("state") == "sent"
        row["state"] = "sending"
        result: dict = {}
        if row.get("method") == "sendPhoto":
            raw = b""
            path = str(row.get("file_path") or "")
            if path:
                try:
                    raw = Path(path).read_bytes()
                except OSError:
                    raw = b""
            if raw:
                result = self._send_photo_now(payload, raw)
        else:
            result = self._call(str(row.get("method") or "sendMessage"),
                                payload, timeout=15)
        if result.get("ok"):
            self.db.execute(
                "UPDATE client_bot_outbox SET state='sent',sent_at=?,last_error='' WHERE id=?",
                (now_iso(), row["id"]),
            )
            path = str(row.get("file_path") or "")
            if path:
                try:
                    Path(path).unlink(missing_ok=True)
                except OSError:
                    pass
            return True
        attempts = int(num(row.get("attempts"))) + 1
        # Экспоненциальная пауза ограничена пятью минутами; запись не удаляем.
        delay = min(300, 2 ** min(attempts, 8))
        available = datetime.fromtimestamp(time.time() + delay).astimezone().isoformat()
        self.db.execute(
            "UPDATE client_bot_outbox SET state='pending',attempts=?,available_at=?,last_error=? WHERE id=?",
            (attempts, available, "Telegram не подтвердил доставку", row["id"]),
        )
        return False

    def _drain_outbox(self, limit: int = 30) -> int:
        """Доставить due-сообщения; вызывается после polling и тестируется отдельно."""
        self.db.execute(
            "UPDATE client_bot_outbox SET state='pending'"
            " WHERE state='sending' AND datetime(replace(available_at,'T',' '))"
            " <= datetime('now','-300 seconds')")
        rows = self.db.query(
            "SELECT * FROM client_bot_outbox WHERE state='pending'"
            " AND (available_at IS NULL OR available_at='' OR"
            " datetime(replace(available_at,'T',' '))<=datetime('now'))"
            " ORDER BY datetime(replace(created_at,'T',' ')), id LIMIT ?",
            (max(1, int(limit)),))
        sent = 0
        for row in rows:
            if self._outbox_send(row):
                sent += 1
        return sent

    def _reply(self, chat: str, text: str, buttons: dict | None = None,
               dedupe_key: str = "") -> None:
        params: dict = {"chat_id": chat, "text": text[:3800],
                        "disable_web_page_preview": "true"}
        if buttons:
            params["reply_markup"] = json.dumps(buttons, ensure_ascii=False)
        if not dedupe_key and self._current_update_id:
            digest = hashlib.sha256(
                (str(chat) + "\0" + str(text) + "\0" + json.dumps(buttons or {}, sort_keys=True)).encode()
            ).hexdigest()[:24]
            dedupe_key = f"update:{self._current_update_id}:message:{digest}"
        row = self._outbox_add(str(chat), "sendMessage", params, dedupe_key)
        if row and row.get("state") != "sent":
            self._outbox_send(row)

    def _reply_keyed(self, chat: str, text: str, buttons: dict | None = None,
                     dedupe_key: str = "") -> None:
        """Совместимый вызов для старых интеграций, подменяющих _reply."""
        try:
            self._reply(chat, text, buttons, dedupe_key=dedupe_key)
        except TypeError as exc:
            if "dedupe_key" not in str(exc):
                raise
            self._reply(chat, text, buttons)

    def _send_photo_now(self, params: dict, raw: bytes) -> dict:
        """Непосредственная multipart-отправка без изменения outbox."""
        token = self._settings().get("client_bot_token", "")
        if not token or not raw:
            return {}
        boundary = f"pfcb{int(time.time() * 1000)}"
        parts: list[bytes] = []
        def field(name: str, value: str) -> None:
            parts.append(f"--{boundary}\r\nContent-Disposition: form-data;"
                         f' name="{name}"\r\n\r\n{value}\r\n'.encode())
        for key, value in params.items():
            if key != "chat_id":
                field(key, str(value))
        field("chat_id", str(params.get("chat_id") or ""))
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data;"
                     f' name="photo"; filename="item.jpg"\r\n'
                     f"Content-Type: image/jpeg\r\n\r\n".encode())
        parts.append(raw)
        parts.append(f"\r\n--{boundary}--\r\n".encode())
        url = API.format(token=token, method="sendPhoto")
        try:
            request = urllib.request.Request(
                url, data=b"".join(parts),
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.loads(response.read().decode("utf-8", "ignore"))
        except Exception:
            return {}

    def _send_photo(self, chat: str, caption: str, raw: bytes,
                    buttons: dict | None = None, dedupe_key: str = "") -> None:
        """Положить карточку с фото в ту же durable outbox, что и текст."""
        if not raw:
            return self._reply_keyed(chat, caption, buttons, dedupe_key=dedupe_key)
        from .config import PHOTO_DIR
        try:
            PHOTO_DIR.mkdir(parents=True, exist_ok=True)
            path = PHOTO_DIR / f"client_outbox_{uid('photo')}.jpg"
            path.write_bytes(raw)
        except OSError:
            return self._reply_keyed(chat, caption, buttons, dedupe_key=dedupe_key)
        params: dict = {"chat_id": chat, "caption": caption[:1000]}
        if buttons:
            params["reply_markup"] = json.dumps(buttons, ensure_ascii=False)
        if not dedupe_key and self._current_update_id:
            dedupe_key = f"update:{self._current_update_id}:photo:{hashlib.sha256((chat + caption).encode()).hexdigest()[:24]}"
        row = self._outbox_add(chat, "sendPhoto", params, dedupe_key, str(path))
        if row and row.get("state") != "sent":
            self._outbox_send(row)

    def _download_file(self, file_id: str) -> bytes | None:
        """Скачать файл покупателя (getFile → скачивание), как во внутреннем боте."""
        token = self._settings().get("client_bot_token", "")
        if not token or not file_id:
            return None
        try:
            info = self._call("getFile", {"file_id": file_id})
            path = str((info.get("result") or {}).get("file_path") or "")
            if not path:
                return None
            url = f"https://api.telegram.org/file/bot{token}/{path}"
            with urllib.request.urlopen(url, timeout=30) as response:
                return response.read()
        except Exception:
            return None

    def _username(self) -> str:
        """@username бота для кнопки «Поделиться» (getMe, кэшируется)."""
        if not self._bot_username:
            me = self.me()
            if me.get("ok"):
                self._bot_username = str(me.get("username") or "")
        return self._bot_username

    def _log(self, chat: str, name: str, text: str, answer: str,
             kind: str = "message", *, direction: str = "in",
             update_id: str = "", event: str = "", order_id: str = "",
             source: str = "", unread: int | None = None,
             operator: str = "") -> None:
        """Записать сообщение и одновременно сформировать inbox-признак."""
        try:
            if unread is None:
                unread = 1 if direction == "in" else 0
            self.db.execute(
                "INSERT INTO client_bot_log(at,chat_id,name,text,answer,kind,update_id,"
                "direction,event,order_id,source,unread,operator)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (now_iso(), chat, name or "", (text or "")[:500],
                 (answer or "")[:1000], kind, str(update_id or self._current_update_id or ""),
                 direction, event, order_id, source, int(unread), operator))
        except Exception:
            pass  # журнал не должен ломать ответы клиенту

    def _touch_chat(self, chat: str, message: dict) -> dict:
        """Обновить профиль приватного Telegram-чата без затирания данных.

        Telegram user id хранится отдельно от chat_id: это позволяет проверить,
        что переданный контакт принадлежит именно отправителю, а не чужому
        профилю, пересланному в чат.
        """
        profile = message.get("from") or {}
        row = self.db.one("SELECT * FROM client_chats WHERE chat_id=?", (chat,))
        patch = {"chat_id": chat, "last_seen": now_iso()}
        if profile.get("id") is not None:
            patch["tg_user_id"] = str(profile.get("id"))
        if str(profile.get("first_name") or profile.get("title") or ""):
            patch["name"] = str(profile.get("first_name") or profile.get("title"))
        if str(profile.get("username") or ""):
            patch["username"] = str(profile.get("username"))
        if row:
            for field in ("name", "username", "tg_user_id", "phone", "phone_verified",
                          "customer_id", "source", "status_notify", "marketing_opt_in",
                          "quiet_from", "quiet_to", "inbox_status", "assigned_to", "last_error"):
                patch.setdefault(field, row.get(field) or (0 if field in ("phone_verified", "status_notify", "marketing_opt_in") else ""))
            patch["created_at"] = row.get("created_at") or now_iso()
        else:
            patch.setdefault("name", "")
            patch.setdefault("username", "")
            patch.setdefault("tg_user_id", "")
            patch.setdefault("phone", "")
            patch.setdefault("phone_verified", 0)
            patch.setdefault("customer_id", "")
            patch.setdefault("source", "")
            patch.setdefault("status_notify", 1)
            patch.setdefault("marketing_opt_in", 0)
            patch.setdefault("quiet_from", "")
            patch.setdefault("quiet_to", "")
            patch.setdefault("inbox_status", "open")
            patch.setdefault("assigned_to", "")
            patch.setdefault("last_error", "")
            patch["created_at"] = now_iso()
        return self.db.upsert("client_chats", patch, key="chat_id")

    def _claim_update(self, update: dict) -> bool | None:
        """Атомарно занять update_id; повторная доставка не исполняется.

        ``True`` означает «обработать», ``False`` — «временно не трогать»,
        ``None`` — update уже успешно завершён (polling может подтвердить
        offset, но побочный эффект повторять нельзя). Упавший update переводим
        обратно в processing, чтобы ошибка сети/процесса не оставила очередь
        навсегда заблокированной.
        """
        update_id = str(update.get("update_id") or "").strip()
        if not update_id:
            return True
        callback = update.get("callback_query") or {}
        message = update.get("message") or callback.get("message") or {}
        chat = str((message.get("chat") or {}).get("id") or "")
        kind = "callback" if callback else "message"
        cur = self.db.execute(
            "INSERT OR IGNORE INTO client_bot_updates"
            "(update_id,chat_id,kind,state,received_at) VALUES(?,?,?,?,?)",
            (update_id, chat, kind, "processing", now_iso()))
        if cur.rowcount:
            return True
        existing = self.db.one(
            "SELECT state,received_at FROM client_bot_updates WHERE update_id=?", (update_id,)) or {}
        state = str(existing.get("state") or "")
        if state == "done":
            return None
        # Не перехватываем живую обработку: второй поток/экземпляр бота
        # должен дождаться её завершения. Повторяем только failed или запись,
        # которая действительно зависла дольше пяти минут.
        stale = state == "failed"
        if state == "processing":
            try:
                received = datetime.fromisoformat(str(existing.get("received_at") or ""))
                if received.tzinfo is None:
                    received = received.astimezone()
                stale = (datetime.now().astimezone() - received).total_seconds() >= 300
            except (TypeError, ValueError, OverflowError):
                stale = False
        if not stale:
            return False
        now = now_iso()
        if state == "processing":
            cur = self.db.execute(
                "UPDATE client_bot_updates SET state='processing',received_at=?,error=''"
                " WHERE update_id=? AND state='processing' AND received_at=?",
                (now, update_id, existing.get("received_at")),
            )
        else:
            cur = self.db.execute(
                "UPDATE client_bot_updates SET state='processing',received_at=?,error=''"
                " WHERE update_id=? AND state='failed'",
                (now, update_id),
            )
        if cur.rowcount:
            return True
        latest = self.db.one("SELECT state FROM client_bot_updates WHERE update_id=?",
                             (update_id,)) or {}
        return None if latest.get("state") == "done" else False

    def _finish_update(self, update_id: str, ok: bool = True, error: str = "") -> None:
        if not update_id:
            return
        self.db.execute(
            "UPDATE client_bot_updates SET state=?,processed_at=?,error=? WHERE update_id=?",
            ("done" if ok else "failed", now_iso(), str(error or "")[:500], update_id))

    def _funnel(self, chat: str, event: str, row: dict | None = None,
                *, order_id: str = "", nom_id: str = "", source: str = "",
                data: dict | None = None) -> None:
        try:
            self.db.execute(
                "INSERT INTO client_bot_funnel(at,chat_id,event,source,order_id,nom_id,data)"
                " VALUES(?,?,?,?,?,?,?)",
                (now_iso(), str(chat), str(event),
                 str(source or (row or {}).get("source") or "telegram"),
                 str(order_id or ""), str(nom_id or ""),
                 json.dumps(data or {}, ensure_ascii=False)))
        except Exception:
            pass

    def _link_order(self, chat: str, row: dict, order: dict,
                    *, source: str = "telegram", update_id: str = "") -> dict:
        """Связать заказ с чатом, клиентом и источником идемпотентно."""
        update_id = str(update_id or self._current_update_id or "")
        self.db.execute(
            "INSERT OR IGNORE INTO client_orders(chat_id,order_id,number,update_id,source,created_at)"
            " VALUES(?,?,?,?,?,?)",
            (chat, order.get("id") or "", order.get("number") or "", update_id,
             source, now_iso()))
        customer_id = str(order.get("customer_id") or "")
        if customer_id:
            try:
                self.db.execute(
                    "UPDATE customers SET tg_user_id=? WHERE id=? AND (tg_user_id IS NULL OR tg_user_id='')",
                    (str(row.get("tg_user_id") or chat), customer_id))
                self.db.execute("UPDATE client_chats SET customer_id=? WHERE chat_id=?",
                                (customer_id, chat))
            except Exception:
                pass
        self._track_token(order.get("id") or "")
        return self.db.one("SELECT * FROM client_orders WHERE chat_id=? AND order_id=?",
                           (chat, order.get("id") or "")) or {}

    def _track_token(self, order_id: str) -> str:
        """Детерминированный секретный токен ссылки, без хранения plaintext."""
        order_id = str(order_id or "")
        token = str(self._settings().get("client_bot_token") or "")
        if not order_id or not token:
            return ""
        raw = hmac.new(token.encode(), order_id.encode(), hashlib.sha256).hexdigest()
        digest = hashlib.sha256(raw.encode()).hexdigest()
        current = self.db.one("SELECT client_track_token_hash FROM orders WHERE id=?", (order_id,)) or {}
        if current.get("client_track_token_hash") != digest:
            self.db.execute(
                "UPDATE orders SET client_track_token_hash=?,client_track_token_at=? WHERE id=?",
                (digest, now_iso(), order_id))
        return raw

    def _in_quiet_hours(self, row: dict | None = None) -> bool:
        settings = self._settings()
        source = row or {}
        has_personal = bool(source.get("quiet_from") and source.get("quiet_to"))
        if not has_personal and not bool(settings.get("client_bot_quiet_hours_enabled", False)):
            return False
        start = str(source.get("quiet_from") or settings.get("client_bot_quiet_from") or "")
        end = str(source.get("quiet_to") or settings.get("client_bot_quiet_to") or "")
        if not start or not end:
            return False
        try:
            now = datetime.now().hour * 60 + datetime.now().minute
            sh, sm = (int(x) for x in start.split(":", 1))
            eh, em = (int(x) for x in end.split(":", 1))
            a, b = sh * 60 + sm, eh * 60 + em
            return now >= a or now < b if a >= b else a <= now < b
        except (TypeError, ValueError):
            return False

    # ---------------------------------------------------------------- цикл
    def _loop(self) -> None:
        while not self._stop.is_set():
            settings = self._settings()
            if not (settings.get("client_bot_enabled")
                    and settings.get("client_bot_token")):
                self._stop.wait(20)
                continue
            try:
                result = self._call("getUpdates", {
                    "offset": self._offset, "timeout": 25,
                    "allowed_updates": json.dumps(["message", "callback_query"]),
                })
                if result.get("ok"):
                    self.last_poll = time.time()
                for update in (result.get("result") or []):
                    update_id = str(update.get("update_id") or "")
                    if not self._handle(update, dedupe=True):
                        # Не подтверждаем более новые update поверх живой или
                        # временно упавшей обработки: сохраняем порядок.
                        break
                    try:
                        next_offset = int(num(update_id)) + 1
                    except (TypeError, ValueError):
                        next_offset = self._offset
                    self._offset = max(self._offset, next_offset)
                    # Telegram подтверждает update только когда offset ушёл
                    # в сеть; храним тот же watermark для восстановления.
                    self.db.set_settings({"client_bot_update_offset": self._offset})
                self._drain_outbox()
                self._maybe_push_statuses()
                self._maybe_ask_reviews()
                self._maybe_remind_pickup()
                self._drain_outbox()
            except Exception:
                self._stop.wait(10)

    # ------------------------------------------------------------ обработка
    def _handle(self, update: dict, *, dedupe: bool = False) -> bool:
        """Обработать одно обновление.

        ``dedupe`` включён только polling-циклом; прямые вызовы этого метода
        остаются удобными для offline-тестов и внутренних интеграций.
        """
        update_id = str(update.get("update_id") or "")
        claimed = False
        if dedupe:
            claim = self._claim_update(update)
            if claim is None:
                return True  # уже выполнен: Telegram можно подтвердить offset
            if not claim:
                return False
            claimed = True
        previous = self._current_update_id
        self._current_update_id = update_id
        try:
            callback = update.get("callback_query") or {}
            if callback:
                self._handle_callback(callback)
                if claimed:
                    self._finish_update(update_id, True)
                return True
            message = update.get("message") or {}
            telegram_chat = message.get("chat") or {}
            chat_type = str(telegram_chat.get("type") or "")
            # Клиентский бот никогда не отвечает в группах/каналах: это
            # предотвращает случайную публикацию заказов и персональных данных.
            if chat_type != "private":
                if claimed:
                    self._finish_update(update_id, True, "non_private_chat")
                return True
            chat = str(telegram_chat.get("id", ""))
            if not chat:
                if claimed:
                    self._finish_update(update_id, True, "no_chat")
                return True
            row = self._touch_chat(chat, message)
            # Подпись к фото работает как текст: покупатель шлёт снимок эскиза
            # с подписью «индивидуальный …» — это уже готовая заявка.
            caption = (message.get("caption") or "").strip()
            text = (message.get("text") or "").strip()
            try:
                if message.get("photo"):
                    answer, buttons = self._photo_reply(chat, row, message, caption)
                elif message.get("document"):
                    answer, buttons = self._document_reply(chat, row, message, caption)
                elif text:
                    answer, buttons = self._dispatch(
                        chat, row, text,
                        reply=self._is_reply_to_operator(message))
                else:
                    answer, buttons = self._no_text_reply(chat, row, message)
                self._reply(chat, answer, buttons)
                self._log(chat, row.get("name") or "",
                          text or (f"📷 {caption}" if caption else
                                   self._media_label(message)), answer,
                          update_id=update_id, source=row.get("source") or "")
            except Exception as exc:
                answer = f"Не получилось: {exc}"
                self._reply(chat, answer)
                self._log(chat, row.get("name") or "", text or caption, answer,
                          update_id=update_id, source=row.get("source") or "")
                if claimed:
                    self._finish_update(update_id, False, str(exc))
                return False
            if claimed:
                self._finish_update(update_id, True)
            return True
        finally:
            self._current_update_id = previous

    def _document_reply(self, chat: str, row: dict, message: dict,
                        caption: str) -> tuple[str, dict | None]:
        """Принять PDF/чертёж/архив как файл заявки и сохранить его в заказе."""
        document = message.get("document") or {}
        file_id = str(document.get("file_id") or "")
        raw = self._download_file(file_id) if file_id else None
        if not raw:
            return ("Файл получил, но скачать его не удалось. Попробуйте отправить ещё раз "
                    "или пришлите ссылку/фото."), self._menu()
        active = [o for o in self._linked_orders(chat, row) if not self._is_final(o)]
        if active:
            self._attach_file(active[0]["id"], raw, document.get("file_name") or "file", chat)
            return (f"Файл получил ✓ Прикрепил к заказу №{active[0].get('number')}. "
                    "Мастер увидит его в карточке."), self._menu()
        task = caption or "Заявка с файлом — описание уточняется"
        order = self.manager.repo.save_order({
            "product": task[:120], "customer_name": row.get("name") or "Покупатель",
            "phone": row.get("phone") or "",
            "messenger": (f"tg:@{row.get('username')}" if row.get("username") else f"tg:{chat}"),
            "channel": "telegram", "status": "new", "qty": 1,
            "client_source": row.get("source") or "telegram",
            "client_quote_status": "requested",
            "notes": "Файл из клиентского бота; описание уточняется",
        })
        self._link_order(chat, row, order, source="telegram")
        self._attach_file(order.get("id") or "", raw,
                          document.get("file_name") or "file", chat, lead=True)
        self._funnel(chat, "file_order", row, order_id=order.get("id") or "")
        return (f"Файл получил ✓ Заявка №{order.get('number')} создана. "
                "Напишите размеры, материал и назначение — мастер посчитает цену и срок."), self._menu()

    def _attach_file(self, order_id: str, raw: bytes | None, filename: str,
                     chat: str, lead: bool = False) -> None:
        """Сохранить файл клиента как order_photo с безопасным именем."""
        from .config import PHOTO_DIR
        if not raw:
            self._alert_master(chat, self.db.one("SELECT * FROM client_chats WHERE chat_id=?", (chat,)) or {},
                               f"Файл {filename} не удалось скачать")
            return
        if len(raw) > 20 * 1024 * 1024:
            raise ValueError("Файл больше 20 МБ")
        safe_name = _re.sub(r"[^a-zA-Z0-9а-яА-Я._-]+", "_", str(filename or "file"))[:80]
        try:
            PHOTO_DIR.mkdir(parents=True, exist_ok=True)
            path = PHOTO_DIR / f"client_{order_id}_{int(time.time() * 1000)}_{safe_name}"
            path.write_bytes(raw)
        except OSError as exc:
            raise ValueError(f"Не удалось сохранить файл: {exc}") from exc
        self.db.upsert("order_photos", {
            "id": uid("file"), "order_id": order_id, "at": now_iso(),
            "file": path.name, "note": "файл из клиентского бота", "kind": "client_file"})
        number = (self.db.one("SELECT number FROM orders WHERE id=?", (order_id,)) or {}).get("number") or ""
        try:
            self.manager.notify_async(
                f"📎 Файл от покупателя — заказ №{number}\nЧат {chat} · {safe_name}",
                critical=bool(lead))
        except Exception:
            pass

    def _photo_reply(self, chat: str, row: dict, message: dict,
                     caption: str) -> tuple[str, dict | None]:
        """Фото покупателя — заявка с референсом.

        Снимок скачивается, крепится к заказу через order_photos и уходит
        мастеру в уведомлении: он видит задачу глазами покупателя.
        """
        file_id = str(((message.get("photo") or [{}])[-1] or {}).get("file_id") or "")
        raw = self._download_file(file_id) if file_id else None
        low = caption.lower().replace("ё", "е").strip()
        if low.startswith("индивидуальн"):
            previous = self._latest_linked(chat)
            answer = self._custom_order(chat, row, caption)
            order = self._latest_linked(chat)
            # Если команда только открыла черновик, не прикрепляем фото к
            # предыдущему заказу. После описания фото можно прислать повторно.
            if order and (not previous or order.get("id") != previous.get("id")):
                self._attach_photo(order["id"], raw, chat)
            return answer, self._menu()
        # «фото 1001» — приложить снимок к своему заказу
        number = next((w for w in low.split() if w.isdigit()), "")
        if number:
            order = self.db.one("SELECT * FROM orders WHERE number=?", (number,))
            if order and any(o["id"] == order["id"]
                             for o in self._linked_orders(chat, row)):
                self._attach_photo(order["id"], raw, chat)
                return (f"Фото получил ✓ Прикрепил к заказу №{number} — "
                        "мастер увидит его в карточке."), self._menu()
            return ("Такой заказ не нашёл среди ваших. Номер — из подтверждения, "
                    "например «фото 1001».", self._menu())
        # без подписи: крепим к последнему активному заказу чата, а если
        # активных нет — заводим лид-фотозаявку и просим описать задачу
        active = [o for o in self._linked_orders(chat, row)
                  if not self._is_final(o)]
        if active:
            self._attach_photo(active[0]["id"], raw, chat)
            return (f"Фото получил ✓ Прикрепил к заказу "
                    f"№{active[0].get('number')}. Если это новая задача — "
                    "напишите «индивидуальный» + описание."), self._menu()
        order = self.manager.repo.save_order({
            "product": "Фотозаявка — описание уточняется",
            "customer_name": row.get("name") or "Покупатель",
            "phone": row.get("phone") or "",
            "messenger": (f"tg:@{row.get('username')}" if row.get("username")
                          else f"tg:{chat}"),
            "channel": "telegram", "status": "new", "qty": 1,
            "client_source": row.get("source") or "telegram",
            "client_quote_status": "requested",
            "notes": "Фотозаявка из клиентского бота (фото без описания)",
        })
        number = order.get("number") or ""
        self._link_order(chat, row, order, source="telegram")
        self._notified[(chat, order.get("id", ""))] = "new"
        self._funnel(chat, "photo_order", row, order_id=order.get("id") or "")
        self._attach_photo(order.get("id"), raw, chat, lead=True)
        return ("Фото получил ✓ Мастерская уже видит снимок. Опишите задачу — "
                "что напечатать, размеры и назначение — и мастер посчитает "
                "цену и срок.", self._menu())

    def _attach_photo(self, order_id: str, raw: bytes | None, chat: str,
                      lead: bool = False) -> None:
        """Сохранить снимок покупателя в order_photos и показать мастеру."""
        from .config import PHOTO_DIR
        name = ""
        if raw:
            try:
                PHOTO_DIR.mkdir(parents=True, exist_ok=True)
                name = f"client_{order_id}_{int(time.time() * 1000)}.jpg"
                (PHOTO_DIR / name).write_bytes(raw)
            except Exception:
                name = ""
        if name:
            self.db.upsert("order_photos", {
                "id": uid("ph"), "order_id": order_id, "at": now_iso(),
                "file": name, "note": "фото из клиентского бота",
                "kind": "client"})
        row = self.db.one("SELECT number FROM orders WHERE id=?", (order_id,))
        number = (row or {}).get("number") or ""
        try:
            self.manager.notify_async(
                f"📷 Фото от покупателя — заказ №{number}\n"
                f"Чат {chat} · ответить: кответ {chat} <текст>",
                photo=raw or None, critical=bool(lead))
        except Exception:
            pass

    def _latest_linked(self, chat: str) -> dict | None:
        order_id = (self.db.one(
            "SELECT order_id FROM client_orders WHERE chat_id=?"
            " ORDER BY datetime(created_at) DESC LIMIT 1", (chat,)) or {}
        ).get("order_id")
        return self.db.one("SELECT * FROM orders WHERE id=?", (order_id,)) \
            if order_id else None

    def _is_final(self, order: dict) -> bool:
        meta = self.db.one("SELECT is_final FROM statuses WHERE id=?",
                           (order.get("status") or "",))
        return bool(meta and num(meta.get("is_final")))

    def _no_text_reply(self, chat: str, row: dict,
                       message: dict) -> tuple[str, dict | None]:
        """Ответ на сообщение без текста: контакт привязываем, медиа — подсказка.

        Раньше фото и голосовые молча игнорировались, хотя помощь просит
        «пришлите фото» — покупатель решал, что бот сломался.
        """
        contact = message.get("contact") or {}
        phone = str(contact.get("phone_number") or "")
        if phone:
            sender_id = str((message.get("from") or {}).get("id") or "")
            contact_owner = str(contact.get("user_id") or "")
            if not sender_id or contact_owner != sender_id:
                return ("Нужно отправить именно свой контакт Telegram — чужой номер "
                        "нельзя использовать для привязки заказов.", self._contact_keyboard())
            return self._save_phone(chat, row, f"телефон {phone}", verified=True), self._menu()
        label = self._media_label(message)
        return (f"{label.capitalize()} получил ✓ Опишите задачу текстом — "
                "«индивидуальный держатель 120 мм» — и мастер посчитает "
                "цену и срок.", self._menu())

    def _media_label(self, message: dict) -> str:
        """Короткое имя вложения для ответа и журнала диалогов."""
        for key, label in (("photo", "фото"), ("document", "файл"),
                           ("video", "видео"), ("video_note", "видео"),
                           ("voice", "голосовое"), ("sticker", "стикер"),
                           ("location", "локацию"), ("contact", "контакт")):
            if message.get(key):
                return label
        return "сообщение"

    def _bot_user_id(self) -> str:
        """Числовой id клиентского бота (getMe), кэшируется на процесс."""
        if not self._bot_user_id_cache:
            me = self._call("getMe", {}, timeout=10)
            self._bot_user_id_cache = str((me.get("result") or {}).get("id") or "")
        return self._bot_user_id_cache

    def _is_reply_to_operator(self, message: dict) -> bool:
        """Покупатель отвечает на сообщение бота (т.е. на ответ оператора).

        У Telegram сообщения бота помечены ``from.is_bot``; числовой id бота —
        запасной способ, когда поле is_bot недоступно (редкие клиенты).
        """
        replied = message.get("reply_to_message") or {}
        if not replied:
            return False
        sender = replied.get("from") or {}
        if sender.get("is_bot"):
            return True
        bot_id = self._bot_user_id()
        return bool(bot_id) and str(sender.get("id") or "") == bot_id

    def _handle_callback(self, callback: dict) -> None:
        message = callback.get("message") or {}
        chat = str((message.get("chat") or {}).get("id", ""))
        data = str(callback.get("data") or "")
        chat_type = str(((message.get("chat") or {}).get("type") or ""))
        if chat_type != "private":
            return
        if not chat or not data:
            return
        self._call("answerCallbackQuery", {"callback_query_id":
                                           str(callback.get("id") or "")})
        row = self.db.one("SELECT * FROM client_chats WHERE chat_id=?", (chat,))
        if not row:
            # Профиль берём из callback (у сообщения от бота поля from нет).
            row = self._touch_chat(chat, {"from": callback.get("from") or {}})
        try:
            if data.startswith("item:"):
                # карточка товара может уйти картинкой — отдельный путь отправки
                self._send_item_card(chat, data)
                answer = "карточка товара"
            else:
                answer, buttons = self._run_callback(chat, row, data)
                # Кнопка обязана ответить: раньше текст только писался в журнал,
                # и покупатель не видел реакции — «кнопки не работают».
                self._reply(chat, answer, buttons)
            # Нажатие inline-кнопки — уже обработанное действие, а не новое
            # непрочитанное сообщение для inbox.
            self._log(chat, row.get("name") or "", data, answer,
                      kind="status", unread=0)
        except Exception as exc:
            answer = f"Не получилось: {exc}"
            self._reply(chat, answer)
            self._log(chat, row.get("name") or "", data, answer,
                      kind="status", unread=0)

    def _run_callback(self, chat: str, row: dict,
                      data: str) -> tuple[str, dict | None]:
        if data.startswith("buy:"):
            return self._order_item(chat, row, data.split(":", 1)[1]), \
                self._menu()
        if data.startswith("cart:"):
            parts = data.split(":", 2)
            return self._add_to_cart(chat, parts[1] if len(parts) > 1 else "",
                                     parts[2] if len(parts) > 2 else ""), self._cart_keyboard(chat)
        if data.startswith("cartv:"):
            parts = data.split(":", 2)
            return self._add_to_cart(chat, parts[1] if len(parts) > 1 else "",
                                     parts[2] if len(parts) > 2 else ""), self._cart_keyboard(chat)
        if data.startswith("cartremove:"):
            parts = data.split(":", 2)
            return self._remove_from_cart(chat, parts[1] if len(parts) > 1 else "",
                                          parts[2] if len(parts) > 2 else ""), self._cart_keyboard(chat)
        if data == "cart":
            return self._cart_text(chat), self._cart_keyboard(chat)
        if data == "checkout":
            return self._checkout_cart(chat, row), self._menu()
        if data.startswith("wish:"):
            return self._toggle_wishlist(chat, data.split(":", 1)[1]), self._menu()
        if data == "wishlist":
            return self._wishlist_text(chat), self._wishlist_keyboard(chat)
        if data.startswith("repeat:"):
            return self._repeat_order(chat, row, data.split(":", 1)[1]), self._menu()
        if data == "notify":
            return self._toggle_notifications(chat, row), self._menu()
        if data == "marketing":
            return self._toggle_marketing(chat, row), self._menu()
        if data.startswith("status:"):
            number = data.split(":", 1)[1]
            return self._order_card(chat, row, number)
        if data.startswith("pay:"):
            return self._pay_card(chat, row, data.split(":", 1)[1])
        if data.startswith("paid:"):
            return self._paid_notice(chat, row, data.split(":", 1)[1])
        if data.startswith("quote_yes:"):
            return self._quote_action(chat, row, data.split(":", 1)[1], True)
        if data.startswith("quote_no:"):
            return self._quote_action(chat, row, data.split(":", 1)[1], False)
        if data.startswith("review:"):
            _, order_id, rating = data.split(":", 2)
            return self._review_rating(chat, row, order_id, rating)
        if data.startswith("catalog:"):
            page = max(1, int(num(data.split(":", 1)[1])))
            return self.text_catalog(page), self._catalog_keyboard(page)
        if data == "catalog":
            return self.text_catalog(), self._catalog_keyboard()
        if data == "mine":
            return self.text_my_orders(chat, row), self._orders_keyboard(chat, row)
        if data == "faq":
            return self.text_faq(), self._menu()
        if data == "help":
            return HELP, self._menu()
        if data == "operator":
            return self._contact_operator(chat, row), self._menu()
        return "Не понял кнопку — напишите «помощь».", self._menu()

    def _menu(self) -> dict:
        return _keyboard(
            [("🛍 Каталог", "catalog"), ("📦 Мои заказы", "mine")],
            [("🛒 Корзина", "cart"), ("💛 Избранное", "wishlist")],
            [("🔔 Статусы", "notify"), ("📣 Рассылка", "marketing")],
            [("❓ Вопрос-ответ", "faq"), ("❔ Помощь", "help")],
            [("👤 Связаться с оператором", "operator")],
        )

    def _contact_keyboard(self) -> dict:
        """Обычная (не inline) клавиатура с кнопкой отправки контакта:
        Telegram сам предлагает номер телефона одним касанием."""
        return {"keyboard": [[{"text": "📱 Отправить номер",
                               "request_contact": True}]],
                "resize_keyboard": True, "one_time_keyboard": True}

    def _dispatch(self, chat: str, row: dict, raw: str,
                  *, reply: bool = False) -> tuple[str, dict | None]:
        """Разбор текстовой команды. Возвращает текст и кнопки; отправляет
        только вызывающий (_handle) — одна точка отправки вместо пяти.

        ``reply=True`` — покупатель отвечает на сообщение оператора: такое
        сообщение не считается «не понял», а уходит мастеру как диалог.
        """
        text = raw.lower().lstrip("/").replace("ё", "е")
        text = _re.sub(r"^nozza\S*\s+", "", text).strip()
        word = text.split()[0] if text else ""

        if word in ("start", "старт", "начать"):
            raw_parts = raw.strip().split(None, 1)
            payload = raw_parts[1].strip()[:120] if len(raw_parts) > 1 else ""
            if payload:
                source = payload
                if payload.startswith(("utm_", "src_")):
                    source = payload.split("_", 1)[1]
                self.db.execute("UPDATE client_chats SET source=? WHERE chat_id=?",
                                (source, chat))
                row["source"] = source
                self._funnel(chat, "start", row, source=source, data={"payload": payload})
            else:
                self._funnel(chat, "start", row)
            welcome = str(self._settings().get("client_bot_welcome") or "").strip()
            return welcome or WELCOME, self._menu()
        quote_answer = self._quote_response(chat, row, raw)
        if quote_answer:
            return quote_answer, self._menu()
        # Если есть незавершённый мастер заявки, любое обычное сообщение —
        # следующий шаг; явные команды меню по-прежнему имеют приоритет.
        if word not in ("help", "помощь", "?", "меню", "каталог", "товары", "витрина",
                        "полка", "catalog", "мои", "заказы", "статус", "status", "заказ",
                        "order", "телефон", "phone", "номер", "faq", "вопрос", "вопросы",
                        "вопрос-ответ", "избранное", "wishlist", "любимое", "уведомления",
                        "уведомление", "рассылка", "отписаться", "корзина", "cart",
                        "оформить", "checkout", "оператор", "оператора", "человек",
                        "связаться", "связь", "контакт", "поддержка"):
            draft_answer, draft_buttons = self._draft_reply(chat, row, raw)
            if draft_answer:
                return draft_answer, draft_buttons
        if word in ("help", "помощь", "?", "меню"):
            return HELP, self._menu()
        if word in ("faq", "вопрос", "вопросы", "вопрос-ответ"):
            self._funnel(chat, "faq", row)
            return self.text_faq(), self._menu()
        if word in ("избранное", "wishlist", "любимое"):
            return self._wishlist_text(chat), self._wishlist_keyboard(chat)
        if word in ("уведомления", "уведомление"):
            return self._toggle_notifications(chat, row), self._menu()
        if word in ("рассылка", "marketing"):
            return self._toggle_marketing(chat, row), self._menu()
        if word in ("отписаться", "стоп рассылка"):
            self.db.execute("UPDATE client_chats SET marketing_opt_in=0 WHERE chat_id=?", (chat,))
            return "Рассылки отключены. Сервисные статусы заказа остаются включены.", self._menu()
        if word in ("каталог", "товары", "витрина", "полка", "catalog"):
            self._funnel(chat, "catalog", row)
            return self.text_catalog(), self._catalog_keyboard()
        if word in ("корзина", "cart"):
            return self._cart_text(chat), self._cart_keyboard(chat)
        if word in ("оформить", "checkout"):
            return self._checkout_cart(chat, row), self._menu()
        if word in ("мои", "мои заказы", "заказы") or text == "мои заказы":
            answer = self.text_my_orders(chat, row)
            # заказов нет и телефон не привязан — вместо пустого списка даём
            # кнопку отправки контакта, чтобы связать заказы с полки
            if not self._linked_orders(chat, row) \
                    and (not str(row.get("phone") or "").strip()
                         or not bool(num(row.get("phone_verified")))):
                return (answer + "\nДля безопасной привязки нажмите «Отправить номер»."), self._contact_keyboard()
            return answer, self._orders_keyboard(chat, row)
        if word in ("статус", "status", "заказ", "order"):
            number = next((w for w in text.split() if w.isdigit()), "")
            if word in ("заказ", "order") and number:
                return self._order_item(chat, row, number), self._menu()
            return self._order_card(chat, row, number)
        if text.startswith("индивидуальн") or word in ("печать", "задача"):
            return self._custom_order(chat, row, raw), self._menu()
        if word in ("телефон", "phone", "номер") or text.startswith("+7") or text.startswith("8 9"):
            # Набранный номер не является доказательством владения им. Сохраняем
            # его только как неподтверждённый; заказы по нему не раскрываются.
            answer = self._save_phone(chat, row, text, verified=False)
            # пока телефон не привязан — предлагаем отправить контакт кнопкой,
            # это одно касание вместо набора номера
            buttons = self._menu() if row.get("phone") else self._contact_keyboard()
            return answer, buttons
        if word in ("оператор", "оператора", "человек", "связаться", "связь",
                    "контакт", "поддержка"):
            return self._contact_operator(chat, row), self._menu()
        contact = str(self._settings().get("company_name", "NOZZA") or "NOZZA")
        # Ждём описание проблемы после оценки «не очень»? Это не «не понял»,
        # а продолжение отзыва — сохраняем комментарий и передаём мастеру.
        pending = self._await_problem.get(chat)
        if pending and time.time() < pending[1]:
            return self._review_comment(chat, row, pending[0], raw), self._menu()
        if pending:
            self._await_problem.pop(chat, None)
        # Ответ на сообщение оператора — это диалог, а не «не понял»: не пугаем
        # покупателя шаблоном и сразу передаём реплику мастеру в inbox.
        if reply:
            return self._operator_reply(chat, row, raw), self._menu()
        answer = ("Не понял сообщение. Напишите «помощь» — покажу, что я умею. "
                  f"Мастерская {contact} ответит и без команд.")
        self._alert_master(chat, row, raw)
        return answer, self._menu()

    def _alert_master(self, chat: str, row: dict, text: str) -> None:
        """Вопрос мимо команд — уведомление команде, чтобы лид не умер в журнале.

        Редкая болтовня не должна превращаться в спам: один чат будит мастера
        не чаще раза в 10 минут.
        """
        last = self._master_alerts.get(chat, 0.0)
        if time.time() - last < 600:
            return
        self._master_alerts[chat] = time.time()
        name = row.get("name") or "Без имени"
        try:
            self.manager.notify_async(
                f"💬 Покупатель ждёт ответа\n{name} · чат {chat}\n"
                f"«{text[:300]}»\nОтветить: кответ {chat} <текст>")
        except Exception:
            pass

    def _contact_operator(self, chat: str, row: dict) -> str:
        """«Оператор» / кнопка связи: явный запрос человека — будим команду.

        В отличие от случайной болтовни (_alert_master), явная просьба связаться
        не троттлится: покупатель сам нажал кнопку и ждёт ответа.
        """
        name = row.get("name") or "Без имени"
        self._funnel(chat, "contact_operator", row)
        try:
            self.manager.notify_async(
                f"👤 Покупатель просит связаться с оператором\n"
                f"{name} · чат {chat}\nОтветить: кответ {chat} <текст>",
                critical=True)
        except Exception:
            pass
        return ("Передал мастерской ✓ Оператор ответит в этом чате. Пока можете "
                "посмотреть «каталог», задать «вопрос-ответ» или просто написать "
                "вопрос текстом — он не потеряется.")

    def _operator_reply(self, chat: str, row: dict, text: str) -> str:
        """Ответ покупателя на сообщение оператора: сразу мастеру, без шаблона."""
        name = row.get("name") or "Без имени"
        self._funnel(chat, "operator_reply", row)
        try:
            self.manager.notify_async(
                f"💬 Ответ покупателя оператору\n{name} · чат {chat}\n"
                f"«{str(text)[:300]}»\nОтветить: кответ {chat} <текст>")
        except Exception:
            pass
        return ("Передал ваш ответ оператору ✓ Ответит в этом чате. "
                "Пока могу показать «каталог» или «вопрос-ответ».")

    # -------------------------------------------------------------- тексты
    def _toggle_wishlist(self, chat: str, nom_id: str) -> str:
        item = self.db.one("SELECT * FROM nomenclature WHERE id=?", (nom_id,))
        if not item or not bool(num(item.get("client_bot_published"), 1)) or num(item.get("archived")):
            return "Эта позиция больше не опубликована."
        existing = self.db.one("SELECT 1 FROM client_wishlist WHERE chat_id=? AND nom_id=?",
                               (chat, nom_id))
        if existing:
            self.db.execute("DELETE FROM client_wishlist WHERE chat_id=? AND nom_id=?", (chat, nom_id))
            self._funnel(chat, "wishlist_remove", source="telegram", nom_id=nom_id)
            return f"«{item.get('name') or 'Позиция'}» убрана из избранного."
        self.db.execute("INSERT OR IGNORE INTO client_wishlist(chat_id,nom_id,created_at) VALUES(?,?,?)",
                        (chat, nom_id, now_iso()))
        self._funnel(chat, "wishlist_add", source="telegram", nom_id=nom_id)
        return f"«{item.get('name') or 'Позиция'}» сохранена в избранном 💛"

    def _wishlist_rows(self, chat: str) -> list[dict]:
        return self.db.query(
            "SELECT n.*, p.price FROM client_wishlist w JOIN nomenclature n ON n.id=w.nom_id"
            " LEFT JOIN (SELECT p.nom_id, p.price FROM prices p JOIN price_types t"
            " ON t.id=p.price_type_id WHERE t.is_base=1"
            " AND (p.variant_id IS NULL OR p.variant_id='')"
            " AND p.rowid=(SELECT p2.rowid FROM prices p2"
            " JOIN price_types t2 ON t2.id=p2.price_type_id"
            " WHERE p2.nom_id=p.nom_id AND t2.is_base=1"
            " AND (p2.variant_id IS NULL OR p2.variant_id='')"
            " ORDER BY datetime(p2.at) DESC,p2.rowid DESC LIMIT 1)) p"
            " ON p.nom_id=n.id WHERE w.chat_id=? AND n.archived=0"
            " ORDER BY datetime(w.created_at) DESC", (chat,))

    def _wishlist_text(self, chat: str) -> str:
        rows = [r for r in self._wishlist_rows(chat) if num(r.get("price")) > 0]
        if not rows:
            return "Избранное пусто. Откройте «каталог» и нажмите 💛 на карточке товара."
        lines = ["💛 Избранное:", ""]
        for i, item in enumerate(rows[:20], 1):
            lines.append(f"{i}. {item.get('name')} — {_money(item.get('price'))}")
        return "\n".join(lines)

    def _wishlist_keyboard(self, chat: str) -> dict:
        rows = self._wishlist_rows(chat)
        keys = [[(f"🛒 {r.get('name')} · {_money(r.get('price'))}"[:60], f"buy:{r['id']}")]
                for r in rows[:8] if num(r.get("price")) > 0]
        keys.append([("🛍 Каталог", "catalog"), ("📦 Мои заказы", "mine")])
        return _keyboard(*keys)

    def _cart_rows(self, chat: str) -> list[dict]:
        """Вернуть живой состав корзины, убирая архивированные карточки."""
        catalog = {str(item.get("id")): item for item in self._catalog_rows()}
        rows = self.db.query(
            "SELECT w.nom_id,w.variant_id,w.qty,w.created_at,w.updated_at,"
            " n.name,n.material,n.grams,n.hours,v.name variant_name,v.color_name"
            " FROM client_bot_cart w JOIN nomenclature n ON n.id=w.nom_id"
            " LEFT JOIN nom_variants v ON v.id=w.variant_id"
            " WHERE w.chat_id=? ORDER BY datetime(w.updated_at) DESC", (chat,))
        result = []
        for row in rows:
            item = catalog.get(str(row.get("nom_id") or ""))
            if not item:
                continue
            variant_id = str(row.get("variant_id") or "")
            price = num(item.get("price"))
            if variant_id:
                variant_price = self.db.one(
                    "SELECT p.price FROM prices p JOIN price_types t ON t.id=p.price_type_id"
                    " WHERE p.variant_id=? AND t.is_base=1 ORDER BY datetime(p.at) DESC,p.rowid DESC LIMIT 1",
                    (variant_id,))
                price = num((variant_price or {}).get("price")) or price
            result.append({**row, "price": price, "item": item,
                           "variant_name": row.get("variant_name") or row.get("color_name") or ""})
        return result

    def _cart_text(self, chat: str) -> str:
        rows = self._cart_rows(chat)
        if not rows:
            return "🛒 Корзина пуста. Откройте «каталог» и добавьте позицию кнопкой «В корзину»."
        total = sum(num(row.get("price")) * num(row.get("qty"), 1) for row in rows)
        lines = ["🛒 Корзина:", ""]
        for index, row in enumerate(rows, 1):
            label = row["item"].get("name") or "Позиция"
            if row.get("variant_name"):
                label += f" · {row['variant_name']}"
            lines.append(f"{index}. {label} ×{num(row.get('qty'), 1):g} — {_money(num(row.get('price')) * num(row.get('qty'), 1))}")
        lines.append(f"\nИтого: {_money(total)}")
        lines.append("Проверьте состав и нажмите «Оформить заявку» — резерв проверим в момент оформления.")
        return "\n".join(lines)

    def _cart_keyboard(self, chat: str) -> dict:
        rows = self._cart_rows(chat)
        keys: list[list[tuple[str, str]]] = []
        for row in rows[:8]:
            label = f"Убрать · {row['item'].get('name') or 'позиция'}"
            if row.get("variant_name"):
                label += f" · {row['variant_name']}"
            keys.append([(label[:58], f"cartremove:{row['nom_id']}:{row.get('variant_id') or '-'}")])
        if rows:
            keys.append([("✅ Оформить заявку", "checkout")])
        keys.append([("🛍 Каталог", "catalog"), ("📦 Мои заказы", "mine")])
        return _keyboard(*keys)

    def _add_to_cart(self, chat: str, nom_id: str, variant_id: str = "") -> str:
        item = next((row for row in self._catalog_rows() if row.get("id") == nom_id), None)
        if not item:
            return "Эта позиция больше не опубликована."
        variant_id = str(variant_id or "").strip()
        if variant_id:
            variant = self.db.one(
                "SELECT id FROM nom_variants WHERE id=? AND nom_id=? AND archived=0",
                (variant_id, nom_id))
            if not variant:
                return "Этот вариант больше недоступен — откройте каталог заново."
        current = self.db.one(
            "SELECT qty FROM client_bot_cart WHERE chat_id=? AND nom_id=? AND variant_id=?",
            (chat, nom_id, variant_id))
        qty = min(100, num((current or {}).get("qty"), 0) + 1)
        stock_qty = num(item.get("qty"))
        if stock_qty > 0:
            try:
                from .stock import Stock
                free = Stock(self.db).qty(nom_id, variant_id=variant_id) - Stock(self.db).reserved(nom_id, variant_id=variant_id)
                if free > 0 and qty > free:
                    return f"В свободном остатке только {free:g} шт. Можно оформить под заказ позже."
            except Exception:
                pass
        stamp = now_iso()
        self.db.execute(
            "INSERT INTO client_bot_cart(chat_id,nom_id,variant_id,qty,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?) ON CONFLICT(chat_id,nom_id,variant_id)"
            " DO UPDATE SET qty=excluded.qty,updated_at=excluded.updated_at",
            (chat, nom_id, variant_id, qty,
             (current or {}).get("created_at") or stamp, stamp))
        self._funnel(chat, "cart_add", source="telegram", nom_id=nom_id,
                     data={"variant_id": variant_id, "qty": qty})
        label = item.get("name") or "Позиция"
        return f"«{label}» добавлена в корзину ✓ Количество: {qty:g}."

    def _remove_from_cart(self, chat: str, nom_id: str, variant_id: str = "") -> str:
        self.db.execute("DELETE FROM client_bot_cart WHERE chat_id=? AND nom_id=? AND variant_id=?",
                        (chat, nom_id, "" if variant_id in ("", "-") else variant_id))
        self._funnel(chat, "cart_remove", source="telegram", nom_id=nom_id)
        return "Позиция убрана из корзины."

    def _checkout_cart(self, chat: str, row: dict) -> str:
        # Сохранение заказа, резервов и очистка корзины должны быть одной
        # критической секцией: параллельный retry не может зарезервировать
        # один и тот же остаток дважды.
        with self.db.transaction():
            return self._checkout_cart_impl(chat, row)

    def _checkout_cart_impl(self, chat: str, row: dict) -> str:
        rows = self._cart_rows(chat)
        if not rows:
            return "Корзина пуста — сначала добавьте позицию из каталога."
        request = f"tg:{self._current_update_id}" if self._current_update_id else ""
        if request:
            existing = self.db.one("SELECT * FROM orders WHERE client_request_id=?", (request,))
            if existing:
                self.db.execute("DELETE FROM client_bot_cart WHERE chat_id=?", (chat,))
                return f"Заявка уже принята ✓ Номер — №{existing.get('number')}."
        items = []
        for item in rows:
            label = item["item"].get("name") or "Позиция"
            if item.get("variant_name"):
                label += f" · {item['variant_name']}"
            items.append({
                "nom_id": item["nom_id"], "variant_id": item.get("variant_id") or "",
                "name": label, "qty": num(item.get("qty"), 1),
                "price": num(item.get("price")), "grams": num(item["item"].get("grams")),
                "hours": num(item["item"].get("hours")),
                "note": "из корзины",
            })
        order = self.manager.repo.save_order({
            "product": ", ".join(str(item["name"]) for item in items)[:240],
            "customer_name": row.get("name") or "Покупатель", "phone": row.get("phone") or "",
            "messenger": f"tg:@{row.get('username')}" if row.get("username") else f"tg:{chat}",
            "channel": "telegram", "client_source": row.get("source") or "telegram",
            "client_request_id": request, "status": "new", "qty": sum(num(item["qty"]) for item in items),
            "nom_id": items[0]["nom_id"] if len(items) == 1 else "",
            "client_variant_id": items[0].get("variant_id") if len(items) == 1 else "",
            "items": items, "notes": "Заказ из корзины клиентского бота",
        })
        self._link_order(chat, row, order, source="telegram", update_id=self._current_update_id)
        # Резервируем готовые варианты после проверки свободного остатка. Если
        # склад не заведён, заказ остаётся «под заказ» и не теряется.
        reserved_any = False
        try:
            from .stock import Stock
            stock = Stock(self.db)
            for item in items:
                try:
                    variant_id = str(item.get("variant_id") or "")
                    stock_qty = stock.qty(item["nom_id"], variant_id=variant_id)
                    free = stock_qty - stock.reserved(item["nom_id"], variant_id=variant_id)
                    if free >= num(item["qty"], 1) and free > 0:
                        warehouse_sql = ("SELECT warehouse_id FROM stock_moves WHERE nom_id=?"
                                         + (" AND variant_id=?" if variant_id else "")
                                         + " GROUP BY warehouse_id ORDER BY SUM(qty) DESC LIMIT 1")
                        warehouse_params = ((item["nom_id"], variant_id)
                                            if variant_id else (item["nom_id"],))
                        warehouse = self.db.one(warehouse_sql, warehouse_params) or {}
                        stock.reserve(item["nom_id"], num(item["qty"], 1), order["id"],
                                      warehouse.get("warehouse_id") or "", "резерв из корзины", variant_id)
                        reserved_any = True
                except Exception:
                    # Отдельная позиция может быть «под заказ», остальные не
                    # должны из-за этого исчезать из корзины/заказа.
                    continue
        except Exception:
            pass
        if reserved_any:
            self.db.execute("UPDATE orders SET reserved=1 WHERE id=?", (order["id"],))
        self.db.execute("DELETE FROM client_bot_cart WHERE chat_id=?", (chat,))
        self._funnel(chat, "cart_checkout", row, order_id=order.get("id") or "",
                     data={"count": len(items)})
        try:
            self.manager.notify_async(
                f"🛒 Заказ из корзины клиентского бота\n№{order.get('number')} · {order.get('product')}",
                critical=True)
        except Exception:
            pass
        return f"Заявка из корзины принята ✓\nНомер заказа — №{order.get('number')}.\nМастер подтвердит цену и срок."

    def _toggle_marketing(self, chat: str, row: dict) -> str:
        if not bool(self._settings().get("client_bot_marketing_enabled", False)):
            return "Рассылки пока отключены владельцем мастерской. Сервисные статусы заказа доступны всегда."
        current = bool(num(row.get("marketing_opt_in")))
        value = 0 if current else 1
        self.db.execute("UPDATE client_chats SET marketing_opt_in=? WHERE chat_id=?", (value, chat))
        row["marketing_opt_in"] = value
        self._funnel(chat, "marketing_opt_in" if value else "marketing_opt_out", row)
        return ("Добровольные рассылки включены 📣 Их можно отключить командой «отписаться»."
                if value else "Добровольные рассылки отключены. Сервисные статусы заказа остаются включены.")

    def _quote_response(self, chat: str, row: dict, raw: str) -> str:
        answer = str(raw or "").strip().lower().replace("ё", "е")
        if answer not in {"да", "ок", "окей", "согласен", "согласна", "подтверждаю" ,
                          "нет", "не согласен", "не согласна"}:
            return ""
        order = next((item for item in self._linked_orders(chat, row)
                      if str(item.get("client_quote_status") or "") in {"sent", "requested"}
                      and num(item.get("price")) > 0), None)
        if not order:
            return ""
        accepted = answer not in {"нет", "не согласен", "не согласна"}
        status = "accepted" if accepted else "declined"
        self.db.execute(
            "UPDATE orders SET client_quote_status=?,client_quote_accepted_at=? WHERE id=?",
            (status, now_iso(), order["id"]))
        self._funnel(chat, "quote_accepted" if accepted else "quote_declined",
                     source=row.get("source") or "telegram", order_id=order["id"],
                     data={"amount": num(order.get("price"))})
        try:
            self.manager.notify_async(
                f"{'✅' if accepted else '↩️'} Покупатель {'согласовал' if accepted else 'не согласовал'} цену"
                f" по заказу №{order.get('number')} · чат {chat}", critical=True)
        except Exception:
            pass
        return (f"Цена и срок по заказу №{order.get('number')} отмечены как согласованные ✓"
                if accepted else
                f"Передал мастеру: цена по заказу №{order.get('number')} требует обсуждения.")

    def _quote_action(self, chat: str, row: dict, order_id: str,
                      accepted: bool) -> tuple[str, dict | None]:
        order = self._own_order(chat, row, order_id)
        if not order or str(order.get("client_quote_status") or "") not in {"sent", "requested"}:
            return "Эта заявка больше не ждёт согласования.", self._menu()
        answer = self._quote_response(chat, row, "да" if accepted else "нет")
        return answer, self._menu()

    def _toggle_notifications(self, chat: str, row: dict) -> str:
        current = bool(num(row.get("status_notify"), 1))
        value = 0 if current else 1
        self.db.execute("UPDATE client_chats SET status_notify=? WHERE chat_id=?", (value, chat))
        row["status_notify"] = value
        self._funnel(chat, "status_notifications_on" if value else "status_notifications_off", row)
        return ("Уведомления о статусе включены 🔔" if value else
                "Уведомления о статусе выключены. Проверить заказ можно в «мои заказы»." )

    def _repeat_order(self, chat: str, row: dict, order_id: str) -> str:
        order = self._own_order(chat, row, order_id)
        if not order:
            return "Это не ваш заказ."
        request = self._current_update_id
        if request:
            key = f"repeat:{chat}:{request}"
            existing = self.db.one("SELECT entity_id FROM idempotency_keys WHERE request_id=?", (key,))
            if existing:
                repeat = self.db.one("SELECT * FROM orders WHERE id=?", (existing.get("entity_id"),)) or {}
                return f"Повтор уже создан — №{repeat.get('number') or '—'}."
        repeat = self.manager.repo.duplicate_order(order_id)
        self._link_order(chat, row, repeat, source="telegram", update_id=request)
        if request:
            self.db.execute("INSERT OR IGNORE INTO idempotency_keys(request_id,kind,entity_id,response,created_at) VALUES(?,?,?,?,?)",
                            (f"repeat:{chat}:{request}", "client_repeat", repeat.get("id") or "", "{}", now_iso()))
        self._funnel(chat, "repeat_order", row, order_id=repeat.get("id") or "")
        return f"Повтор заказа создан ✓ Новый номер — №{repeat.get('number')}. Мастер подтвердит срок."

    def _catalog_rows(self) -> list[dict]:
        """Позиции витрины: товары номенклатуры с ценой, по порядку."""
        try:
            from .nomenclature import Nomenclature
            items = Nomenclature(self.db).items(kind="product")
        except Exception:
            items = []
        return [item for item in items
                if num(item.get("price")) > 0
                and not bool(num(item.get("archived")))
                and bool(num(item.get("client_bot_published"), 1))]

    def text_catalog(self, page: int = 1) -> str:
        if not bool(self._settings().get("client_bot_catalog", True)):
            return ("Каталог по запросу отключён. Напишите, что ищете — "
                    "мастер подскажет и посчитает.")
        rows = self._catalog_rows()
        if not rows:
            return ("На полке сейчас нет опубликованных позиций. "
                    "Напишите «индивидуальный» + задача — посчитаем печать под вас.")
        pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(max(1, page), pages)
        start = (page - 1) * PAGE_SIZE
        chunk = rows[start:start + PAGE_SIZE]
        lines = [f"🛍 Каталог NOZZA · страница {page} из {pages}:", ""]
        for i, item in enumerate(chunk, start + 1):
            qty = num(item.get("qty"))
            tail = " · есть на полке" if qty > 0 else " · под заказ"
            lines.append(f"{i}. {item.get('name')} — {_money(item.get('price'))}{tail}")
        lines += ["", f"Показаны {start + 1}–{start + len(chunk)} из {len(rows)}. "
                  "Заказ: «заказ <номер>» или кнопкой-карточкой."]
        return "\n".join(lines)

    def _catalog_keyboard(self, page: int = 1) -> dict:
        # Витрину могли выключить настройкой — тогда кнопок заказа не даём,
        # иначе каталог «закрыт», а кнопки под ним всё ещё продают.
        if not bool(self._settings().get("client_bot_catalog", True)):
            return self._menu()
        rows = self._catalog_rows()
        if not rows:
            return self._menu()
        pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(max(1, page), pages)
        start = (page - 1) * PAGE_SIZE
        keys: list[list[dict]] = []
        for item in rows[start:start + PAGE_SIZE]:
            qty = num(item.get("qty"))
            tail = " · есть" if qty > 0 else " · под заказ"
            keys.append([{"text": f"🛍 {item.get('name')} · "
                                  f"{_money(item.get('price'))}{tail}"[:64],
                          "callback_data": f"item:{item['id']}:{page}"}])
        if pages > 1:
            nav: list[dict] = []
            if page > 1:
                nav.append({"text": "⬅️", "callback_data": f"catalog:{page - 1}"})
            nav.append({"text": f"{page}/{pages}", "callback_data": f"catalog:{page}"})
            if page < pages:
                nav.append({"text": "➡️", "callback_data": f"catalog:{page + 1}"})
            keys.append(nav)
        keys.append([{"text": "📦 Мои заказы", "callback_data": "mine"},
                     {"text": "❓ Вопрос-ответ", "callback_data": "faq"}])
        share = self._share_button()
        if share:
            keys.append([share])
        return {"inline_keyboard": keys}

    def _share_button(self) -> dict | None:
        """Кнопка «Поделиться»: системный экран пересылки со ссылкой на бота."""
        username = self._username()
        if not username:
            return None
        link = f"https://t.me/{username}"
        url = ("https://t.me/share/url?url=" + urllib.parse.quote(link, safe="")
               + "&text=" + urllib.parse.quote(SHARE_TEXT, safe=""))
        return {"text": "📤 Поделиться каталогом", "url": url}

    def _item_card_payload(self, nom_id: str,
                           page: int = 1) -> tuple[str, bytes, dict]:
        """Текст, фото и кнопки карточки товара каталога."""
        item = self.db.one("SELECT * FROM nomenclature WHERE id=?", (nom_id,))
        rows = self._catalog_rows()
        catalog_item = next((r for r in rows if r.get("id") == nom_id), None)
        if not item or not catalog_item:
            return "Эта карточка больше не опубликована.", b"", {"inline_keyboard": [[{"text": "🛍 Каталог", "callback_data": "catalog"}]]}
        index = next((i for i, r in enumerate(rows, 1) if r["id"] == nom_id), 0)
        price = num(catalog_item.get("price"))
        qty = num(catalog_item.get("qty"))
        caption = [f"🛍 {item.get('name') if item else 'Товар'}"]
        caption.append(f"Цена: {_money(price)}")
        caption.append("Есть на полке" if qty > 0 else "Под заказ")
        if (item or {}).get("material"):
            caption.append(f"Материал: {item.get('material')}")
        if (item or {}).get("client_bot_description"):
            caption.append(str(item.get("client_bot_description"))[:500])
        if index:
            caption.append(f"Заказ: кнопкой ниже или «заказ {index}»")
        variants = self.db.query(
            "SELECT v.id,v.name,v.color_name,v.size,"
            " COALESCE((SELECT p.price FROM prices p JOIN price_types t ON t.id=p.price_type_id"
            " WHERE p.variant_id=v.id AND t.is_base=1 ORDER BY datetime(p.at) DESC LIMIT 1),0) price,"
            " COALESCE((SELECT SUM(m.qty) FROM stock_moves m WHERE m.variant_id=v.id),0) qty"
            " FROM nom_variants v WHERE v.nom_id=? AND v.archived=0"
            " ORDER BY v.position,v.name LIMIT 8", (nom_id,))
        if variants:
            caption.append("Варианты:")
            for variant in variants:
                label = str(variant.get("name") or variant.get("color_name") or variant.get("size") or "вариант")
                variant_text = f"{label} — {_money(num(variant.get('price')) or price)}"
                caption.append(variant_text + (" · есть" if num(variant.get("qty")) > 0 else " · под заказ"))
        keys = [
            [{"text": "🛒 Заказать", "callback_data": f"buy:{nom_id}"},
             {"text": "🛒 В корзину", "callback_data": f"cart:{nom_id}"}],
            [{"text": "💛 В избранное", "callback_data": f"wish:{nom_id}"}],
        ]
        for variant in variants:
            label = str(variant.get("name") or variant.get("color_name") or variant.get("size") or "вариант")
            keys.append([{"text": f"В корзину · {label}"[:60],
                          "callback_data": f"cartv:{nom_id}:{variant['id']}"}])
        keys.append([{"text": "⬅️ Каталог", "callback_data": f"catalog:{page}"}])
        raw = b""
        if (item or {}).get("photo"):
            from .config import PHOTO_DIR
            path = PHOTO_DIR / str(item["photo"])
            if path.is_file():
                try:
                    raw = path.read_bytes()
                except Exception:
                    raw = b""
        return "\n".join(caption), raw, {"inline_keyboard": keys}

    def _send_item_card(self, chat: str, data: str) -> None:
        """Отправить карточку товара (фото, если есть) без текстового дубля."""
        parts = data.split(":")
        nom_id = parts[1] if len(parts) > 1 else ""
        page = max(1, int(num(parts[2]))) if len(parts) > 2 else 1
        caption, raw, buttons = self._item_card_payload(nom_id, page)
        if raw:
            self._send_photo(chat, caption, raw, buttons)
        else:
            self._reply(chat, caption, buttons)

    def text_faq(self) -> str:
        custom = str(self._settings().get("client_bot_faq") or "").strip()
        return custom or FAQ_TEXT

    def _orders_keyboard(self, chat: str, row: dict,
                         orders: list[dict] | None = None) -> dict:
        """К каждому заказу — кнопка-карточка: покупатель касается номера и
        видит статус со сроком, без набора «статус 1001»."""
        if orders is None:
            orders = self._linked_orders(chat, row)
        keys = [[(f"№{o.get('number')} · {self._status_label(o)}"[:60],
                 f"status:{o['number']}")]
                for o in orders[:6] if o.get("number")]
        keys.append([("🛍 Каталог", "catalog"), ("❔ Помощь", "help")])
        return _keyboard(*keys)

    def _order_item(self, chat: str, row: dict, nom_id: str) -> str:
        """Заказать товар с id/номером и безопасно пережить повтор callback."""
        parts = str(nom_id or "").split(":")
        nom_key = parts[0]
        variant_id = ""
        qty = 1
        if len(parts) > 2 and parts[1] == "variant":
            variant_id = parts[2]
        elif len(parts) > 1:
            qty = max(1, min(100, int(num(parts[1], 1))))
        if self._current_update_id:
            existing = self.db.one(
                "SELECT o.* FROM client_orders l JOIN orders o ON o.id=l.order_id"
                " WHERE l.chat_id=? AND l.update_id=? LIMIT 1",
                (chat, self._current_update_id))
            if existing:
                return f"Этот заказ уже принят ✓ Номер — №{existing.get('number')}."
        rows = self._catalog_rows()
        item = next((r for r in rows if r["id"] == nom_key), None)
        if item is None and nom_key.isdigit():
            index = int(nom_key)
            if 1 <= index <= len(rows):
                item = rows[index - 1]
        if not item:
            return ("Такой позиции в каталоге нет. Напишите «каталог» и "
                    "закажите по номеру из списка (например, «заказ 2»). "
                    "Если вы про номер заказа — «статус <номер>».")
        variant = None
        if variant_id:
            variant = self.db.one(
                "SELECT * FROM nom_variants WHERE id=? AND nom_id=? AND archived=0",
                (variant_id, item.get("id")))
            if not variant:
                return "Этот вариант больше недоступен — откройте карточку товара заново."
        display_name = item.get("name") or ""
        if variant:
            display_name += " · " + str(variant.get("name") or variant.get("color_name") or variant.get("size") or "вариант")
        variant_price = self.db.one(
            "SELECT p.price FROM prices p JOIN price_types t ON t.id=p.price_type_id"
            " WHERE p.variant_id=? AND t.is_base=1 ORDER BY datetime(p.at) DESC LIMIT 1",
            (variant_id,)) if variant_id else None
        unit_price = num((variant_price or {}).get("price")) or num(item.get("price"))
        order = self.manager.repo.save_order({
            "product": display_name,
            "customer_name": row.get("name") or "Покупатель",
            "phone": row.get("phone") or "",
            "messenger": (f"tg:@{row.get('username')}" if row.get("username")
                          else f"tg:{chat}"),
            "channel": "telegram", "client_source": row.get("source") or "telegram",
            "client_request_id": f"tg:{self._current_update_id}" if self._current_update_id else "",
            "status": "new", "qty": qty,
            "material": item.get("material") or "",
            "color": str((variant or {}).get("color_name") or ""),
            "client_variant_id": variant_id,
            "grams": num((variant or {}).get("grams")) or num(item.get("grams")),
            "hours": num((variant or {}).get("hours")) or num(item.get("hours")),
            "price": round(unit_price * qty, 2),
            "notes": "Заказ из клиентского бота",
            "nom_id": item.get("id"),
        })
        number = order.get("number") or ""
        self._link_order(chat, row, order, source="telegram")
        self._maybe_reserve(order, item, qty)
        self._notified[(chat, order.get("id", ""))] = "new"
        self.db.add_event("lead", "Заказ из клиентского бота",
                          f"№{number} · {display_name}",
                          data={"order_id": order.get("id"), "chat_id": chat,
                                "source": row.get("source") or "telegram",
                                "variant_id": variant_id})
        self._funnel(chat, "order_created", row, order_id=order.get("id") or "",
                     nom_id=item.get("id") or "")
        try:
            self.manager.notify_async(
                f"🛎 Новый заказ из клиентского бота\n№{number} · "
                f"{display_name} · {_money(unit_price * qty)}",
                critical=True)
        except Exception:
            pass
        return (f"Заказ принят ✓\nНомер заказа — №{number}.\n"
                "Мастер подтвердит цену и срок. Статус: «мои заказы».")

    def _maybe_reserve(self, order: dict, item: dict, qty: int) -> None:
        """Зарезервировать готовый товар, если реальный склад это позволяет."""
        if num(item.get("qty")) < qty or not order.get("nom_id"):
            return
        try:
            from .stock import Stock
            stock = Stock(self.db)
            variant_id = str(order.get("client_variant_id") or "")
            warehouse_sql = ("SELECT warehouse_id, SUM(qty) balance FROM stock_moves WHERE nom_id=?"
                             + (" AND variant_id=?" if variant_id else "")
                             + " GROUP BY warehouse_id HAVING balance>=? ORDER BY balance DESC LIMIT 1")
            warehouse_params = ((order["nom_id"], variant_id, qty)
                                if variant_id else (order["nom_id"], qty))
            warehouse = self.db.one(warehouse_sql, warehouse_params) or {}
            warehouse_id = warehouse.get("warehouse_id") or ""
            variant_id = str(order.get("client_variant_id") or "")
            if (stock.qty(order["nom_id"], warehouse_id, variant_id)
                    - stock.reserved(order["nom_id"], warehouse_id, variant_id) < qty):
                return
            stock.reserve(order["nom_id"], qty, order["id"], warehouse_id,
                          "резерв из клиентского Telegram-бота", variant_id)
            self.db.execute("UPDATE orders SET reserved=1,warehouse_id=?,updated_at=? WHERE id=?",
                            (warehouse_id, now_iso(), order["id"]))
        except Exception:
            # Каталог остаётся доступен «под заказ», даже если склад не настроен.
            return

    def _custom_order(self, chat: str, row: dict, raw: str) -> str:
        """«индивидуальный …» — сохранить заявку с ценой/сроком мастера."""
        task = _re.sub(r"^\s*индивидуальн\w*\s*[:\-]?\s*", "", raw.strip(),
                       flags=_re.IGNORECASE).strip()
        if not task:
            self.db.upsert("client_bot_drafts", {
                "chat_id": chat, "step": "description", "data": "{}",
                "created_at": now_iso(), "updated_at": now_iso()}, key="chat_id")
            self._funnel(chat, "custom_started", row)
            return ("Индивидуальная заявка — шаг 1 из 3. Опишите, что напечатать. "
                    "Черновик сохранён; можно вернуться после перезапуска бота.")
        return self._create_custom_order(chat, row, task)

    def _create_custom_order(self, chat: str, row: dict, task: str,
                             *, dimensions: str = "", purpose: str = "") -> str:
        if self._current_update_id:
            existing = self.db.one(
                "SELECT o.* FROM client_orders l JOIN orders o ON o.id=l.order_id"
                " WHERE l.chat_id=? AND l.update_id=? LIMIT 1",
                (chat, self._current_update_id))
            if existing:
                return f"Эта заявка уже принята ✓ Номер — №{existing.get('number')}."
        notes = "Заявка из клиентского бота (индивидуальная)"
        if dimensions:
            notes += f"\nРазмеры: {dimensions[:300]}"
        if purpose:
            notes += f"\nНазначение: {purpose[:300]}"
        order = self.manager.repo.save_order({
            "product": task[:120],
            "customer_name": row.get("name") or "Покупатель",
            "phone": row.get("phone") or "",
            "messenger": (f"tg:@{row.get('username')}" if row.get("username")
                          else f"tg:{chat}"),
            "channel": "telegram", "client_source": row.get("source") or "telegram",
            "client_request_id": f"tg:{self._current_update_id}" if self._current_update_id else "",
            "client_quote_status": "requested", "status": "new", "qty": 1,
            "notes": notes,
        })
        number = order.get("number") or ""
        self._link_order(chat, row, order, source="telegram")
        self._notified[(chat, order.get("id", ""))] = "new"
        self.db.add_event("lead", "Заявка из клиентского бота",
                          f"№{number} · {task[:80]}",
                          data={"order_id": order.get("id"), "chat_id": chat,
                                "source": row.get("source") or "telegram"})
        self._funnel(chat, "custom_order_created", row, order_id=order.get("id") or "")
        try:
            self.manager.notify_async(
                f"🛎 Индивидуальная заявка из бота\n№{number} · {task[:200]}",
                critical=True)
        except Exception:
            pass
        return ("Заявка принята ✓ Номер — №"
                f"{number}.\nДля расчёта пришлите сюда размеры (Д×Ш×В), "
                "назначение и фото/эскиз. Мастер ответит с ценой и сроком.")

    def _draft_reply(self, chat: str, row: dict, raw: str) -> tuple[str, dict | None]:
        draft = self.db.one("SELECT * FROM client_bot_drafts WHERE chat_id=?", (chat,))
        if not draft:
            return "", None
        try:
            data = json.loads(draft.get("data") or "{}")
        except (TypeError, ValueError):
            data = {}
        step = draft.get("step") or "description"
        text = raw.strip()
        if step == "description":
            data["description"] = text[:500]
            next_step = "dimensions"
            answer = "Шаг 2 из 3. Укажите размеры или напишите «нет»."
        elif step == "dimensions":
            data["dimensions"] = text[:300]
            next_step = "purpose"
            answer = "Шаг 3 из 3. Для чего нужна деталь и какой материал предпочтительнее?"
        else:
            data["purpose"] = text[:300]
            answer = self._create_custom_order(chat, row, data.get("description") or text,
                                                dimensions=data.get("dimensions") or "",
                                                purpose=data.get("purpose") or "")
            self.db.execute("DELETE FROM client_bot_drafts WHERE chat_id=?", (chat,))
            return answer, self._menu()
        self.db.execute("UPDATE client_bot_drafts SET step=?,data=?,updated_at=? WHERE chat_id=?",
                        (next_step, json.dumps(data, ensure_ascii=False), now_iso(), chat))
        return answer, self._menu()

    def _save_phone(self, chat: str, row: dict, text: str,
                    *, verified: bool = False) -> str:
        match = _re.search(r"(\+?\d[\d\s()\-]{8,17}\d)", text)
        if not match:
            return ("Формат: «телефон +7 978 000-00-00» — или нажмите "
                    "«Отправить номер». По номеру найдутся заказы, "
                    "оформленные на полке и в переписке.")
        phone = _re.sub(r"[^\d+]", "", match.group(1))
        self.db.execute("UPDATE client_chats SET phone=?,phone_verified=? WHERE chat_id=?",
                        (phone, 1 if verified else 0, chat))
        row["phone"] = phone
        row["phone_verified"] = 1 if verified else 0
        if verified:
            self._funnel(chat, "phone_verified", row)
            return f"Телефон {phone} привязан. Напишите «мои заказы» — покажу всё."
        self._funnel(chat, "phone_submitted", row)
        return (f"Номер {phone} сохранён, но заказы пока не привязаны: "
                "для безопасной проверки нажмите «Отправить номер».\n"
                "После этого напишите «мои заказы».")

    def _linked_orders(self, chat: str, row: dict) -> list[dict]:
        """Заказы чата: прямые ссылки + совпадение по телефону."""
        orders: dict[str, dict] = {}
        for link in self.db.query(
                "SELECT order_id FROM client_orders WHERE chat_id=?", (chat,)):
            order = self.db.one("SELECT * FROM orders WHERE id=?",
                                (link["order_id"],))
            if order:
                orders[order["id"]] = order
        phone = str(row.get("phone") or "").strip()
        if phone and bool(num(row.get("phone_verified"))):
            # Телефоны в заказах пишут по-разному (+7…, 8…, с дефисами),
            # поэтому сравниваем последние 10 цифр в Python, а не через LIKE.
            digits = _re.sub(r"\D", "", phone)[-10:]
            if len(digits) == 10:
                for order in self.db.query(
                        "SELECT * FROM orders WHERE phone IS NOT NULL AND phone!=''"
                        " ORDER BY datetime(created_at) DESC LIMIT 50"):
                    order_digits = _re.sub(r"\D", "", str(order.get("phone") or ""))
                    if order_digits.endswith(digits):
                        orders.setdefault(order["id"], order)
        return sorted(orders.values(),
                      key=lambda o: str(o.get("created_at") or ""), reverse=True)

    def _status_label(self, order: dict) -> str:
        status = self.db.one("SELECT name FROM statuses WHERE id=?",
                             (order.get("status") or "",))
        return (status or {}).get("name") or str(order.get("status") or "новый")

    def text_my_orders(self, chat: str, row: dict) -> str:
        orders = self._linked_orders(chat, row)
        if not orders:
            return ("Ваших заказов не нашёл. Если заказывали на полке или в "
                    "переписке — пришлите «телефон +7…» и повторите.")
        lines = ["📦 Ваши заказы:", ""]
        for order in orders[:10]:
            due = f" · к {str(order.get('due') or '')[:10]}" if order.get("due") else ""
            lines.append(f"№{order.get('number')} · {order.get('product')}"
                         f" — {self._status_label(order)}{due}")
        if len(orders) > 10:
            lines.append(f"…и ещё {len(orders) - 10}")
        lines.append("\nПодробнее: «статус <номер>».")
        return "\n".join(lines)

    def text_order_status(self, chat: str, row: dict, number: str) -> str:
        if not number:
            return "Формат: «статус 1001» — номер заказа из подтверждения."
        order = self.db.one("SELECT * FROM orders WHERE number=?", (number,))
        if not order:
            return f"Заказ №{number} не найден. Проверьте номер из подтверждения."
        allowed = any(o["id"] == order["id"] for o in self._linked_orders(chat, row))
        if not allowed:
            return ("Это не ваш заказ. Если вы заказывали его на полке — "
                    "пришлите «телефон +7…», привяжу номер.")
        due = f"\nОжидаем к: {str(order.get('due'))[:10]}" if order.get("due") else ""
        qty = num(order.get("qty"))
        return (f"Заказ №{order.get('number')}\n{order.get('product')}\n"
                f"Статус: {self._status_label(order)}"
                + (f"\nКоличество: {qty:g}" if qty > 1 else "") + due)

    def _order_card(self, chat: str, row: dict,
                    number: str) -> tuple[str, dict | None]:
        """Карточка своего заказа: текст статуса + кнопки оплаты и трекинга."""
        text = self.text_order_status(chat, row, number)
        order = self.db.one("SELECT * FROM orders WHERE number=?", (number or "",))
        if not order or not any(o["id"] == order["id"]
                                for o in self._linked_orders(chat, row)):
            return text, self._menu()
        return text, self._order_card_keyboard(order)

    def _order_card_keyboard(self, order: dict) -> dict:
        """Кнопки карточки заказа: оплатить (если есть реквизиты и долг),
        страница статуса (если задан публичный адрес панели) и меню."""
        settings = self._settings()
        keys: list[list[dict]] = []
        extra: list[dict] = []
        due = max(0.0, num(order.get("price")) - num(order.get("discount")) -
                   max(num(order.get("paid")), num(order.get("prepaid"))))
        pay_info = str(settings.get("client_bot_pay_info") or "").strip()
        pay_qr = str(settings.get("client_bot_pay_qr") or "").strip()
        can_open_qr = pay_qr.startswith(("http://", "https://", "tg://"))
        if not self._is_final(order):
            # В статусе «Ждём предоплату» кнопка показывается всегда — покупатель
            # должен видеть способы оплаты, даже если реквизиты ещё настраивают.
            if str(order.get("status") or "") == "prepay":
                extra.append({"text": "💳 Способы оплаты", "callback_data":
                              f"pay:{order['id']}"})
            elif due > 0 and (pay_info or can_open_qr):
                extra.append({"text": "💳 Оплатить", "callback_data":
                              f"pay:{order['id']}"})
        track = str(settings.get("client_bot_track_url") or "").strip()
        if track and order.get("number"):
            token = self._track_token(order.get("id") or "")
            url = (track.rstrip("/") + "/track?number="
                   + urllib.parse.quote(str(order["number"]), safe=""))
            if token:
                url += "&token=" + urllib.parse.quote(token, safe="")
            extra.append({"text": "🔗 Статус онлайн", "url": url})
        if extra:
            keys.append(extra)
        if str(order.get("client_quote_status") or "") in {"sent", "requested"} and num(order.get("price")) > 0:
            keys.append([
                {"text": "✅ Согласен", "callback_data": f"quote_yes:{order['id']}"},
                {"text": "↩️ Обсудить", "callback_data": f"quote_no:{order['id']}"},
            ])
        keys.append([{"text": "🛍 Каталог", "callback_data": "catalog"},
                     {"text": "📦 Мои заказы", "callback_data": "mine"}])
        return {"inline_keyboard": keys}

    def _own_order(self, chat: str, row: dict, order_id: str) -> dict | None:
        order = self.db.one("SELECT * FROM orders WHERE id=?", (order_id,))
        if order and any(o["id"] == order["id"]
                         for o in self._linked_orders(chat, row)):
            return order
        return None

    def _payment_purpose(self, number: str) -> str:
        purpose = str(self._settings().get("client_bot_payment_purpose") or "").strip()
        purpose = purpose.replace("{номер заказа}", str(number or ""))
        purpose = purpose.replace("{number}", str(number or ""))
        return purpose or f"NOZZA {number}"

    def _pay_card(self, chat: str, row: dict,
                  order_id: str) -> tuple[str, dict | None]:
        """«Оплатить»: реквизиты владельца + сумма и код платежа.

        Никакого эквайринга: мастер сверяет поступление сам, бот только
        передаёт покупателю публичные реквизиты и комментарий платежа.
        """
        order = self._own_order(chat, row, order_id)
        if not order:
            return ("Это не ваш заказ — оплата доступна в его карточке.",
                    self._menu())
        settings = self._settings()
        pay_info = str(settings.get("client_bot_pay_info") or "").strip()
        qr = str(settings.get("client_bot_pay_qr") or "").strip()
        if self._is_final(order):
            return (f"Заказ №{order.get('number')} уже закрыт — оплата не нужна.",
                    self._menu())
        number = order.get("number")
        is_prepay = str(order.get("status") or "") == "prepay"
        if not pay_info and not qr.startswith(("http://", "https://", "tg://")):
            if is_prepay:
                return (f"Реквизиты предоплаты по заказу №{number} ещё не "
                        "настроены. Мастер напишет способ оплаты в этом чате.",
                        self._menu())
            return ("Реквизиты оплаты ещё не настроены. Мастер согласует "
                    "способ оплаты при выдаче.", self._menu())
        due = max(0.0, num(order.get("price")) - num(order.get("discount")) -
                   max(num(order.get("paid")), num(order.get("prepaid"))))
        if due <= 0:
            if is_prepay:
                return (f"Сумма предоплаты по заказу №{number} уточняется — "
                        "мастер напишет реквизиты в этом чате.", self._menu())
            return "По этому заказу задолженности уже нет — повторная оплата не нужна.", self._menu()
        purpose = self._payment_purpose(number)
        pay_details = pay_info or "Откройте QR СБП кнопкой ниже."
        title = (f"💳 Предоплата по заказу №{number}"
                 if is_prepay else f"💳 Заказ №{number}")
        text = (f"{title} — к оплате {_money(due)}\n\n"
                f"{pay_details}\n\nВ назначении перевода напишите: {purpose}\n"
                "После перевода нажмите «✅ Я оплатил» — мастер сверит поступление "
                "и подтвердит оплату вручную.")
        keys = {"inline_keyboard": [
            [{"text": "✅ Я оплатил", "callback_data": f"paid:{order['id']}"}],
        ]}
        qr = str(self._settings().get("client_bot_pay_qr") or "").strip()
        if qr.startswith(("http://", "https://", "tg://")):
            keys["inline_keyboard"].append([{"text": "▣ Открыть QR СБП", "url": qr}])
        keys["inline_keyboard"].append(
            [{"text": "⬅️ К заказу", "callback_data": f"status:{number}"}])
        return text, keys

    def _paid_notice(self, chat: str, row: dict,
                     order_id: str) -> tuple[str, dict | None]:
        """Создать ручную заявку на сверку платежа, не проводя деньги автоматически."""
        order = self._own_order(chat, row, order_id)
        if not order:
            return "Это не ваш заказ.", self._menu()
        number = order.get("number")
        due = max(0.0, num(order.get("price")) - num(order.get("discount")) -
                   max(num(order.get("paid")), num(order.get("prepaid"))))
        if due <= 0:
            return "По этому заказу задолженности уже нет — повторное подтверждение не нужно.", self._menu()
        request_id = f"tg:{self._current_update_id}" if self._current_update_id else uid("pay-intent")
        existing = self.db.one(
            "SELECT * FROM client_payment_intents WHERE order_id=? AND chat_id=?"
            " AND status IN ('pending','confirmed') ORDER BY created_at DESC LIMIT 1",
            (order_id, chat))
        if existing:
            if existing.get("status") == "confirmed":
                return "Оплата уже подтверждена мастером ✓", self._menu()
            return ("Сообщение уже передано мастеру ✓ Заявка на сверку ожидает "
                    "ручного подтверждения."), self._menu()
        intent_id = uid("payint")
        purpose = self._payment_purpose(number)
        try:
            self.db.execute(
                "INSERT INTO client_payment_intents"
                "(id,order_id,chat_id,request_id,amount,currency,purpose,status,created_at,updated_at)"
                " VALUES(?,?,?,?,?,'RUB',?,'pending',?,?)",
                (intent_id, order_id, chat, request_id, round(due, 2), purpose,
                 now_iso(), now_iso()))
        except Exception:
            # Гонка двух callback-активаций: уникальный pending intent победил.
            existing = self.db.one(
                "SELECT * FROM client_payment_intents WHERE order_id=? AND chat_id=?"
                " AND status='pending' LIMIT 1", (order_id, chat))
            if existing:
                return "Сообщение уже передано мастеру ✓ Заявка на сверку ожидает ручного подтверждения.", self._menu()
            raise
        try:
            self.manager.notify_async(
                f"💳 Покупатель сообщил об оплате\n№{number} · {_money(due)}\n"
                f"Чат {chat} · заявка {intent_id}\n"
                "Сверьте поступление и подтвердите в панели или командой «оплата подтвердить "
                f"{number}»", critical=True)
        except Exception:
            pass
        self.db.add_event("lead", "Покупатель сообщил об оплате",
                          f"№{number}", data={"order_id": order["id"],
                                              "chat_id": chat, "intent_id": intent_id,
                                              "amount": round(due, 2)})
        self._funnel(chat, "payment_submitted", row, order_id=order_id,
                     data={"intent_id": intent_id, "amount": round(due, 2)})
        return ("Передал мастеру ✓ Заявка на сверку оплаты создана. "
                "Подтвердим получение здесь же. Статус: «мои заказы»."), self._menu()

    # ------------------------------------------------------------ уведомления
    def _maybe_push_statuses(self) -> None:
        """Сообщить покупателям об изменении статуса и согласованной цене."""
        if not bool(self._settings().get("client_bot_notify", True)):
            return
        links = self.db.query(
            "SELECT l.chat_id,l.order_id,l.last_notified_status,l.source,"
            " c.status_notify,c.quiet_from,c.quiet_to,c.name chat_name"
            " FROM client_orders l JOIN orders o ON o.id=l.order_id"
            " LEFT JOIN client_chats c ON c.chat_id=l.chat_id"
            " WHERE COALESCE(c.status_notify,1)=1"
            " ORDER BY datetime(l.created_at) DESC LIMIT 500")
        for link in links:
            chat, order_id = link["chat_id"], link["order_id"]
            order = self.db.one("SELECT * FROM orders WHERE id=?", (order_id,))
            if not order:
                continue
            status = str(order.get("status") or "")
            last = str(link.get("last_notified_status") or "")
            # Backfill памяти после обновления 9.3: первый проход только
            # фиксирует текущий статус, чтобы не спамить старыми заказами.
            if not last:
                self.db.execute("UPDATE client_orders SET last_notified_status=? WHERE chat_id=? AND order_id=?",
                                (status, chat, order_id))
                self._notified[(chat, order_id)] = status
            elif last != status:
                if self._in_quiet_hours(link):
                    continue
                self._reply_keyed(chat, f"Заказ №{order.get('number')} «{order.get('product')}» — "
                                      f"{self._status_label(order)}"
                                      + (f"\nОжидаем к: {order.get('due')}" if order.get("due") else ""),
                                  self._order_card_keyboard(order),
                                  dedupe_key=f"status:{chat}:{order_id}:{status}")
                self.db.execute("UPDATE client_orders SET last_notified_status=? WHERE chat_id=? AND order_id=?",
                                (status, chat, order_id))
                self._notified[(chat, order_id)] = status
                self._log(chat, link.get("chat_name") or "", "",
                          f"№{order.get('number')}: {status}", kind="push", direction="out",
                          unread=0, order_id=order_id, source=link.get("source") or "")
            if status == "ready" and not order.get("client_ready_at"):
                self.db.execute("UPDATE orders SET client_ready_at=? WHERE id=?", (now_iso(), order_id))
            # Для индивидуальной заявки цена появляется позже: уведомить один
            # раз, не смешивая это с обычной сменой статуса.
            if (str(order.get("client_quote_status") or "") == "requested"
                    and num(order.get("price")) > 0
                    and not str(order.get("client_quote_sent_at") or "")):
                if self._in_quiet_hours(link):
                    continue
                quote = (f"По заявке №{order.get('number')} мастер согласовал цену: "
                         f"{_money(num(order.get('price')))}"
                         + (f"\nСрок: {order.get('due')}" if order.get("due") else "")
                         + "\nЕсли всё подходит, ответьте «да» мастеру в чате.")
                quote_buttons = self._order_card_keyboard(order)
                quote_buttons.setdefault("inline_keyboard", []).insert(0, [
                    {"text": "✅ Согласен", "callback_data": f"quote_yes:{order_id}"},
                    {"text": "↩️ Обсудить", "callback_data": f"quote_no:{order_id}"},
                ])
                self._reply_keyed(chat, quote, quote_buttons,
                                  dedupe_key=f"quote:{chat}:{order_id}:{order.get('client_quote_version') or 0}")
                self.db.execute("UPDATE orders SET client_quote_sent_at=?,client_quote_status='sent' WHERE id=?",
                                (now_iso(), order_id))
                self._funnel(chat, "quote_sent", source=link.get("source") or "telegram",
                             order_id=order_id, data={"amount": num(order.get("price"))})

    def _maybe_ask_reviews(self) -> None:
        """Через 2 дня после выдачи — спросить отзыв (один раз на заказ).

        Оценка приходит кнопкой, «есть проблема» — просим описать и будим
        мастера: плохой отзыв без реакции стоит дороже самого отзыва.
        """
        if not bool(self._settings().get("client_bot_review", True)):
            return
        links = self.db.query(
            "SELECT l.chat_id, l.order_id, c.quiet_from, c.quiet_to FROM client_orders l"
            " JOIN orders o ON o.id=l.order_id"
            " LEFT JOIN client_chats c ON c.chat_id=l.chat_id"
            " WHERE COALESCE(o.client_delivered_at,'')!=''"
            "   AND datetime(replace(o.client_delivered_at,'T',' ')) <= datetime('now', '-2 days')")
        for link in links:
            chat, order_id = link["chat_id"], link["order_id"]
            if self._in_quiet_hours(link):
                continue
            if self.db.one("SELECT 1 FROM client_reviews"
                           " WHERE order_id=? AND chat_id=?", (order_id, chat)):
                continue
            order = self.db.one("SELECT * FROM orders WHERE id=?", (order_id,))
            if not order:
                continue
            self.db.execute(
                "INSERT OR IGNORE INTO client_reviews(order_id,chat_id,asked_at,state,sent_at)"
                " VALUES(?,?,?,'asked',?)", (order_id, chat, now_iso(), now_iso()))
            self._reply_keyed(
                chat, f"Заказ №{order.get('number')} выдан. Всё ли хорошо?",
                _keyboard([("👍 Всё хорошо", f"review:{order_id}:good"),
                           ("👎 Есть проблема", f"review:{order_id}:bad")]),
                dedupe_key=f"review:{chat}:{order_id}")
            self._log(chat, "", "→ review", f"№{order.get('number')}: вопрос",
                      kind="push", direction="out", unread=0, order_id=order_id)

    def _review_rating(self, chat: str, row: dict, order_id: str,
                       rating: str) -> tuple[str, dict | None]:
        self.db.execute(
            "UPDATE client_reviews SET rating=?, state=?, created_at=?"
            " WHERE order_id=? AND chat_id=?",
            (rating, "needs_attention" if rating == "bad" else "rated", now_iso(), order_id, chat))
        order = self.db.one("SELECT * FROM orders WHERE id=?", (order_id,))
        number = (order or {}).get("number") or ""
        if rating == "good":
            return ("Спасибо! Если понадобится ещё — «каталог» всегда рядом.",
                    self._menu())
        self._await_problem[chat] = (order_id, time.time() + 86400)
        try:
            self.manager.notify_async(
                f"👎 Покупатель недоволен\nЗаказ №{number} · чат {chat}\n"
                f"Спросили подробности — ответить: кответ {chat} <текст>",
                critical=True)
        except Exception:
            pass
        return ("Сожалею 🙏 Опишите, что не так, одним сообщением — передам "
                "мастеру, и мы это поправим."), self._menu()

    def _review_comment(self, chat: str, row: dict, order_id: str,
                        text: str) -> str:
        self._await_problem.pop(chat, None)
        self.db.execute(
            "UPDATE client_reviews SET comment=?,state='needs_attention' WHERE order_id=? AND chat_id=?",
            (text[:1000], order_id, chat))
        order = self.db.one("SELECT number FROM orders WHERE id=?", (order_id,))
        number = (order or {}).get("number") or ""
        try:
            self.manager.notify_async(
                f"👎 Детали проблемы — заказ №{number}\nЧат {chat}\n«{text[:400]}»\n"
                f"Ответить: кответ {chat} <текст>", critical=True)
        except Exception:
            pass
        self.db.add_event("order", "Отзыв с деталями проблемы",
                          f"№{number}", "", {"order_id": order_id, "chat_id": chat})
        return ("Спасибо за подробности — мастер свяжется и поправит. "
                "Хорошего дня!")

    def _maybe_remind_pickup(self) -> None:
        """«Готов» висит дольше N дней — мягкое напоминание покупателю."""
        days = int(num(self._settings().get("client_bot_pickup_days"), 3))
        if days <= 0:
            return
        links = self.db.query(
            "SELECT l.chat_id, l.order_id, c.quiet_from, c.quiet_to FROM client_orders l"
            " JOIN orders o ON o.id=l.order_id"
            " LEFT JOIN client_chats c ON c.chat_id=l.chat_id"
            " WHERE o.status='ready' AND COALESCE(l.reminded_at,'')=''"
            "   AND COALESCE(c.status_notify,1)=1"
            f"   AND datetime(replace(o.updated_at,'T',' ')) <= datetime('now', '-{days} days')")
        for link in links:
            chat, order_id = link["chat_id"], link["order_id"]
            order = self.db.one("SELECT * FROM orders WHERE id=?", (order_id,))
            if not order:
                continue
            if self._in_quiet_hours(link):
                continue
            self.db.execute("UPDATE client_orders SET reminded_at=?"
                            " WHERE chat_id=? AND order_id=?",
                            (now_iso(), chat, order_id))
            self._reply_keyed(chat, f"Заказ №{order.get('number')} "
                                  f"«{order.get('product')}» готов и ждёт вас. "
                                  "Если забрали — извините за беспокойство 🙂",
                                  self._menu(),
                                  dedupe_key=f"pickup:{chat}:{order_id}")
            self._log(chat, "", "→ pickup", f"№{order.get('number')}: ждёт",
                      kind="push", direction="out", unread=0, order_id=order_id)

    # ------------------------------------------------------------- для панели
    def stats(self) -> dict:
        def count(sql: str, params: tuple = ()) -> int:
            try:
                return int(num(self.db.one(sql, params)["n"]))
            except Exception:
                return 0
        day = datetime.now().strftime("%Y-%m-%d")
        return {
            "chats": count("SELECT COUNT(*) n FROM client_chats"),
            "orders": count("SELECT COUNT(*) n FROM client_orders"),
            "messages": count("SELECT COUNT(*) n FROM client_bot_log"),
            "messages_today": count(
                "SELECT COUNT(*) n FROM client_bot_log WHERE at LIKE ?", (day + "%",)),
            "reviews_good": count(
                "SELECT COUNT(*) n FROM client_reviews WHERE rating='good'"),
            "reviews_bad": count(
                "SELECT COUNT(*) n FROM client_reviews WHERE rating='bad'"),
            "unread": count("SELECT COUNT(*) n FROM client_bot_log WHERE direction='in' AND unread=1"),
            "pending_payments": count("SELECT COUNT(*) n FROM client_payment_intents WHERE status='pending'"),
            "funnel_events": count("SELECT COUNT(*) n FROM client_bot_funnel"),
            "outbox_pending": count("SELECT COUNT(*) n FROM client_bot_outbox WHERE state='pending'"),
            "last_poll": self.last_poll,
        }

    def analytics(self, days: int = 30) -> dict:
        """Локальная воронка Telegram: источники, события и конверсия."""
        days = max(1, min(int(days or 30), 3650))
        # ISO-время в SQLite сравнивается лексикографически; Python timestamp
        # нужен только как безопасный fallback для старых записей без timezone.
        from datetime import timedelta
        at = (datetime.now() - timedelta(days=days)).isoformat()
        events = self.db.query(
            "SELECT event,COUNT(*) count FROM client_bot_funnel WHERE at>=?"
            " GROUP BY event ORDER BY count DESC", (at,))
        sources = self.db.query(
            "SELECT COALESCE(NULLIF(source,''),'direct') source,COUNT(DISTINCT chat_id) chats,"
            " SUM(CASE WHEN event IN ('order_created','custom_order_created','photo_order','file_order') THEN 1 ELSE 0 END) orders"
            " FROM client_bot_funnel WHERE at>=? GROUP BY source ORDER BY chats DESC", (at,))
        chats = count = 0
        try:
            chats = int(num((self.db.one("SELECT COUNT(DISTINCT chat_id) n FROM client_bot_funnel WHERE at>=?", (at,)) or {}).get("n")))
            count = int(num((self.db.one("SELECT COUNT(DISTINCT order_id) n FROM client_bot_funnel WHERE at>=? AND order_id<>''", (at,)) or {}).get("n")))
        except Exception:
            pass
        return {"days": days, "events": events, "sources": sources,
                "unique_chats": chats, "orders": count,
                "conversion": round(count / chats * 100, 1) if chats else 0.0}

    def me(self) -> dict:
        """Проверка токена: getMe — панель показывает имя бота."""
        result = self._call("getMe", {}, timeout=10)
        user = (result.get("result") or {})
        if not user:
            return {"ok": False, "error": "Токен не принят или сети нет"}
        return {"ok": True, "username": user.get("username") or "",
                "name": user.get("first_name") or ""}
