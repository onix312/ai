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
import ast
import math
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path
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

# Дата и день недели по-русски. «Сегодня» модель берёт отсюда, а не из
# памяти и не из веб-поиска: поиск на «какое сегодня число» приносил SEO-
# страницы с чужими датами, которые маленькая модель вставляла дословно.
_RU_DAYS = ("понедельник", "вторник", "среда", "четверг", "пятница",
            "суббота", "воскресенье")
_RU_MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня",
              "июля", "августа", "сентября", "октября", "ноября", "декабря")


def date_line(now: datetime | None = None) -> str:
    """«Сегодня — …, сейчас …» — единственный источник даты для модели."""
    now = now or datetime.now()
    return (f"Сегодня — {_RU_DAYS[now.weekday()]}, {now.day} {_RU_MONTHS[now.month - 1]} "
            f"{now.year} года, сейчас {now:%H:%M}.")


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


# Прокси из окружения (HTTP_PROXY) не умеет ходить на 127.0.0.1 этой машины:
# urllib тогда ждёт таймаут вместо «порт закрыт», и панель «тупит» на каждом
# пинге агента, речи и модели. Loopback открываем мимо прокси.
_LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_AGENT_START_LOCK = threading.Lock()


def _local_open(request: urllib.request.Request, timeout: float):
    """Открыть loopback-запрос, не отдавая его системному прокси."""
    return _LOCAL_OPENER.open(request, timeout=timeout)


def _tcp_up(url: str, timeout: float = 0.35) -> tuple[bool, str]:
    """Порт слушает? Быстрее HTTP: закрытый порт на Windows — отказ, не 1.5 с."""
    try:
        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname or "127.0.0.1"
        port = int(parsed.port or (443 if parsed.scheme == "https" else 80))
    except (TypeError, ValueError):
        return False, "адрес не разбирается"
    if host == "localhost":
        host = "127.0.0.1"
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        return True, ""
    except TimeoutError:
        return False, "timed out"
    except OSError as exc:
        return False, str(exc)
    finally:
        sock.close()


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
        with _local_open(request, timeout) as response:
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


def _get_json(url: str, timeout: float, *, label: str = "агент") -> tuple[bool, Any, str]:
    """Один GET к локальному рантайму: список моделей или навыки агента.

    GET не посылает тело: у Ollama ``/api/tags`` отвечает 405 на POST.
    ``label`` оставляет причину отказа понятной для обоих рантаймов.
    """
    request = urllib.request.Request(
        url, method="GET",
        headers={"Accept": "application/json",
                 "User-Agent": "PrintFlow-assistant/1"})
    try:
        with _local_open(request, timeout) as response:
            raw = response.read(8 * 1024 * 1024).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return False, None, f"{label} ответил {exc.code}"
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        return False, None, f"{label} недоступен: {reason}"
    try:
        return True, json.loads(raw or "{}"), ""
    except json.JSONDecodeError:
        return False, None, f"{label} ответил не JSON"


def list_models(db: Database) -> list[str]:
    """Какие модели рантайм уже отдаёт. Пусто — не ошибка, а «не спросили»."""
    cfg = config(db)
    if not _loopback_ok(cfg["url"])[0]:
        return []
    ok, payload, _reason = _get_json(f"{cfg['url']}/api/tags", PING_TIMEOUT_SEC, label="рантайм")
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
    ok, payload, reason = _get_json(f"{cfg['url']}/api/tags", PING_TIMEOUT_SEC, label="рантайм")
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
    if cfg["model"] not in models and f"{cfg['model']}:latest" not in models:
        out["reason"] = (f"Модели «{cfg['model']}» у рантайма нет "
                         + (f"(есть: {', '.join(models)})" if models
                            else "(локальных моделей нет — выполните `ollama pull qwen2.5:3b`)"))
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
        "4. Если фраза — вопрос о состоянии цеха, выбирай чтение, а не действие.\n"
        "5. explain — одно предложение по-русски: что будет сделано.\n"
        "6. Арифметика, приветствие и вопрос не про цех — не действие: action пустая строка.\n\n"
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


# Фраза, которую имеет смысл отдавать диспетчеру каталога. «2+2», приветствие и
# вопрос про погоду сюда не входят: маленькая модель иначе выбирает первое
# чтение из списка — «Состояние парка» — и панель выполняет GET /api/state.
_CATALOG_RE = re.compile(
    r"пауз|возобнов|продолж|запусти|отмен|продай|выдай|смени статус|"
    r"останови|стоп|сохрани|поставь|"
    r"парк|станк|принтер|печат|заказ|долг|касс|полк|стеллаж|"
    r"очеред|план|клиент|финанс|пульт|диагност|настройк",
    re.IGNORECASE)

