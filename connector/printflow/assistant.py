"""Помощник: внешний локальный рантайм → черновик, который подтверждает человек.

Зачем этот модуль существует и почему он устроен именно так.

Владелец просил «полноценного ИИ-помощника». Полноценный помощник, который сам
меняет базу, двигает мышью и печатает в чужие окна, в PrintFlow невозможен по
двум причинам, а не по одной:

  * безопасность физического мира. Любая автоматика, способная тронуть принтер,
    уже закрыта `unattended_dangerous_actions` и явными флагами. У базы
    клиентов с долгами и у файловой системы предела вреда нет вовсе, а
    галлюцинированный путь в команде удаления не отличим от правильного;
  * проверяемость. Компонент, который нельзя прогнать в `scripts/check.py`,
    не является «готовым» — значит он обязан быть маленьким, отключаемым и
    таким, чтобы его отказ ничего не ломал.

Поэтому контракт помощника узкий и сознательный:

  1. **Рантайм внешний.** Модель крутит отдельная программа на этом же
     компьютере (Ollama, llama.cpp server). В `requirements.txt` не появляется
     ничего: коннектор ходит в неё через `urllib` из стандартной библиотеки —
     тот же узор, которым шлюз Bambu Studio говорит со сторонней студией.
  2. **Только этот компьютер.** Адрес рантайма обязан оставаться loopback:
     текст заказа с телефоном и суммой не уезжает за пределы машины так же, как
     он не уезжал до появления помощника (`order_intake.py`).
  3. **Вывод — черновик.** Помощник не пишет в базу и не трогает принтеры. Он
     возвращает предложения, которые становятся фактом только после того, как
     человек нажал «Сохранить» в карточке заказа.
  4. **Модель не считает деньги.** Суммы, количество, граммы, часы и сроки —
     из детерминированного разбора и из справочников. Всё, что модель
     прислала числом, отбрасывается: уверенно названный неверный долг хуже
     пустого поля.
  5. **Отказ незаметен.** Нет рантайма, нет модели, таймаут, мусор вместо JSON —
     помощник отвечает «недоступен» с причиной, а панель работает как раньше
     (образец — `spaghetti.py` без Pillow: детектор честно выключается).
"""
from __future__ import annotations

import json
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from typing import Any

from .db import Database
from .logging_setup import log

# Адрес рантайма по умолчанию — Ollama на этой же машине. Порт намеренно не
# вынесен в константу приложения: владелец может поднять llama.cpp server или
# совместимый ответчик где угодно, но только в loopback (см. `_loopback_ok`).
DEFAULT_URL = "http://127.0.0.1:11434"

# Сколько ждём ответ. Локальная модель на 8 ГБ видеопамяти отвечает десятками
# секунд, поэтому таймаут длинный, но конечный: помощник не имеет права
# держать HTTP-поток коннектора занятым дольше, чем оператор готов смотреть
# на «Разбираю…».
TIMEOUT_SEC = 90.0
# Проба рантайма (список моделей) обязана быть быстрой: её зовут диагностика,
# карточка настроек и модалка входящего заказа.
PING_TIMEOUT_SEC = 2.0

MAX_INPUT_CHARS = 4000
MAX_REPLY_CHARS = 600
MAX_VALUE_CHARS = 160

# Текстовые поля черновика, которые помощнику разрешено предлагать. Всё
# остальное (qty, price, grams, hours, niche_id, nom_id, customer_id, file) —
# числа и ссылки на записи базы: их источник только детерминированный разбор и
# справочники, поэтому из ответа модели они вычёркиваются целиком.
SUGGEST_FIELDS = ("product", "customer_name", "phone", "messenger",
                  "material", "color", "due")

# Материалы — тот же список, что распознаёт парсер входящего текста. Модель,
# предложившая «древесину», получает отказ, а не новое значение в справочнике.
MATERIALS = ("PLA", "PETG", "ABS", "ASA", "TPU", "TPE", "PA", "PC", "PVA", "HIPS")

# Цвета — канонические подписи парсера (`order_intake._COLORS`). Своих названий
# у помощника нет: два источника правды о цвете в одном черновике — это два
# разных цвета в одном заказе.
COLORS = ("Чёрный", "Белый", "Красный", "Синий", "Зелёный", "Жёлтый",
          "Оранжевый", "Серый", "Розовый", "Фиолетовый", "Золотой",
          "Серебристый", "Прозрачный")

_JSON_START = re.compile(r"[\{\[]")
_MONEY_RE = re.compile(r"\d")


def _setting(db: Database, key: str, default: Any = "") -> Any:
    try:
        value = db.setting(key, default)
    except Exception:  # база может быть закрыта или не создана — помощник молчит
        return default
    return default if value is None else value


def config(db: Database) -> dict[str, Any]:
    """Настройки помощника одним словарём (без обращения к рантайму)."""
    url = str(_setting(db, "assistant_url", DEFAULT_URL) or "").strip() or DEFAULT_URL
    model = str(_setting(db, "assistant_model", "") or "").strip()
    return {
        "enabled": bool(_setting(db, "assistant_enabled", False)),
        "url": url.rstrip("/"),
        "model": model,
        "timeout_sec": float(_setting(db, "assistant_timeout_sec", TIMEOUT_SEC) or TIMEOUT_SEC),
    }


