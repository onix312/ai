"""Генератор платёжного QR: PrintFlow рисует код оплаты сам.

Зачем модуль. Платёжный QR СБП (``qr.nspk.ru/AS…``) выпускает НСПК через
банк-эквайер: сгенерировать его «на своей стороне» нельзя — ссылку выдаёт
банк после подключения торговой точки. Но платёж с уже подставленной суммой
получить можно и без эквайринга: банковские приложения России читают QR
формата **ГОСТ Р 56042-2014** («оплата по реквизитам»), а его мы собираем
полностью локально из реквизитов счёта.

Поэтому у кассы три источника кода, режим выбирается настройкой
``pay_qr_mode``:

``static``
    Статический QR магазина из банка (``sbp_shop_qr``) — настоящий СБП,
    самая низкая комиссия, но сумму покупатель вводит руками.
``gost``
    Реквизитный QR по ГОСТ Р 56042-2014, который PrintFlow генерирует сам:
    сумма и назначение уже внутри, работает в любом банковском приложении,
    зачисление — обычным переводом на расчётный счёт.
``link``
    Шаблон платёжной ссылки банка (``pay_qr_link``) с подстановкой
    ``{amount}``, ``{amount_kop}``, ``{number}``, ``{purpose}`` — для банков,
    которые умеют ссылку с суммой.
``auto`` (по умолчанию)
    Динамический QR платежа от банка → готовый ГОСТ-код с суммой →
    шаблон ссылки → статический QR магазина.

Строка кодируется встроенным генератором (``qrgen``, версии 1–10). Чтобы код
гарантированно поместился и сканировался, длинный payload собирается
экономно: сначала режется назначение, затем необязательные КПП/ИНН.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any
from urllib.parse import quote, urlsplit

from .accounting import num

# Заголовок формата: ST — стандарт, 0001 — версия, 2 — кодировка UTF-8.
GOST_HEADER = "ST00012"
MODES = ("auto", "gost", "link", "static", "off")
DEFAULT_MODE = "auto"

# Ограничения ГОСТ Р 56042-2014 (раздел 5.2) на длину значений.
FIELD_LIMITS = {
    "Name": 160, "PersonalAcc": 20, "BankName": 45, "BIC": 9, "CorrespAcc": 20,
    "PayeeINN": 12, "KPP": 9, "Purpose": 210,
}
# Встроенный генератор поддерживает версии 1–10: 271 байт на уровне L
# и 213 на уровне M. Держим запас, чтобы код читался с экрана телефона.
MAX_QR_BYTES = 271
COMFORT_QR_BYTES = 213
MIN_PURPOSE = 12  # ниже этого назначение не режем — лучше убрать целиком

_DIGITS = re.compile(r"\D+")
_SPACES = re.compile(r"\s+")


def _clean(value: Any, limit: int = 0) -> str:
    """Значение реквизита без разделителей формата и лишних пробелов."""
    text = str(value or "").replace("|", " ")
    # Управляющие символы в QR не пускаем (кроме пробельных — они схлопнутся).
    text = "".join(ch for ch in text
                   if ch in " \t\n\r\f\v" or unicodedata.category(ch) != "Cc")
    text = _SPACES.sub(" ", text).strip()
    return text[:limit] if limit else text


def safe_open_url(text: Any) -> str:
    """Можно ли открыть payload кнопкой «Открыть оплату».

    Разрешён только ``https://`` без логина/пароля в адресе: ``javascript:``,
    ``data:``, deep link приложений и ссылки с пробелами/кавычками —
    не открываем. Домен не ограничиваем жёстким списком (банки разные),
    но возвращаем его отдельно в ``domain_of`` для показа и диагностики.
    """
    raw = str(text or "").strip()
    if not raw or len(raw) > 2000:
        return ""
    if any(ch.isspace() for ch in raw):
        return ""
    if "<" in raw or ">" in raw or '"' in raw:
        return ""
    try:
        parts = urlsplit(raw)
    except ValueError:
        return ""
    if parts.scheme.lower() != "https":
        return ""
    if not parts.hostname:
        return ""
    if parts.username or parts.password:
        return ""
    return raw


def domain_of(text: Any) -> str:
    """Домен payload-ссылки для диагностики (пусто — не ссылка)."""
    try:
        return str(urlsplit(str(text or "").strip()).hostname or "")
    except ValueError:
        return ""


def qr_purpose_of(kind: str, text: str) -> str:
    """Назначение, фактически лежащее внутри QR (ГОСТ: поле Purpose)."""
    if kind != "gost" or not text:
        return ""
    for part in str(text).split("|"):
        if part.startswith("Purpose="):
            return part[len("Purpose="):]
    return ""


def _digits(value: Any) -> str:
    return _DIGITS.sub("", str(value or ""))


def kopecks(amount: Any) -> int:
    """Сумма в копейках — ГОСТ требует целое без разделителя."""
    return int(round(num(amount) * 100))


# --------------------------------------------------------------- реквизиты
def requisites(db) -> dict[str, str]:
    """Реквизиты получателя из настроек (с запасными значениями)."""
    s = db.settings() if hasattr(db, "settings") else {}
    return {
        "Name": _clean(s.get("pay_payee_name") or s.get("legal_name")
                       or s.get("company_name"), FIELD_LIMITS["Name"]),
        "PersonalAcc": _digits(s.get("pay_account")),
        "BankName": _clean(s.get("pay_bank_name"), FIELD_LIMITS["BankName"]),
        "BIC": _digits(s.get("pay_bic")),
        "CorrespAcc": _digits(s.get("pay_corr_account")),
        "PayeeINN": _digits(s.get("pay_payee_inn") or s.get("inn")),
        "KPP": _digits(s.get("pay_payee_kpp")),
    }


def check(req: dict[str, str]) -> list[dict[str, str]]:
    """Что мешает собрать ГОСТ-код: пустые и некорректные реквизиты.

    Возвращает список проблем для экрана настроек — с человеческим текстом,
    а не «ошибка валидации». Пустой список означает «код можно печатать».
    """
    problems: list[dict[str, str]] = []

    def bad(field: str, label: str, message: str) -> None:
        problems.append({"field": field, "label": label, "message": message})

    if not req.get("Name"):
        bad("pay_payee_name", "Получатель",
            "укажите наименование получателя платежа (ИП/ООО)")
    account = req.get("PersonalAcc") or ""
    if len(account) != 20:
        bad("pay_account", "Расчётный счёт",
            "20 цифр" + (f", а сейчас {len(account)}" if account else " — пусто"))
    if not req.get("BankName"):
        bad("pay_bank_name", "Банк", "укажите наименование банка получателя")
    bic = req.get("BIC") or ""
    if len(bic) != 9:
        bad("pay_bic", "БИК",
            "9 цифр" + (f", а сейчас {len(bic)}" if bic else " — пусто"))
    corr = req.get("CorrespAcc") or ""
    if len(corr) != 20:
        bad("pay_corr_account", "Корр. счёт",
            "20 цифр" + (f", а сейчас {len(corr)}" if corr else " — пусто"))
    elif len(bic) == 9 and corr[-3:] != bic[-3:]:
        # Классическая проверка платёжки: хвост корсчёта повторяет хвост БИК.
        bad("pay_corr_account", "Корр. счёт",
            f"последние 3 цифры должны совпадать с БИК ({bic[-3:]})")
    inn = req.get("PayeeINN") or ""
    if inn and len(inn) not in (10, 12):
        bad("pay_payee_inn", "ИНН", "10 цифр у организации или 12 у ИП")
    kpp = req.get("KPP") or ""
    if kpp and len(kpp) != 9:
        bad("pay_payee_kpp", "КПП", "9 цифр")
    return problems


# ------------------------------------------------------------- сборка строк
def gost_payload(req: dict[str, str], amount: float = 0.0,
                 purpose: str = "") -> str:
    """Строка платёжного QR по ГОСТ Р 56042-2014.

    Обязательные реквизиты идут первыми, затем ИНН/КПП, сумма в копейках и
    назначение. Если строка не помещается во встроенный генератор, лишнее
    убирается по приоритету: назначение → КПП → ИНН, а сами реквизиты счёта
    остаются нетронутыми — без них платёж не проведётся.
    """
    problems = check(req)
    if problems:
        raise ValueError("Не хватает реквизитов: "
                         + ", ".join(f"{p['label']} — {p['message']}" for p in problems))
    base = [GOST_HEADER]
    for key in ("Name", "PersonalAcc", "BankName", "BIC", "CorrespAcc"):
        base.append(f"{key}={_clean(req.get(key), FIELD_LIMITS[key])}")
    optional: list[str] = []
    if req.get("PayeeINN"):
        optional.append(f"PayeeINN={req['PayeeINN'][:FIELD_LIMITS['PayeeINN']]}")
    if req.get("KPP"):
        optional.append(f"KPP={req['KPP'][:FIELD_LIMITS['KPP']]}")
    money = [f"Sum={kopecks(amount)}"] if num(amount) > 0 else []
    note = _clean(purpose, FIELD_LIMITS["Purpose"])

    def build(opts: list[str], text: str) -> str:
        tail = ([f"Purpose={text}"] if text else [])
        return "|".join(base + opts + money + tail)

    payload = build(optional, note)
    if _fits(payload):
        return payload
    # 1) режем назначение до предела читаемости
    while note and len(note) > MIN_PURPOSE and not _fits(payload):
        note = note[:-8].rstrip()
        payload = build(optional, note)
    if _fits(payload):
        return payload
    # 2) убираем назначение, затем КПП и ИНН
    for candidate in (build(optional, ""),
                      build([o for o in optional if not o.startswith("KPP=")], ""),
                      build([], "")):
        if _fits(candidate):
            return candidate
        payload = candidate
    raise ValueError("Реквизиты не помещаются в QR — сократите наименования "
                     "получателя и банка")


def _fits(payload: str) -> bool:
    return len(payload.encode("utf-8")) <= MAX_QR_BYTES


def link_payload(template: str, amount: float = 0.0, purpose: str = "",
                 number: str = "") -> str:
    """Платёжная ссылка банка с подстановкой суммы и назначения.

    Назначение и номер URL-кодируются: пробелы и ``&`` иначе рвут ссылку
    (18.0 — раньше подставлялись как есть и ломали платёж).
    """
    text = str(template or "").strip()
    if not text:
        return ""
    value = num(amount)
    rub = f"{value:.2f}".rstrip("0").rstrip(".") if value else ""
    return (text.replace("{amount}", rub)
                .replace("{amount_kop}", str(kopecks(value)) if value else "")
                .replace("{number}", quote(str(number or ""), safe=""))
                .replace("{purpose}", quote(_clean(purpose), safe="")))


def render_svg(text: str, scale: int = 5) -> str:
    """Компактный SVG платёжного QR.

    Уровень коррекции подбирается под длину строки, а тёмные модули строки
    склеиваются в один ``path``: картинка получается в разы легче поштучных
    прямоугольников — важно, потому что её отдаёт API кассы.
    """
    if not text:
        return ""
    from .qrgen import matrix
    level = "M" if len(text.encode("utf-8")) <= COMFORT_QR_BYTES else "L"
    try:
        mod = matrix(text, level=level)
    except Exception:
        return ""
    border = 2
    size = len(mod)
    side = size + border * 2
    parts: list[str] = []
    for y, row in enumerate(mod):
        x = 0
        while x < size:
            if not row[x]:
                x += 1
                continue
            run = 1
            while x + run < size and row[x + run]:
                run += 1
            parts.append(f"M{x + border} {y + border}h{run}v1h-{run}z")
            x += run
    px = max(1, int(scale))
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {side} {side}"'
            f' width="{side * px}" height="{side * px}" shape-rendering="crispEdges">'
            f'<rect width="{side}" height="{side}" fill="#ffffff"/>'
            f'<path d="{"".join(parts)}" fill="#111111"/></svg>')


# ------------------------------------------------------------------- сборка
def mode_of(db) -> str:
    value = str(db.setting("pay_qr_mode", DEFAULT_MODE) or DEFAULT_MODE).strip().lower()
    return value if value in MODES else DEFAULT_MODE


def build(db, amount: float = 0.0, purpose: str = "", number: str = "",
          payment: dict | None = None, with_svg: bool = True) -> dict[str, Any]:
    """Что показать плательщику: строка QR, картинка и понятная подсказка.

    Никогда не бросает исключение: касса и панель должны открыться даже с
    пустыми реквизитами — тогда в ответе будет ``problems`` и текст, что
    именно дозаполнить.
    """
    settings = db.settings() if hasattr(db, "settings") else {}
    mode = mode_of(db)
    dynamic = _clean((payment or {}).get("qr_payload"))
    static = str(settings.get("sbp_shop_qr") or "").strip()
    template = str(settings.get("pay_qr_link") or "").strip()
    req = requisites(db)
    problems = check(req)
    # «Камерный» приоритет (17.0.10): ссылка вместо ГОСТ-текста, чтобы обычная
    # камера телефона предлагала открыть банк, а не показывала строку.
    camera = bool(db.setting("pay_qr_camera", False))
    amount = round(num(amount), 2)
    purpose = _clean(purpose)

    result: dict[str, Any] = {
        "mode": mode, "kind": "", "text": "", "svg": "", "camera": camera,
        "amount": amount, "amount_in_qr": False, "purpose": purpose,
        "bank_name": str(settings.get("sbp_bank_name") or ""),
        "problems": problems, "hint": "", "enabled": mode != "off",
    }
    if mode == "off":
        result["hint"] = "Показ QR отключён настройкой «Режим платёжного QR»"
        return result

    def use(kind: str, text: str, amount_in_qr: bool) -> bool:
        if not text:
            return False
        result.update({"kind": kind, "text": text, "amount_in_qr": amount_in_qr})
        return True

    def gost() -> bool:
        if problems:
            return False
        try:
            return use("gost", gost_payload(req, amount, purpose), amount > 0)
        except ValueError as exc:
            result["hint"] = str(exc)
            return False

    order = {
        "gost": (gost,),
        "link": (lambda: use("link", link_payload(template, amount, purpose, number),
                             bool(template and "{amount" in template and amount > 0)),),
        "static": (lambda: use("static", static, False),),
    }
    if mode == "auto":
        if camera:
            # Порядок для камеры: то, что телефон открывает тапом, важнее суммы
            # внутри кода. Ссылку без суммы банк всё равно примет — покупатель
            # введёт её сам, и это осознанный выбор владельца.
            chain = [lambda: use("dynamic", dynamic, True), order["link"][0],
                     order["static"][0], gost]
        else:
            chain = [lambda: use("dynamic", dynamic, True), gost,
                     order["link"][0], order["static"][0]]
    else:
        chain = [lambda: use("dynamic", dynamic, True)] + list(order[mode])
    for step in chain:
        if step():
            break

    if not result["text"]:
        if mode in ("auto", "gost") and problems:
            result["hint"] = ("Заполните реквизиты для платёжного QR: "
                              + "; ".join(f"{p['label']} — {p['message']}"
                                          for p in problems))
        elif mode == "link":
            result["hint"] = ("Задайте шаблон платёжной ссылки банка "
                              "(настройка «Ссылка для оплаты»)")
        elif mode == "static":
            result["hint"] = ("QR магазина не задан: вставьте ссылку из банка в "
                              "настройку «Статический QR магазина (СБП)»")
        result["diagnostics"] = _diagnostics(result, template, purpose)
        result["open_url"] = ""
        result["can_open"] = False
        return result
    if with_svg:
        result["svg"] = render_svg(result["text"])
        if not result["svg"]:
            result["hint"] = "Строка не помещается в QR — сократите реквизиты"
    result["diagnostics"] = _diagnostics(result, template, purpose)
    result["open_url"] = str(result["diagnostics"]["open_url"])
    result["can_open"] = bool(result["diagnostics"]["can_open"])
    return result


DIAG_SOURCE = {
    "dynamic": "bank_dynamic", "gost": "gost_local",
    "link": "bank_link_template", "static": "shop_static", "": "none",
}


def _diagnostics(result: dict[str, Any], template: str,
                 purpose: str) -> dict[str, Any]:
    """Безопасная диагностика QR для кассы и панели (18.0).

    Отвечает на вопросы «что внутри», «откроется ли банк» и «почему текст» —
    без секретов: только тип, домен и флаги. ``purpose_in_qr=None`` означает
    «содержимое кода банка нам неизвестно» (честное «не знаю»).
    """
    kind = str(result.get("kind") or "")
    text = str(result.get("text") or "")
    open_url = safe_open_url(text) if text else ""
    domain = domain_of(text) if open_url else ""
    qr_purpose = qr_purpose_of(kind, text)
    if kind == "gost":
        purpose_in_qr: bool | None = bool(qr_purpose)
        purpose_truncated = bool(purpose) and qr_purpose != purpose
    elif kind == "link":
        purpose_in_qr = bool(purpose) and "{purpose}" in str(template or "")
        purpose_truncated = False
    elif kind in ("dynamic", "static"):
        purpose_in_qr = None
        purpose_truncated = False
    else:
        purpose_in_qr = False
        purpose_truncated = False
    can_open = bool(open_url)
    if kind == "gost":
        why = ("Платёжный QR по реквизитам: камера покажет текст. "
               "Откройте приложение банка и отсканируйте внутри него.")
        fallback = "bank_app"
    elif kind == "link":
        why = (f"Ссылка банка ({domain}): камера предложит открыть."
               if domain else "Ссылка банка: камера предложит открыть.")
        fallback = "web"
    elif kind == "dynamic":
        why = "Динамический QR банка: сумма внутри."
        fallback = "web" if can_open else "bank_app"
    elif kind == "static":
        why = ("QR магазина — это ссылка: камера предложит открыть банк. "
               "Сумму покупатель вводит сам."
               if can_open else "QR магазина: сумму вводит покупатель.")
        fallback = "web" if can_open else "bank_app"
    else:
        why = str(result.get("hint") or "QR не настроен")
        fallback = "none"
    return {
        "kind": kind, "source": DIAG_SOURCE.get(kind, "none"), "domain": domain,
        "static": kind == "static", "amount_in_qr": bool(result.get("amount_in_qr")),
        "purpose_in_qr": purpose_in_qr, "purpose_truncated": purpose_truncated,
        "qr_purpose": qr_purpose, "open_url": open_url, "can_open": can_open,
        "expect_bank_chooser": can_open, "fallback": fallback, "why": why,
    }