# В веб идут только действительно свежие факты. «Сегодня», «сейчас», «курс»,
# «в сети» из триггеров убраны: «какое сегодня число» уходило в SEO-поиск
# вместо часов, «сколько станков в сети» — в поиск вместо панели, «курс»
# покрывает и «курс доллара» (слово «доллар» осталось в списке).
_WEB_RE = re.compile(
    r"интернет|погугл|загугл|новост|погод|доллар|евро|биткоин|"
    r"закон|актуальн|википед|найди|поищи|сайт",
    re.IGNORECASE)
_MATH_PREFIX_RE = re.compile(
    r"^(?:сколько будет|посчитай|вычисли|реши|сколько)\s+", re.IGNORECASE)
_THINK_RE = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)
CHAT_CHARS = 1600
_WEB_TOOLS = [
    {"type": "function", "function": {
        "name": "web_search",
        "description": ("Поиск в интернете через включённый веб-поиск Ollama. "
                        "Нужен для свежих фактов: новости, курсы, погода, законы. "
                        "Не передавай имена клиентов, телефоны, суммы и номера заказов."),
        "parameters": {"type": "object", "required": ["query"], "properties": {
            "query": {"type": "string", "description": "Короткий поисковый запрос"},
            "max_results": {"type": "integer", "description": "От 1 до 5"}}}}},
    {"type": "function", "function": {
        "name": "web_fetch",
        "description": "Прочитать публичную http(s)-страницу. Не для адресов этой машины.",
        "parameters": {"type": "object", "required": ["url"], "properties": {
            "url": {"type": "string"}}}}},
]


def catalog_phrase(text: str) -> bool:
    """Команда или вопрос про цех — единственное, что диспетчер имеет право разбирать."""
    return bool(_CATALOG_RE.search(str(text or "")))


def simple_math(text: str) -> str:
    """Арифметика без модели: «2+2» не должен ждать Ollama и не должен стать действием."""
    raw = " ".join(str(text or "").split()).strip().rstrip("?？").strip()
    raw = (raw.replace("×", "*").replace("÷", "/").replace("х", "*")
           .replace(":", "/").replace(",", "."))
    raw = _MATH_PREFIX_RE.sub("", raw).strip().rstrip("=").strip()
    if not raw or len(raw) > 40 or not re.fullmatch(r"[0-9+\-*/().\s]+", raw):
        return ""
    if not re.search(r"\d", raw) or not re.search(r"[+\-*/]", raw):
        return ""
    try:
        tree = ast.parse(raw, mode="eval")
    except SyntaxError:
        return ""
    allowed = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant,
               ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod, ast.FloorDiv,
               ast.USub, ast.UAdd)
    for node in ast.walk(tree):
        if not isinstance(node, allowed):
            return ""
        if isinstance(node, ast.Constant) and not isinstance(node.value, (int, float)):
            return ""
    try:
        value = eval(compile(tree, "<math>", "eval"), {"__builtins__": {}}, {})
    except Exception:
        return ""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return ""
    if isinstance(value, float) and not math.isfinite(value):
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, float):
        shown = f"{value:.4f}".rstrip("0").rstrip(".").replace(".", ",")
    else:
        shown = str(value)
    return shown


def _public_http_url(url: str) -> bool:
    """Страница для web_fetch: только чужой http(s), не панель и не принтер."""
    try:
        parsed = urllib.parse.urlparse(str(url or "").strip())
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").strip().lower()
    if not host or host in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}:
        return False
    if host.endswith(".local") or host.endswith(".lan"):
        return False
    if host.startswith("10.") or host.startswith("192.168.") or host.startswith("169.254."):
        return False
    if host.startswith("172."):
        parts = host.split(".")
        if len(parts) >= 2 and parts[1].isdigit() and 16 <= int(parts[1]) <= 31:
            return False
    return True


def _search_query_ok(query: str) -> bool:
    """В поиск уходит вопрос, не карточка клиента: телефон и сумма остаются на машине."""
    text = " ".join(str(query or "").split())
    if len(text) < 3 or len(text) > 200:
        return False
    if re.search(r"\d{10,}", text) or "₽" in text:
        return False
    return True


def _clean_web_text(value: str) -> str:
    """Выдержку поиска без SEO-хлама.

    Маленькая модель вставляет маркировку ссылок и заголовки страниц
    дословно («++**[сайт](http://сайт)**++»): фидуем ей чистый текст, а
    пересказывать просит промпт.
    """
    text = str(value or "")
    text = re.sub(r"\[[^\]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"\[([^\]]*)\]", r"\1", text)
    text = re.sub(r"[*_+]{1,3}", "", text)
    return " ".join(text.split())