def _loopback_ok(url: str) -> tuple[bool, str]:
    """Помощник говорит только с этим компьютером — и никогда с чужим.

    Проверка не косметическая: текст входящего заказа содержит телефон клиента
    и сумму, а «локальный» помощник, который шлёт их на адрес в другой сети,
    перестаёт быть локальным. Хост сравнивается с loopback-именами так же, как
    это делает `config._LOOPBACK_NAMES` для Host-заголовка.
    """
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return False, "адрес рантайма не разбирается"
    if parsed.scheme not in ("http", "https"):
        return False, f"схема {parsed.scheme or 'не указана'} не поддерживается"
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return False, "в адресе нет хоста"
    loopback = {"localhost", "127.0.0.1", "::1", "[::1]", "0.0.0.0"}
    if host not in loopback:
        return False, f"адрес {host} не этот компьютер"
    # DNS может вернуть внешний адрес для «localhost» в чужом hosts-файле.
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except OSError:
        return False, "адрес рантайма не разрешается"
    for info in infos:
        ip = str(info[4][0])
        if not (ip.startswith("127.") or ip == "::1" or ip == "0.0.0.0"):
            return False, f"адрес {host} разрешается в {ip} — это не этот компьютер"
    return True, ""


def _post_json(url: str, payload: dict, timeout: float) -> tuple[bool, Any, str]:
    """Один POST в рантайм. Возвращает (получилось, JSON, причина отказа).

    Сетевые ошибки не исключение, а ответ: «рантайм не запущен» — штатное
    состояние помощника, панель должна показывать его как выключенную функцию.
    """
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json",
                 "User-Agent": "PrintFlow-assistant/1"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(8 * 1024 * 1024).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return False, None, f"рантайм ответил {exc.code}"
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        return False, None, f"рантайм недоступен: {reason}"
    try:
        return True, json.loads(raw or "{}"), ""
    except json.JSONDecodeError:
        return False, None, "рантайм ответил не JSON"