def _format_hits(payload: dict[str, Any]) -> tuple[str, list[dict[str, str]]]:
    rows = payload.get("results") if isinstance(payload, dict) else None
    lines: list[str] = []
    sources: list[dict[str, str]] = []
    for row in (rows or [])[:4]:
        if not isinstance(row, dict):
            continue
        title = _clean_web_text(str(row.get("title") or ""))[:160]
        url = str(row.get("url") or "").strip()[:300]
        content = _clean_web_text(str(row.get("content") or ""))[:400]
        if not title and not content:
            continue
        sources.append({"title": title or url, "url": url})
        lines.append(f"- {title or 'страница'}" + (f" ({url})" if url else "")
                     + (f": {content}" if content else ""))
    return "\n".join(lines), sources


def _ollama_web(db: Database, kind: str, payload: dict[str, Any]) -> tuple[bool, Any, str]:
    """Веб-поиск только через локальную Ollama: она уже вошла в аккаунт и включила поиск.

    PrintFlow сам на ollama.com не ходит. База клиентов в этот запрос не попадает.
    """
    cfg = config(db)
    local, why = _loopback_ok(cfg["url"])
    if not local:
        return False, None, why
    paths = (("/api/experimental/web_search", "/api/web_search") if kind == "search"
             else ("/api/experimental/web_fetch", "/api/web_fetch"))
    last = "веб-поиск Ollama не ответил"
    for path in paths:
        ok, body, reason = _post_json(f"{cfg['url']}{path}", payload, timeout=12)
        if ok and isinstance(body, dict):
            return True, body, ""
        last = reason or last
    return False, None, last


def web_search(db: Database, query: str, max_results: int = 4) -> dict[str, Any]:
    """Поиск через включённый веб-поиск Ollama на этом компьютере."""
    clean = " ".join(str(query or "").split())[:200]
    if not _search_query_ok(clean):
        return {"ok": False, "results": [], "text": "", "sources": [],
                "reason": "Запрос в интернет слишком короткий или похож на данные клиента"}
    try:
        limit = max(1, min(5, int(max_results or 4)))
    except (TypeError, ValueError):
        limit = 4
    ok, body, reason = _ollama_web(db, "search", {"query": clean, "max_results": limit})
    if not ok or not isinstance(body, dict):
        return {"ok": False, "results": [], "text": "", "sources": [],
                "reason": reason or "Веб-поиск Ollama не ответил"}
    text, sources = _format_hits(body)
    if not text:
        return {"ok": False, "results": [], "text": "", "sources": [],
                "reason": "Поиск Ollama ничего не нашёл"}
    return {"ok": True, "results": sources, "text": text, "sources": sources, "reason": ""}


def web_fetch(db: Database, url: str) -> dict[str, Any]:
    """Текст публичной страницы через Ollama. Адрес панели и принтера не открываем."""
    clean = str(url or "").strip()[:300]
    if not _public_http_url(clean):
        return {"ok": False, "text": "", "title": "",
                "reason": "Этот адрес в интернет не отправляю"}
    ok, body, reason = _ollama_web(db, "fetch", {"url": clean})
    if not ok or not isinstance(body, dict):
        return {"ok": False, "text": "", "title": "",
                "reason": reason or "Страница не открылась"}
    title = " ".join(str(body.get("title") or "").split())[:160]
    content = " ".join(str(body.get("content") or "").split())[:2000]
    if not content and not title:
        return {"ok": False, "text": "", "title": "", "reason": "Страница пустая"}
    return {"ok": True, "text": content, "title": title, "reason": ""}


def _needs_web(question: str) -> bool:
    return bool(_WEB_RE.search(str(question or "")))


def _visible_answer(text: str) -> str:
    cleaned = _THINK_RE.sub("", str(text or ""))
    cleaned = re.sub(r"</?think>", "", cleaned, flags=re.IGNORECASE)
    lines = [" ".join(line.split()) for line in cleaned.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _tool_calls(message: dict[str, Any]) -> list[dict[str, Any]]:
    raw = message.get("tool_calls") if isinstance(message, dict) else None
    out: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return out
    for item in raw[:3]:
        if not isinstance(item, dict):
            continue
        fn = item.get("function") if isinstance(item.get("function"), dict) else item
        name = str(fn.get("name") or "").strip()
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        if not isinstance(args, dict):
            args = {}
        if name in ("web_search", "web_fetch"):
            out.append({"name": name, "arguments": args})
    return out


def _run_tool(db: Database, name: str, args: dict[str, Any]) -> tuple[str, list[dict[str, str]]]:
    if name == "web_search":
        found = web_search(db, str(args.get("query") or ""), int(args.get("max_results") or 4))
        return found["text"] or found.get("reason") or "поиск пуст", list(found.get("sources") or [])
    if name == "web_fetch":
        page = web_fetch(db, str(args.get("url") or ""))
        if not page.get("ok"):
            return page.get("reason") or "страница не открылась", []
        return f"{page.get('title') or ''}\n{page.get('text') or ''}".strip(), []
    return "неизвестный инструмент", []


def converse(db: Database, question: str, history: list | None = None,
             context: str = "") -> dict[str, Any]:
    """Обычный вопрос → ответ модели. Свежие факты — через веб-поиск самой Ollama.

    Сюда не попадают команды станку и вопросы про базу цеха: те остаются в
    каталоге и в фактах. В поиск уходит только текст вопроса, не карточки клиентов.

    `context` — дайджест фактов цеха (дата, парк, склад, долги), собранный
    вызывающим из сервисов панели: с ним модель отвечает и на общие вопросы
    по цеху, а не из памяти.
    """
    clean = " ".join(str(question or "").split())
    if not clean:
        return {"ok": False, "answer": "", "model": "", "sources": [], "web": False,
                "reason": "Пустой вопрос", "source": ""}
    math_answer = simple_math(clean)
    if math_answer:
        return {"ok": True, "answer": math_answer, "model": "", "sources": [],
                "web": False, "reason": "", "source": "math"}
    state = status(db)
    if not state.get("available"):
        return {"ok": False, "answer": "", "model": state.get("model", ""),
                "sources": [], "web": False,
                "reason": state.get("reason") or "Модель недоступна", "source": "ollama"}
    cfg = config(db)
    needed = _needs_web(clean)
    sources: list[dict[str, str]] = []
    web_reason = ""
    snippets = ""
    if needed:
        found = web_search(db, clean)
        if found.get("ok"):
            snippets = found["text"]
            sources = list(found.get("sources") or [])
        else:
            web_reason = str(found.get("reason") or "Веб-поиск Ollama не ответил")
    user = clean[:MAX_INPUT_CHARS]
    if snippets:
        user += ("\n\nВыдержки из включённого поиска Ollama. Перескажи их своими "
                 "словами и назови источник одной фразой. Не вставляй ссылки, "
                 "названия сайтов и заголовки страниц. Не выдумывай сверх выдержек.\n"
                 + snippets)
    context_lines = [line for line in str(context or "").splitlines() if line.strip()]
    if not any(line.strip().startswith("Сегодня") for line in context_lines):
        context_lines.insert(0, date_line())
    context_block = "\n".join(context_lines)
    messages: list[dict[str, Any]] = [{
        "role": "system",
        "content": (
            "Ты помощник владельца мастерской 3D-печати. Ты работаешь в панели "
            "PrintFlow и видишь факты цеха в блоке ниже. Отвечай по-русски, "
            "коротко (два-четыре предложения), обычными словами, без заголовков "
            "и без списков ради списков.\n\n"
            f"Факты цеха (из базы, обновлены только что):\n{context_block}\n\n"
            "Правила:\n"
            "1. Цифры — только из фактов цеха или из выдержек поиска. Суммы, "
            "граммы, сроки и названия не выдумывай; если факта нет — честно "
            "скажи, что в базе этого нет.\n"
            "2. Дату и время бери из блока фактов, а не из поиска и не из памяти.\n"
            "3. Свежие факты (погода, новости, курсы) — вызови web_search и "
            "перескажи; если поиска нет — так и скажи, что отвечаешь без интернета.\n"
            "4. В запрос поиска не клади имена клиентов, телефоны, суммы и "
            "номера заказов.\n"
            "5. Команды станкам, заказы и деньги не выполняешь: это делает "
            "человек в панели. На такой запрос коротко подскажи, где это сделать.\n"
            "6. Вопрос не про цех — отвечай по существу, коротко; если не уверен — "
            "скажи, что не уверен."
        ),
    }]
    for turn in (history or [])[-6:]:
        if not isinstance(turn, dict):
            continue
        role = str(turn.get("role") or "")
        if role not in ("user", "assistant"):
            continue
        content = " ".join(str(turn.get("content") or "").split())[:400]
        if content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user})
    tools_on = True
    answer = ""
    for _step in range(3):
        body: dict[str, Any] = {
            "model": cfg["model"], "stream": False,
            "options": {"temperature": 0.2},
            "messages": messages,
        }
        if tools_on:
            body["tools"] = _WEB_TOOLS
        ok, payload, reason = _post_json(
            f"{cfg['url']}/api/chat", body, timeout=cfg["timeout_sec"])
        if not ok and tools_on and ("400" in reason or "tool" in reason.casefold()):
            tools_on = False
            continue
        if not ok or not isinstance(payload, dict):
            return {"ok": False, "answer": "", "model": cfg["model"], "sources": sources,
                    "web": bool(snippets), "reason": f"Рантайм не ответил: {reason}",
                    "source": "ollama", "web_reason": web_reason}
        message = payload.get("message") if isinstance(payload.get("message"), dict) else {}
        calls = _tool_calls(message)
        visible = _visible_answer(str(message.get("content") or payload.get("response") or ""))
        if not calls:
            answer = visible
            break
        messages.append(message)
        for call in calls:
            tool_text, tool_sources = _run_tool(db, call["name"], call["arguments"])
            if tool_sources and not sources:
                sources = tool_sources
            messages.append({
                "role": "tool",
                "name": call["name"],
                "tool_name": call["name"],
                "content": tool_text[:2000],
            })
        if visible and not answer:
            answer = visible
    answer = _visible_answer(answer)[:CHAT_CHARS]
    if not answer:
        return {"ok": False, "answer": "", "model": cfg["model"], "sources": sources,
                "web": bool(snippets or sources),
                "reason": "Модель ответила пустотой", "source": "ollama",
                "web_reason": web_reason}
    warnings = []
    if needed and not snippets and web_reason:
        warnings.append("Поиск Ollama не сработал — ответ без интернета. " + web_reason)
    return {"ok": True, "answer": answer, "model": cfg["model"], "sources": sources[:4],
            "web": bool(snippets or sources), "reason": "", "source": "ollama",
            "web_reason": web_reason, "warnings": warnings}


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
    # «2+2» и любой вопрос не из цеха — не действие. Иначе модель выбирает
    # первое чтение каталога, и панель сама запрашивает состояние парка.
    if not catalog_phrase(clean_text):
        return {"ok": True, "available": True, "reason": "",
                "action": None, "params": {},
                "explain": "Это вопрос, не команда цеху.",
                "warnings": []}
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
        with _local_open(request, cfg["timeout_sec"]) as response:
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
    # Сначала сокет: мёртвый порт не должен держать кнопку «Идеи» на таймауте HTTP.
    up, why = _tcp_up(cfg["url"], 0.35)
    if not up:
        out["reason"] = (
            "Агент компьютера не запущен. Навыки и окна появятся после кнопки "
            "«Запустить агента» в разделе «Компьютер». Идеи для ТГ собираются и без него.")
        return out
    ok, payload, reason = _post_json(f"{cfg['url']}/health", {}, PING_SEC)
    if not ok:
        out["reason"] = (f"Агент на {cfg['url']} не отвечает ({reason}). "
                         "Нажмите «Запустить агента» в разделе «Компьютер».")
        return out
    payload = payload if isinstance(payload, dict) else {}
    out.update(available=True, reason="",
               wake_word=bool(payload.get("wake_word")),
               window=str(payload.get("window") or "")[:200])
    return out