def _get_json(url: str, timeout: float) -> tuple[bool, Any, str]:
    """Один GET к внешнему рантайму. Отказ — ответ с причиной, не исключение.

    Чтение реестра навыков агента идёт именно так: у агента нет тела запроса,
    а panel-side помощнику нечего ему отправить.
    """
    request = urllib.request.Request(
        url, method="GET",
        headers={"Accept": "application/json",
                 "User-Agent": "PrintFlow-assistant/1"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(8 * 1024 * 1024).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return False, None, f"агент ответил {exc.code}"
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        return False, None, f"агент недоступен: {reason}"
    try:
        return True, json.loads(raw or "{}"), ""
    except json.JSONDecodeError:
        return False, None, "агент ответил не JSON"


def list_models(db: Database) -> list[str]:
    """Какие модели рантайм уже отдаёт. Пусто — не ошибка, а «не спросили»."""
    cfg = config(db)
    if not _loopback_ok(cfg["url"])[0]:
        return []
    ok, payload, _reason = _post_json(f"{cfg['url']}/api/tags", {}, PING_TIMEOUT_SEC)
    if not ok or not isinstance(payload, dict):
        return []
    return [str(row.get("name") or "") for row in (payload.get("models") or [])
            if isinstance(row, dict) and row.get("name")]


def status(db: Database) -> dict[str, Any]:
    """Состояние помощника для панели, настроек и `pf.py doctor`.

    Ответ всегда содержит `available` и `reason`: интерфейс рисует причину, а
    не догадывается о ней по пустому списку моделей.
    """
    cfg = config(db)
    out: dict[str, Any] = {
        "ok": True, "enabled": cfg["enabled"], "url": cfg["url"],
        "model": cfg["model"], "available": False, "reason": "",
        "models": [], "loopback": True, "actions": actions_payload(),
        "speech": speech_status(db),
    }
    local, why = _loopback_ok(cfg["url"])
    if not local:
        out.update(loopback=False, reason=f"Адрес рантайма должен быть этим компьютером: {why}")
        return out
    if not cfg["enabled"]:
        out["reason"] = "Помощник выключен в настройках"
        return out
    ok, payload, reason = _post_json(f"{cfg['url']}/api/tags", {}, PING_TIMEOUT_SEC)
    if not ok:
        out["reason"] = (f"Рантайм на {cfg['url']} не отвечает ({reason}). "
                         "Запустите его на этом компьютере — например, `ollama serve`.")
        return out
    models = [str(row.get("name") or "") for row in ((payload or {}).get("models") or [])
              if isinstance(row, dict) and row.get("name")]
    out["models"] = models
    if not cfg["model"]:
        out["reason"] = ("Модель не выбрана: впишите имя в настройках "
                         + (f"(рантайм отдаёт: {', '.join(models)})" if models
                            else "(рантайм пока не отдаёт ни одной модели — скачайте её)"))
        return out
    if models and not any(cfg["model"] in name for name in models):
        out["reason"] = (f"Модели «{cfg['model']}» у рантайма нет "
                         f"(есть: {', '.join(models)})")
        return out
    out.update(available=True, reason="")
    return out


def _prompt(raw_text: str, draft: dict[str, Any]) -> str:
    """Задание модели: только пустые поля и только факты из сообщения."""
    missing = [key for key in SUGGEST_FIELDS if not str(draft.get(key) or "").strip()]
    known = "\n".join(
        f"{key}: {draft.get(key)}" for key in
        ("product", "customer_name", "phone", "messenger", "material", "color",
         "due", "qty", "price")
        if str(draft.get(key) or "").strip()) or "(ничего не разобрано)"
    return (
        "Ты разбираешь входящее сообщение клиента мастерской 3D-печати.\n"
        "Сообщение:\n"
        f"\"\"\"\n{raw_text[:MAX_INPUT_CHARS]}\n\"\"\"\n\n"
        f"Уже разобрано системой:\n{known}\n\n"
        f"Заполни только пустые поля из этого списка: {', '.join(missing) or '(пустых нет)'}.\n"
        "Правила, нарушение которых делает ответ бесполезным:\n"
        "1. Бери только то, что есть в сообщении. Не придумывай факты.\n"
        "2. Не предлагай числа: количество, цену, вес, время и сроки считает система.\n"
        "3. material — одна из: " + ", ".join(MATERIALS) + ".\n"
        "4. color — одна из: " + ", ".join(COLORS) + ".\n"
        "5. due — дата в формате ГГГГ-ММ-ДД, только если она названа в сообщении.\n"
        "6. phone — только если номер есть в сообщении.\n"
        "7. reply — короткий ответ клиенту по-русски без сумм, сроков и обещаний: "
        "их подставит человек.\n\n"
        'Ответь одним JSON-объектом: {"fields": {"имя": "значение"}, "reply": "текст"}'
    )


def _extract_json(text: str) -> dict[str, Any]:
    """Первый JSON-объект из ответа модели: с рамкой ```json или без неё.

    Модель умеет добавить пояснение до и после JSON. Вырезать объект по первым
    сбалансированным скобкам дешевле, чем спорить с ней о формате.
    """
    raw = str(text or "")
    start = _JSON_START.search(raw)
    if not start:
        return {}
    depth = 0
    in_string = False
    escaped = False
    for index in range(start.start(), len(raw)):
        ch = raw[index]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 0:
                try:
                    parsed = json.loads(raw[start.start():index + 1])
                except json.JSONDecodeError:
                    return {}
                return parsed if isinstance(parsed, dict) else {}
    return {}


def _clean_phone(value: Any) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    return f"+{digits}" if len(digits) == 11 and digits.startswith("7") else ""


def _clean_messenger(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if not text.startswith("@"):
        text = "@" + text.lstrip(" \t")
    return text if re.fullmatch(r"@[A-Za-z0-9_]{3,}", text) else ""


def _clean_value(key: str, value: Any) -> str:
    """Превратить ответ модели в значение поля или в пустую строку (отказ).

    Пустая строка здесь означает «не принято», а не «модель промолчала»: вызывающий
    код знает, какие поля были пустыми, и просто не увидит предложения.
    """
    if isinstance(value, (bool, int, float, dict, list)) or value is None:
        return ""  # числа и структуры не принимаем вовсе — см. докстринг модуля
    text = " ".join(str(value).split()).strip()[:MAX_VALUE_CHARS]
    if not text:
        return ""
    if key == "phone":
        return _clean_phone(text)
    if key == "messenger":
        return _clean_messenger(text)
    if key == "material":
        upper = text.upper().replace("PET-G", "PETG").replace(" ", "")
        return upper if upper in MATERIALS else ""
    if key == "color":
        low = text.casefold().replace("ё", "е")
        return next((color for color in COLORS if color.casefold().replace("ё", "е") == low), "")
    if key == "due":
        match = re.fullmatch(r"(20\d{2})-(\d{1,2})-(\d{1,2})", text)
        if not match:
            return ""
        try:
            return date(int(match.group(1)), int(match.group(2)),
                        int(match.group(3))).isoformat()
        except ValueError:
            return ""
    return text


def suggest(db: Database, draft: dict[str, Any], raw_text: str = "",
            channel: str = "") -> dict[str, Any]:
    """Предложить значения для пустых полей черновика и черновик ответа клиенту.

    Возвращает `{ok, available, reason, draft, suggestions, reply, warnings}`.
    `draft` — копия входного черновика с добрыми предложениями; `suggestions` —
    список `{field, value, source}`, чтобы интерфейс показал, какое поле
    придумал помощник, а какое разобрала система.
    """
    source_draft = dict(draft or {})
    text = " ".join(str(raw_text or source_draft.get("notes") or "").split())
    if not text:
        # Проверка до пробы рантайма: незачем держать HTTP-запрос к модели,
        # если разбирать нечего.
        return {"ok": False, "available": False, "reason": "Нет текста для разбора",
                "draft": source_draft, "suggestions": [], "reply": "", "warnings": []}
    state = status(db)
    if not state.get("available"):
        return {
            "ok": False, "available": False,
            "reason": state.get("reason") or "Помощник недоступен",
            "draft": source_draft, "suggestions": [], "reply": "",
            "warnings": [],
        }

    cfg = config(db)
    started = time.time()
    ok, payload, reason = _post_json(
        f"{cfg['url']}/api/chat",
        {"model": cfg["model"], "stream": False,
         "options": {"temperature": 0.1},
         "messages": [{"role": "user", "content": _prompt(text, source_draft)}]},
        timeout=cfg["timeout_sec"])
    if not ok or not isinstance(payload, dict):
        log().warning("Помощник: рантайм отказал (%s)", reason)
        return {"ok": False, "available": True,
                "reason": f"Рантайм не ответил: {reason}",
                "draft": source_draft, "suggestions": [], "reply": "",
                "warnings": []}

    content = payload.get("message") or {}
    if not isinstance(content, dict):
        content = {}
    answer = _extract_json(str(content.get("content") or payload.get("response") or ""))
    fields = answer.get("fields")
    if not isinstance(fields, dict):
        fields = {}

    out = dict(source_draft)
    suggestions: list[dict[str, str]] = []
    dropped: list[str] = []
    for key, value in fields.items():
        name = str(key or "").strip()
        if name not in SUGGEST_FIELDS:
            dropped.append(name or "(пустое имя)")
            continue
        if str(out.get(name) or "").strip():
            continue  # поле уже разобрано — модель его не пересматривает
        clean = _clean_value(name, value)
        if not clean:
            dropped.append(name)
            continue
        out[name] = clean
        suggestions.append({"field": name, "value": clean, "source": "assistant"})

    warnings: list[str] = []
    if suggestions:
        names = ", ".join(item["field"] for item in suggestions)
        warnings.append(f"Помощник предложил поля: {names} — проверьте перед сохранением")
    reply = " ".join(str(answer.get("reply") or "").split())[:MAX_REPLY_CHARS]
    if reply and _MONEY_RE.search(reply):
        # Модель не считает деньги (правило 4), но может переписать цифру из
        # сообщения. Черновик ответа с цифрой помечается явно: сумму и срок
        # подставляет человек, иначе клиенту уйдёт обещание, которого нет в базе.
        warnings.append("В черновике ответа есть числа — суммы и сроки проверьте вручную")

    if dropped:
        log().info("Помощник: отброшены значения %s", ", ".join(sorted(set(dropped))[:10]))
    log().info("Помощник: предложений %d за %.1f с (модель %s)",
               len(suggestions), time.time() - started, cfg["model"])
    return {"ok": True, "available": True, "reason": "",
            "draft": out, "suggestions": suggestions, "reply": reply,
            "warnings": warnings}


# ---------------------------------------------------------------------------
# Каталог действий (18.13): помощник предлагает, панель выполняет, журнал пишет
# ---------------------------------------------------------------------------
#
# Каталог намеренно состоит только из маршрутов, которые реально зовут страницы
# панели: мёртвый маршрут в каталоге — это кнопка, которая молча ничего не
# делает. Контракт сверяет каждый адрес с реестром и if-цепочками в тесте
# `test_assistant_panel.py`, а не на глаз.
#
# `confirm` — не пожелание, а правило репозитория: «Подтверждение — только для
# денег и печати» (продажа, выемка, стоп, перепечатка). Помощник не имеет права
# понизить его через `confirmed` в теле запроса: значение берётся из каталога.
ACTIONS: dict[str, dict[str, Any]] = {
    # --- чтение: выполняется сразу, как и в панели
    "park": {"title": "Состояние парка", "method": "GET", "path": "/api/state",
             "params": (), "confirm": False,
             "doc": "Принтеры, прогресс, AMS, тревоги — одним снимком."},
    "pult": {"title": "Сводка пульта", "method": "GET", "path": "/api/pult/summary",
             "params": (), "confirm": False,
             "doc": "Что печатается и что ждёт — как на телефоне у станка."},
    "orders": {"title": "Список заказов", "method": "GET", "path": "/api/orders",
               "params": (), "confirm": False, "doc": "Заказы с экономикой."},
    "queue": {"title": "Очередь печати", "method": "GET", "path": "/api/jobs",
              "params": (), "confirm": False, "doc": "Задания парка."},
    "insights": {"title": "Здоровье бизнеса", "method": "GET", "path": "/api/insights",
                 "params": (), "confirm": False,
                 "doc": "Цель месяца, касса вперёд, налоговый календарь."},
    "plan": {"title": "План на сегодня", "method": "GET", "path": "/api/plan/day",
             "params": (), "confirm": False, "doc": "Мастер-план производства."},
    "finance": {"title": "Финансы", "method": "GET", "path": "/api/finance",
                "params": (), "confirm": False, "doc": "Доход, расход, прибыль."},
    "clients": {"title": "Клиенты", "method": "GET", "path": "/api/customers",
                "params": (), "confirm": False, "doc": "История покупок и долги."},
    "shelf": {"title": "Стеллаж", "method": "GET", "path": "/api/shelf",
              "params": (), "confirm": False, "doc": "Остатки и продажи полки."},
    "diagnostics": {"title": "Самодиагностика", "method": "GET",
                    "path": "/api/diagnostics/report", "params": (),
                    "confirm": False, "doc": "Снимок системы текстом."},
    "messages": {"title": "Диалоги, ждущие ответа", "method": "GET",
                 "path": "/api/conversations/by-order", "params": (),
                 "confirm": False, "doc": "По каким заказам клиент ждёт ответа."},
    "search": {"title": "Поиск по цеху", "method": "GET", "path": "/api/search",
               "params": ("q",), "confirm": False,
               "doc": "Заказы, клиенты, катушки одним поиском."},
    # --- станок: печать, поэтому только через «Подтвердить»
    "printer_command": {"title": "Команда станку", "method": "POST",
                        "path": "/api/printer/command",
                        "params": ("printer_id", "command", "value"),
                        "confirm": True,
                        "doc": "pause, resume, stop, light_toggle, speed."},
    "job_start": {"title": "Запустить задание", "method": "POST",
                  "path": "/api/jobs/start", "params": ("id", "printer_id"),
                  "confirm": True, "doc": "Старт печати из очереди."},
    "job_cancel": {"title": "Отменить задание", "method": "POST",
                   "path": "/api/jobs/cancel", "params": ("id",),
                   "confirm": True, "doc": "Снять задание с очереди."},
    # --- заказы и деньги: только через «Подтвердить»
    "order_save": {"title": "Сохранить заказ", "method": "POST",
                   "path": "/api/order/save", "params": ("draft",),
                   "confirm": True, "doc": "Новый заказ или правка существующего."},
    "order_status": {"title": "Сменить статус заказа", "method": "POST",
                     "path": "/api/order/status", "params": ("id", "status"),
                     "confirm": True, "doc": "Перевод по доске."},
    "order_fulfill": {"title": "Выдать заказ", "method": "POST",
                      "path": "/api/order/fulfill",
                      "params": ("id", "payment_action", "account_id",
                                 "payment_method"),
                      "confirm": True,
                      "doc": "Передача клиенту с записью оплаты или долга."},
    "shelf_sale": {"title": "Продажа с полки", "method": "POST",
                   "path": "/api/shelf/sale", "params": ("item_id", "qty"),
                   "confirm": True, "doc": "Быстрая продажа позиции стеллажа."},
    "settings_save": {"title": "Изменить настройки", "method": "POST",
                      "path": "/api/settings", "params": ("patch",),
                      "confirm": True,
                      "doc": "Тарифы, автоматизация, пороги — через схему настроек."},
}

# Действия, которые журнал обязан показывать отдельно: они двигают деньги или
# физический станок. Чтение в журнал не пишется вовсе — иначе лента утонет.
CONFIRMED_ACTIONS = tuple(name for name, action in ACTIONS.items() if action["confirm"])


def actions_payload() -> list[dict[str, Any]]:
    """Каталог для панели: без внутренних полей, с явным признаком подтверждения."""
    return [{"id": name, "title": action["title"], "method": action["method"],
             "path": action["path"], "params": list(action["params"]),
             "confirm": bool(action["confirm"]), "doc": action["doc"]}
            for name, action in ACTIONS.items()]


def _intent_prompt(text: str) -> str:
    catalog = "\n".join(
        f"- {name}: {action['title']}"
        + (f" (параметры: {', '.join(action['params'])})" if action["params"] else "")
        + (f" — {action['doc']}" if action.get("doc") else "")
        for name, action in ACTIONS.items())
    return (
        "Ты диспетчер локальной системы учёта 3D-производства PrintFlow.\n"
        f"Фраза владельца:\n\"\"\"\n{text[:MAX_INPUT_CHARS]}\n\"\"\"\n\n"
        f"Доступные действия:\n{catalog}\n\n"
        "Правила:\n"
        "1. Выбери ровно одно действие или ни одного. Не выдумывай действия из списка.\n"
        "2. Параметры — только из списка этого действия и только значениями из фразы.\n"
        "3. Не подставляй суммы, количество, вес и сроки, которых во фразе нет.\n"
        "4. Если фраза — вопрос о состоянии, выбирай чтение, а не действие.\n"
        "5. explain — одно предложение по-русски: что будет сделано.\n\n"
        'Ответь одним JSON-объектом: {"action": "идентификатор", '
        '"params": {"имя": "значение"}, "explain": "текст"}'
    )


def _clean_params(action: dict[str, Any], raw: Any) -> dict[str, Any]:
    """Параметры действия: только разрешённые имена, только строки и числа.

    Структуры принимаются исключительно там, где маршрут их ждёт (`draft` у
    сохранения заказа, `patch` у настроек): иначе модель могла бы прислать
    вложенный объект в поле, которое сервер читает как строку.
    """
    allowed = tuple(action.get("params") or ())
    if not allowed or not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    for key in allowed:
        if key not in raw:
            continue
        value = raw[key]
        if value is None:
            continue
        if isinstance(value, (dict, list)):
            # Структуру ждут ровно два маршрута: черновик заказа и патч
            # настроек. В остальных полях словарь превратился бы в строку
            # «{'a': 1}», которую сервер прочитал бы как имя.
            if key in ("draft", "patch"):
                out[key] = value
            continue
        if isinstance(value, bool):
            out[key] = value
        elif isinstance(value, (int, float)):
            out[key] = value
        else:
            text = " ".join(str(value).split())[:MAX_VALUE_CHARS]
            if text:
                out[key] = text
    return out


def complete(db: Database, prompt: str) -> dict[str, Any]:
    """Один запрос к рантайму модели: промпт на входе, текст на выходе.

    Общая точка входа для всех разговоров с моделью (намерение, ответ по фактам
    базы). Правила те же, что у помощника в целом: только loopback, отказ всегда
    с причиной, пустой ответ модели — тоже отказ, а не пустая строка.
    """
    state = status(db)
    if not state.get("available"):
        return {"ok": False, "text": "", "model": state.get("model", ""),
                "reason": state.get("reason") or "Помощник недоступен"}
    cfg = config(db)
    ok, payload, reason = _post_json(
        f"{cfg['url']}/api/chat",
        {"model": cfg["model"], "stream": False, "options": {"temperature": 0.0},
         "messages": [{"role": "user", "content": str(prompt or "")[:MAX_INPUT_CHARS * 4]}]},
        timeout=cfg["timeout_sec"])
    if not ok or not isinstance(payload, dict):
        return {"ok": False, "text": "", "model": cfg["model"],
                "reason": f"Рантайм не ответил: {reason}"}
    message = payload.get("message") or {}
    text = " ".join(str(message.get("content") or payload.get("response") or "").split())
    if not text:
        return {"ok": False, "text": "", "model": cfg["model"],
                "reason": "Модель ответила пустотой"}
    return {"ok": True, "text": text[:MAX_REPLY_CHARS * 3], "model": cfg["model"],
            "reason": ""}


def parse_intent(db: Database, text: str) -> dict[str, Any]:
    """Фраза владельца → действие из каталога, параметры и объяснение.

    Помощник здесь ничего не выполняет и не знает адресов маршрутов: он
    возвращает идентификатор действия, а панель берёт адрес, метод и признак
    подтверждения из каталога. Именно поэтому «полный доступ» не превращается
    в «модель дёргает любой URL»: адреса в ответе модели нет вовсе.
    """
    clean_text = " ".join(str(text or "").split())
    if not clean_text:
        return {"ok": False, "available": False, "reason": "Пустая фраза",
                "action": None, "params": {}, "explain": "", "warnings": []}
    state = status(db)
    if not state.get("available"):
        return {"ok": False, "available": False,
                "reason": state.get("reason") or "Помощник недоступен",
                "action": None, "params": {}, "explain": "", "warnings": []}

    cfg = config(db)
    ok, payload, reason = _post_json(
        f"{cfg['url']}/api/chat",
        {"model": cfg["model"], "stream": False,
         "options": {"temperature": 0.0},
         "messages": [{"role": "user", "content": _intent_prompt(clean_text)}]},
        timeout=cfg["timeout_sec"])
    if not ok or not isinstance(payload, dict):
        return {"ok": False, "available": True,
                "reason": f"Рантайм не ответил: {reason}",
                "action": None, "params": {}, "explain": "", "warnings": []}

    message = payload.get("message")
    answer = _extract_json(str((message or {}).get("content") or payload.get("response") or ""))
    action_id = str(answer.get("action") or "").strip().casefold()
    action = ACTIONS.get(action_id)
    if action is None:
        return {"ok": True, "available": True, "reason": "",
                "action": None, "params": {},
                "explain": " ".join(str(answer.get("explain") or "").split())[:MAX_VALUE_CHARS],
                "warnings": []}

    params = _clean_params(action, answer.get("params"))
    missing = [key for key in action["params"] if key not in params
               and key not in ("value", "account_id", "payment_method")]
    warnings: list[str] = []
    if missing:
        warnings.append("Не хватает параметров: " + ", ".join(missing)
                        + " — добавьте их в карточке действия")
    if action["confirm"]:
        warnings.append("Действие требует подтверждения: деньги или печать")
    return {"ok": True, "available": True, "reason": "",
            "action": {"id": action_id, "title": action["title"],
                       "method": action["method"], "path": action["path"],
                       "confirm": bool(action["confirm"]), "doc": action["doc"]},
            "params": params,
            "explain": " ".join(str(answer.get("explain") or "").split())[:MAX_VALUE_CHARS],
            "warnings": warnings}


def journal(db: Database, action_id: str, title: str, outcome: str,
            detail: str = "", data: dict | None = None,
            printer_id: str = "") -> dict[str, Any]:
    """Запись в журнал: кто сделал — помощник, и чем это кончилось.

    Вход в панель помощника не требуется (решение владельца), поэтому журнал —
    единственное место, где остаётся след действия. Запись создаёт панель
    после выполнения, потому что только она знает итог: подтвердил человек,
    отказал сервер или действие прошло.
    """
    payload = {"source": "assistant", "action": str(action_id or ""),
               "outcome": str(outcome or ""), **(data or {})}
    try:
        return db.add_event("assistant", str(title or "Действие помощника"),
                            str(detail or ""), str(printer_id or ""), payload)
    except Exception as exc:
        log().warning("Журнал помощника: запись не удалась (%s)", exc)
        return {"ok": False, "error": str(exc)}


def journal_recent(db: Database, limit: int = 30) -> list[dict[str, Any]]:
    """Последние действия помощника — для панели и для разбора инцидента."""
    try:
        rows = db.events(limit=max(1, min(200, int(limit))), kind="assistant")
    except Exception:
        return []
    return rows


# ---------------------------------------------------------------------------
# Рантайм речи (срез 2): звук → текст на этом же компьютере
# ---------------------------------------------------------------------------
#
# Распознавание намеренно не живёт в браузере: `webkitSpeechRecognition` в
# WebView2 отправляет звук в облако вендора, а обещание системы — данные на
# одном компьютере. Поэтому речь устроена как модель (вопрос 8 допроса):
# внешняя программа, loopback, stdlib-клиент, честное выключение.
#
# На видеопамять рантайм речи не претендует вовсе: vosk и whisper.cpp с
# маленькой моделью работают на процессоре, а 8 ГБ уже делят текстовая модель и
# WebGPU-нарезка (`gpuslice.js`, ADR-0003).
DEFAULT_SPEECH_URL = "http://127.0.0.1:8791"
MAX_AUDIO_BYTES = 12 * 1024 * 1024
# Пинг рантаймов короткий: статус рисует панель при каждом открытии, и молчание
# мёртвой программы не должно тормозить кнопки.
PING_SEC = min(1.5, PING_TIMEOUT_SEC)


def speech_config(db: Database) -> dict[str, Any]:
    url = str(_setting(db, "assistant_speech_url", DEFAULT_SPEECH_URL) or "").strip()
    return {
        "enabled": bool(_setting(db, "assistant_speech_enabled", False)),
        "url": (url or DEFAULT_SPEECH_URL).rstrip("/"),
        "model": str(_setting(db, "assistant_speech_model", "") or "").strip(),
        "timeout_sec": float(_setting(db, "assistant_speech_timeout_sec", 30.0) or 30.0),
    }


def speech_status(db: Database) -> dict[str, Any]:
    """Доступен ли рантайм речи. Ответ всегда с причиной — как у модели."""
    cfg = speech_config(db)
    out: dict[str, Any] = {"ok": True, "enabled": cfg["enabled"], "url": cfg["url"],
                           "model": cfg["model"], "available": False,
                           "reason": "", "loopback": True}
    local, why = _loopback_ok(cfg["url"])
    if not local:
        out.update(loopback=False, reason=f"Адрес рантайма речи должен быть этим компьютером: {why}")
        return out
    if not cfg["enabled"]:
        out["reason"] = "Голос выключен в настройках"
        return out
    ok, payload, reason = _post_json(f"{cfg['url']}/health", {}, PING_SEC)
    if not ok:
        out["reason"] = (f"Рантайм речи на {cfg['url']} не отвечает ({reason}). "
                         "Запустите его на этом компьютере — звук обрабатывается на процессоре.")
        return out
    if isinstance(payload, dict) and payload.get("model"):
        out["model"] = str(payload["model"])
    out.update(available=True, reason="")
    return out


def transcribe(db: Database, audio: bytes, language: str = "ru") -> dict[str, Any]:
    """Звук → текст. Ничего не решает и ничего не выполняет.

    Байты уходят только в loopback и только если рантайм включён: запись голоса
    не покидает компьютер, как не покидает его текст сообщения клиента.
    """
    cfg = speech_config(db)
    state = speech_status(db)
    if not state.get("available"):
        return {"ok": False, "available": False, "text": "",
                "reason": state.get("reason") or "Рантайм речи недоступен"}
    if not audio:
        return {"ok": False, "available": True, "text": "", "reason": "Пустая запись"}
    if len(audio) > MAX_AUDIO_BYTES:
        return {"ok": False, "available": True, "text": "",
                "reason": "Запись длиннее допустимого — говорите короче"}

    boundary = "----printflow-speech"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="language"\r\n\r\n'
        f"{language or 'ru'}\r\n"
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="audio"; filename="phrase.wav"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode("utf-8") + bytes(audio) + f"\r\n--{boundary}--\r\n".encode("utf-8")

    request = urllib.request.Request(
        f"{cfg['url']}/transcribe", data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                 "User-Agent": "PrintFlow-assistant/1"})
    try:
        with urllib.request.urlopen(request, timeout=cfg["timeout_sec"]) as response:
            raw = response.read(1024 * 1024).decode("utf-8", "replace")
        payload = json.loads(raw or "{}")
    except urllib.error.HTTPError as exc:
        return {"ok": False, "available": True, "text": "",
                "reason": f"Рантайм речи ответил {exc.code}"}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError,
            json.JSONDecodeError) as exc:
        reason = getattr(exc, "reason", exc)
        return {"ok": False, "available": True, "text": "",
                "reason": f"Рантайм речи не ответил: {reason}"}

    text = " ".join(str((payload or {}).get("text") or "").split())
    if not text:
        return {"ok": False, "available": True, "text": "",
                "reason": "Речь не распознана — повторите фразу"}
    return {"ok": True, "available": True, "text": text[:MAX_INPUT_CHARS],
            "reason": "", "language": language or "ru"}


# ---------------------------------------------------------------------------
# Агент компьютера (срез 3): стоп-слово, активное окно, действия в нём
# ---------------------------------------------------------------------------
#
# Агент — третья внешняя программа, иPrintFlow намеренно не хранит ни его
# зависимостей, ни его кода запуска: в репозитории лежат только адрес, статус и
# протокол. Иначе torч, pywin32 и OCR приехали бы в окружение коннектора,
# которое `pf.py` ставит каждому, включая телефон-кассу.
DEFAULT_AGENT_URL = "http://127.0.0.1:8799"


def agent_config(db: Database) -> dict[str, Any]:
    url = str(_setting(db, "assistant_agent_url", DEFAULT_AGENT_URL) or "").strip()
    return {"enabled": bool(_setting(db, "assistant_agent_enabled", False)),
            "url": (url or DEFAULT_AGENT_URL).rstrip("/")}


def agent_status(db: Database) -> dict[str, Any]:
    """Жив ли агент и что он сейчас видит. Только чтение: PrintFlow не
    выполняет действия в чужих окнах и не хранит снимки экрана."""
    cfg = agent_config(db)
    out: dict[str, Any] = {"ok": True, "enabled": cfg["enabled"], "url": cfg["url"],
                           "available": False, "reason": "", "loopback": True,
                           "wake_word": False, "window": ""}
    local, why = _loopback_ok(cfg["url"])
    if not local:
        out.update(loopback=False,
                   reason=f"Адрес агента должен быть этим компьютером: {why}")
        return out
    if not cfg["enabled"]:
        out["reason"] = "Агент компьютера выключен в настройках"
        return out
    ok, payload, reason = _post_json(f"{cfg['url']}/health", {}, PING_SEC)
    if not ok:
        out["reason"] = (f"Агент на {cfg['url']} не отвечает ({reason}). "
                         "Запустите его отдельно: папка agent/ со своим окружением.")
        return out
    payload = payload if isinstance(payload, dict) else {}
    out.update(available=True, reason="",
               wake_word=bool(payload.get("wake_word")),
               window=str(payload.get("window") or "")[:200])
    return out


def agent_skills(db: Database) -> dict[str, Any]:
    """Реестр навыков ассистента компьютера (18.15, идея И136).

    Панель показывает реестр, но не владеет им: навыки объявляет агент, а панель
    читает их по loopback. Поэтому здесь нет ни списка навыков, ни их параметров
    — только чтение и причина, если агент выключен или не отвечает. Исполняет
    навыки тоже агент: панель не зовёт их сама и не подменяет подтверждение.
    """
    state = agent_status(db)
    out: dict[str, Any] = {"ok": True, "enabled": state["enabled"], "url": state["url"],
                           "available": False, "reason": state.get("reason") or "",
                           "skills": [], "count": 0, "ready": 0, "unavailable": []}
    if not state.get("available"):
        return out
    ok, payload, reason = _get_json(f"{state['url']}/skills", PING_SEC)
    if not ok or not isinstance(payload, dict):
        out["reason"] = f"Агент не отдал реестр навыков ({reason})"
        return out
    rows = [row for row in (payload.get("skills") or []) if isinstance(row, dict)]
    out.update(available=True, reason="", skills=rows, count=len(rows),
               ready=int(payload.get("ready") or 0),
               unavailable=[row for row in (payload.get("unavailable") or [])
                            if isinstance(row, dict)])
    return out


def _call_agent_skill(db: Database, name: str, params: dict[str, Any],
                      timeout: float = 30.0) -> dict[str, Any]:
    """Вызвать навык агента по loopback (Авито, ТГ). Возвращает ответ агента."""
    state = agent_status(db)
    if not state.get("available"):
        return {"ok": False, "reason": state.get("reason") or "Агент недоступен"}
    ok, payload, reason = _post_json(f"{state['url']}/skill",
                                     {"name": str(name), "params": dict(params or {})},
                                     timeout=timeout)
    if not ok or not isinstance(payload, dict):
        return {"ok": False, "reason": f"Агент не ответил: {reason}"}
    # Агент может вернуть needs_confirmation для write-навыков
    return payload


def avito_watch(db: Database, query: str, city: str = "", category: str = "",
                max_price: int = 0, min_price: int = 0) -> dict[str, Any]:
    return _call_agent_skill(db, "avito.watch",
                             {"query": query, "city": city, "category": category,
                              "max_price": max_price, "min_price": min_price})


def avito_search(db: Database, query: str, city: str = "", category: str = "",
                 max_price: int = 0, min_price: int = 0, limit: int = 20) -> dict[str, Any]:
    return _call_agent_skill(db, "avito.search",
                             {"query": query, "city": city, "category": category,
                              "max_price": max_price, "min_price": min_price, "limit": limit},
                             timeout=20.0)


def avito_check(db: Database, watch_id: int = 0, only_new: bool = True) -> dict[str, Any]:
    return _call_agent_skill(db, "avito.check",
                             {"watch_id": watch_id, "only_new": only_new},
                             timeout=30.0)


def avito_reply(db: Database, thread: str, intent: str = "", city: str = "") -> dict[str, Any]:
    return _call_agent_skill(db, "avito.reply",
                             {"thread": thread, "intent": intent, "city": city},
                             timeout=30.0)


def tg_draft(db: Database, topic: str, tone: str = "дружелюбный",
             facts: str = "", source: str = "") -> dict[str, Any]:
    return _call_agent_skill(db, "tg.draft",
                             {"topic": topic, "tone": tone, "facts": facts, "source": source},
                             timeout=30.0)


def tg_ideas(db: Database, context: str = "", limit: int = 8) -> dict[str, Any]:
    return _call_agent_skill(db, "tg.ideas",
                             {"context": context, "limit": limit},
                             timeout=20.0)


def tg_post(db: Database, draft_id: int = 0, text: str = "", chat: str = "") -> dict[str, Any]:
    return _call_agent_skill(db, "tg.post",
                             {"draft_id": draft_id, "text": text, "chat": chat},
                             timeout=20.0)