def agent_skills(db: Database) -> dict[str, Any]:
    """Реестр навыков ассистента компьютера (18.16, идеи И136, И191-И205).

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
    result = _call_agent_skill(db, "tg.ideas",
                               {"context": context, "limit": limit},
                               timeout=20.0)
    if result.get("ok") and (result.get("ideas") or result.get("result")):
        return result
    # Агент — отдельная программа. Темы постов из заказов цеха ей не нужны:
    # иначе кнопка «Идеи» показывает только «порт не отвечает».
    return _workshop_tg_ideas(db, context, limit, str(result.get("reason") or ""))


_TG_IDEA_TEMPLATES = (
    "Что напечатали на этой неделе — три изделия с фото",
    "Чем PETG отличается от PLA на ваших деталях",
    "Из чего складывается цена: пластик, время, работа",
    "Как готовится стол перед печатью",
    "Кейс: от сообщения клиента до выдачи",
    "Три ошибки в моделях, из-за которых печать дорожает",
    "Новый цвет или материал на складе",
    "Итог месяца: что печатали и что было сложным",
)


def _workshop_tg_ideas(db: Database, context: str, limit: int,
                       agent_reason: str = "") -> dict[str, Any]:
    """Темы для ТГ из своих заказов, без агента и без модели."""
    try:
        limit = max(1, min(12, int(limit or 8)))
    except (TypeError, ValueError):
        limit = 8
    ideas: list[str] = []
    ctx = " ".join(str(context or "").split())[:200]
    try:
        rows = db.query(
            "SELECT product, material FROM orders "
            "WHERE TRIM(COALESCE(product, '')) <> '' "
            "ORDER BY created_at DESC LIMIT 8")
    except Exception:
        rows = []
    for row in rows or []:
        product = " ".join(str(row.get("product") or "").split())
        material = " ".join(str(row.get("material") or "").split())
        if not product:
            continue
        line = f"Кейс: {product}" + (f" — {material}" if material else "")
        if line not in ideas:
            ideas.append(line)
    if ctx and ctx not in ideas:
        ideas.insert(0, ctx)
    for line in _TG_IDEA_TEMPLATES:
        if line not in ideas:
            ideas.append(line)
        if len(ideas) >= limit:
            break
    return {"ok": True, "ideas": ideas[:limit], "source": "workshop", "model": "",
            "reason": "",
            "hint": ("Темы собраны из заказов цеха. Агент компьютера сейчас не нужен — "
                     "публикация всё равно только после «Подтвердить»."
                     if agent_reason else
                     "Темы собраны из заказов цеха.")}


def start_agent(db: Database) -> dict[str, Any]:
    """Поднять `python -m agent` рядом с репозиторием, не импортируя пакет.

    Агент остаётся чужой программой: коннектор только запускает процесс и ждёт
    порт. Окно не открываем — человек уже в панели помощника.
    """
    cfg = agent_config(db)
    if not cfg["enabled"]:
        return {"ok": False, "started": False, "available": False,
                "reason": "Агент выключен в настройках панели. Включите его там и нажмите ещё раз."}
    local, why = _loopback_ok(cfg["url"])
    if not local:
        return {"ok": False, "started": False, "available": False,
                "reason": f"Адрес агента должен быть этим компьютером: {why}"}
    up, _why = _tcp_up(cfg["url"], 0.3)
    if up:
        state = agent_status(db)
        state["started"] = False
        return state
    root = Path(__file__).resolve().parents[2]
    if not (root / "agent" / "__main__.py").is_file():
        return {"ok": False, "started": False, "available": False,
                "reason": "Рядом с PrintFlow нет папки agent — запуск невозможен."}
    log_path = Path.home() / ".printflow" / "agent-start.log"
    with _AGENT_START_LOCK:
        up, _why = _tcp_up(cfg["url"], 0.2)
        if up:
            state = agent_status(db)
            state["started"] = False
            return state
        env = os.environ.copy()
        env["PRINTFLOW_ASSISTANT_WINDOW"] = "0"
        env.setdefault("PYTHONUTF8", "1")
        flags = 0
        if sys.platform.startswith("win"):
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
                subprocess, "DETACHED_PROCESS", 0)
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_file = open(log_path, "a", encoding="utf-8")
            log_file.write("\n--- старт ---\n")
            log_file.flush()
            subprocess.Popen(
                [sys.executable, "-m", "agent"],
                cwd=str(root), env=env,
                stdout=log_file, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                creationflags=flags,
                start_new_session=not sys.platform.startswith("win"))
        except OSError as exc:
            return {"ok": False, "started": False, "available": False,
                    "reason": f"Не удалось запустить агента: {exc}"}
    deadline = time.time() + 2.8
    while time.time() < deadline:
        if _tcp_up(cfg["url"], 0.2)[0]:
            state = agent_status(db)
            state["started"] = True
            state["reason"] = state.get("reason") or "Агент запущен"
            return state
        time.sleep(0.15)
    tail = ""
    try:
        tail = log_path.read_text(encoding="utf-8", errors="replace")[-240:].strip()
    except OSError:
        tail = ""
    reason = "Агент не открыл порт. Проверьте, что Python видит папку agent."
    if tail:
        reason = f"{reason} Последняя строка: {tail.splitlines()[-1][:180]}"
    return {"ok": False, "started": True, "available": False, "reason": reason}



def tg_post(db: Database, draft_id: int = 0, text: str = "", chat: str = "") -> dict[str, Any]:
    return _call_agent_skill(db, "tg.post",
                             {"draft_id": draft_id, "text": text, "chat": chat},
                             timeout=20.0)


# --- 18.16: архив переписок, связка, расписание, дедуп, календарь, шаблоны ---

def avito_threads(db: Database, limit: int = 30, status: str = "") -> dict[str, Any]:
    return _call_agent_skill(db, "avito.threads", {"limit": limit, "status": status})


def avito_thread_save(db: Database, thread: str, intent: str = "", city: str = "", status: str = "new") -> dict[str, Any]:
    return _call_agent_skill(db, "avito.thread_save",
                             {"thread": thread, "intent": intent, "city": city, "status": status})


def avito_to_order(db: Database, listing_id: int = 0, url: str = "", title: str = "", price: str = "", city: str = "") -> dict[str, Any]:
    return _call_agent_skill(db, "avito.to_order",
                             {"listing_id": listing_id, "url": url, "title": title, "price": price, "city": city},
                             timeout=30.0)


def avito_schedule(db: Database, watch_id: int, interval_hours: int = 0, notify: bool = False) -> dict[str, Any]:
    return _call_agent_skill(db, "avito.schedule",
                             {"watch_id": watch_id, "interval_hours": interval_hours, "notify": notify})


def avito_notify(db: Database, watch_id: int, enabled: bool = True) -> dict[str, Any]:
    return _call_agent_skill(db, "avito.notify", {"watch_id": watch_id, "enabled": enabled})


def avito_dedup(db: Database, limit: int = 20, image_hash: str = "") -> dict[str, Any]:
    return _call_agent_skill(db, "avito.dedup", {"limit": limit, "image_hash": image_hash})


def tg_schedule(db: Database, draft_id: int, planned_at: str, chat: str = "") -> dict[str, Any]:
    return _call_agent_skill(db, "tg.schedule",
                             {"draft_id": draft_id, "planned_at": planned_at, "chat": chat})


def tg_schedules(db: Database, limit: int = 30, status: str = "") -> dict[str, Any]:
    return _call_agent_skill(db, "tg.schedules", {"limit": limit, "status": status})


def tg_template_save(db: Database, name: str, tone: str = "", template: str = "") -> dict[str, Any]:
    return _call_agent_skill(db, "tg.template_save", {"name": name, "tone": tone, "template": template})


def tg_templates(db: Database, limit: int = 30) -> dict[str, Any]:
    return _call_agent_skill(db, "tg.templates", {"limit": limit})


def tg_template_apply(db: Database, template_id: int, facts: str = "", topic: str = "", tone: str = "") -> dict[str, Any]:
    return _call_agent_skill(db, "tg.template_apply",
                             {"template_id": template_id, "facts": facts, "topic": topic, "tone": tone})


def tg_hashtags(db: Database, text: str, limit: int = 6) -> dict[str, Any]:
    return _call_agent_skill(db, "tg.hashtags", {"text": text, "limit": limit})


def tg_search(db: Database, query: str, limit: int = 20) -> dict[str, Any]:
    return _call_agent_skill(db, "tg.search", {"query": query, "limit": limit})


def tg_export(db: Database, status: str = "", limit: int = 100, fmt: str = "md") -> dict[str, Any]:
    return _call_agent_skill(db, "tg.export", {"status": status, "limit": limit, "format": fmt})


def tg_stats(db: Database) -> dict[str, Any]:
    return _call_agent_skill(db, "tg.stats", {})


def tg_idea_save(db: Database, context: str = "", idea: str = "") -> dict[str, Any]:
    return _call_agent_skill(db, "tg.idea_save", {"context": context, "idea": idea})


def tg_ideas_history(db: Database, limit: int = 30, status: str = "") -> dict[str, Any]:
    return _call_agent_skill(db, "tg.ideas_history", {"limit": limit, "status": status})

# --- 18.17: полноценный ассистент ПК (голос+система+зрение+окна+И206-И221) ----

def system_autostart(db, enabled: bool = True, app_name: str = "PrintFlowAssistant") -> dict:
    return _call_agent_skill(db, "system.autostart", {"enabled": enabled, "app_name": app_name})

def system_process_list(db, limit: int = 20) -> dict:
    return _call_agent_skill(db, "system.process_list", {"limit": limit})

def system_audio_device(db, device_id: str = "") -> dict:
    return _call_agent_skill(db, "system.audio_device", {"device_id": device_id})

def system_volume(db, level: int = 0) -> dict:
    return _call_agent_skill(db, "system.volume", {"level": level})

def system_display(db) -> dict:
    return _call_agent_skill(db, "system.display", {})

def system_focus(db, minutes: int = 30) -> dict:
    return _call_agent_skill(db, "system.focus", {"minutes": minutes})

def system_power(db, action: str = "lock") -> dict:
    return _call_agent_skill(db, "system.power", {"action": action})

def system_health(db) -> dict:
    return _call_agent_skill(db, "system.health", {})

def window_active(db) -> dict:
    return _call_agent_skill(db, "window.active", {})

def window_list(db, limit: int = 20) -> dict:
    return _call_agent_skill(db, "window.list", {"limit": limit})

def window_focus(db, title: str = "") -> dict:
    return _call_agent_skill(db, "window.focus", {"title": title})

def window_text(db, title: str = "") -> dict:
    return _call_agent_skill(db, "window.text", {"title": title})

def window_controls(db, title: str = "") -> dict:
    return _call_agent_skill(db, "window.controls", {"title": title})

def window_click(db, x: int = 0, y: int = 0) -> dict:
    return _call_agent_skill(db, "window.click", {"x": x, "y": y})

def window_type(db, text: str = "") -> dict:
    return _call_agent_skill(db, "window.type", {"text": text})

def window_snap(db, left_title: str = "", right_title: str = "") -> dict:
    return _call_agent_skill(db, "window.snap", {"left_title": left_title, "right_title": right_title})

def screen_shot(db, max_side: int = 800) -> dict:
    return _call_agent_skill(db, "screen.shot", {"max_side": max_side})

def screen_region_shot(db, left: int = 0, top: int = 0, right: int = 0, bottom: int = 0) -> dict:
    return _call_agent_skill(db, "screen.region_shot", {"left": left, "top": top, "right": right, "bottom": bottom})

def screen_describe(db, max_side: int = 800) -> dict:
    return _call_agent_skill(db, "screen.describe", {"max_side": max_side})

def screen_find(db, text: str = "") -> dict:
    return _call_agent_skill(db, "screen.find", {"text": text})

def screen_find_and_click(db, text: str = "") -> dict:
    return _call_agent_skill(db, "screen.find_and_click", {"text": text})

def screen_archive(db, title: str = "") -> dict:
    return _call_agent_skill(db, "screen.archive", {"title": title})

def screen_archive_search(db, query: str = "", limit: int = 20) -> dict:
    return _call_agent_skill(db, "screen.archive_search", {"query": query, "limit": limit})

def screen_archive_erase(db) -> dict:
    return _call_agent_skill(db, "screen.archive_erase", {})

def voice_listen(db, seconds: int = 5) -> dict:
    return _call_agent_skill(db, "voice.listen", {"seconds": seconds})

def voice_say(db, text: str = "", tone: str = "") -> dict:
    return _call_agent_skill(db, "voice.say", {"text": text, "tone": tone})

def voice_dictate(db, seconds: int = 5) -> dict:
    return _call_agent_skill(db, "voice.dictate", {"seconds": seconds})

def voice_note(db, text: str = "", due: str = "") -> dict:
    return _call_agent_skill(db, "voice.note", {"text": text, "due": due})

def voice_command(db, text: str = "") -> dict:
    return _call_agent_skill(db, "voice.command", {"text": text})

def voice_profile(db, speed: str = "", tone: str = "", volume: int = 0) -> dict:
    return _call_agent_skill(db, "voice.profile", {"speed": speed, "tone": tone, "volume": volume})

def clipboard_history(db, limit: int = 20) -> dict:
    return _call_agent_skill(db, "clipboard.history", {"limit": limit})

def clipboard_read(db) -> dict:
    return _call_agent_skill(db, "clipboard.read", {})

def clipboard_write(db, text: str = "") -> dict:
    return _call_agent_skill(db, "clipboard.write", {"text": text})

def focus_timer(db, minutes: int = 25, note: str = "") -> dict:
    return _call_agent_skill(db, "scheduler.focus_timer", {"minutes": minutes, "note": note})

def focus_list(db, limit: int = 20) -> dict:
    return _call_agent_skill(db, "scheduler.focus_list", {"limit": limit})

def focus_stop(db, timer_id: int = 0) -> dict:
    return _call_agent_skill(db, "scheduler.focus_stop", {"timer_id": timer_id})

def files_watch(db, path: str = "", enabled: bool = True) -> dict:
    return _call_agent_skill(db, "files.watch", {"path": path, "enabled": enabled})

def files_watches(db, limit: int = 20) -> dict:
    return _call_agent_skill(db, "files.watches", {"limit": limit})

def files_quick_open(db, name: str = "", limit: int = 10) -> dict:
    return _call_agent_skill(db, "files.quick_open", {"name": name, "limit": limit})

def knowledge_preferences(db, key: str = "") -> dict:
    return _call_agent_skill(db, "knowledge.preferences", {"key": key})

def knowledge_preference_save(db, key: str = "", value: str = "") -> dict:
    return _call_agent_skill(db, "knowledge.preference_save", {"key": key, "value": value})

def safety_whitelist(db, limit: int = 100) -> dict:
    return _call_agent_skill(db, "safety.whitelist", {"limit": limit})

def safety_whitelist_save(db, app_name: str = "", allowed: bool = True) -> dict:
    return _call_agent_skill(db, "safety.whitelist_save", {"app_name": app_name, "allowed": allowed})

def assistant_macro(db, name: str = "", steps=None, description: str = "") -> dict:
    return _call_agent_skill(db, "assistant.macro", {"name": name, "steps": steps or [], "description": description})

def assistant_macros(db, limit: int = 20) -> dict:
    return _call_agent_skill(db, "assistant.macros", {"limit": limit})

def assistant_macro_run(db, name: str = "") -> dict:
    return _call_agent_skill(db, "assistant.macro_run", {"name": name})

def system_check(db) -> dict:
    return _call_agent_skill(db, "system.check", {})

def system_install(db, what: str = "pip", confirm_text: str = "") -> dict:
    return _call_agent_skill(db, "system.install", {"what": what, "confirm_text": confirm_text})


def system_watchdog(db, mode: str = "status") -> dict:
    """Надзор за процессами агента и панели (18.19, И263) — только чтение."""
    return _call_agent_skill(db, "system.watchdog", {"mode": mode})


def system_watchdog_arm(db, enabled: str = "on", interval: int = 0, stale_sec: int = 0,
                        max_restarts: int = 0, confirm_text: str = "") -> dict:
    """Вооружить или снять надзор. Поднимает процессы надзиратель, а не панель."""
    return _call_agent_skill(db, "system.watchdog_arm", {
        "enabled": enabled, "interval": interval, "stale_sec": stale_sec,
        "max_restarts": max_restarts, "confirm_text": confirm_text})


def system_watchdog_once(db, roles: str = "", dry: bool = False, confirm_text: str = "") -> dict:
    """Один проход надзора по кнопке: проверить пульс и поднять упавшие роли (И263)."""
    return _call_agent_skill(db, "system.watchdog_once", {
        "roles": roles, "dry": "on" if dry else "off", "confirm_text": confirm_text})
