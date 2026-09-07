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
from typing import Any

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
    text = _SPACES.sub(" ", str(value or "").replace("|", " ")).strip()
    return text[:limit] if limit else text


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
    """Платёжная ссылка банка с подстановкой суммы и назначения."""
    text = str(template or "").strip()
    if not text:
        return ""
    value = num(amount)
    rub = f"{value:.2f}".rstrip("0").rstrip(".") if value else ""
    return (text.replace("{amount}", rub)
                .replace("{amount_kop}", str(kopecks(value)) if value else "")
                .replace("{number}", str(number or ""))
                .replace("{purpose}", _clean(purpose)))


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
    amount = round(num(amount), 2)
    purpose = _clean(purpose)

    result: dict[str, Any] = {
        "mode": mode, "kind": "", "text": "", "svg": "",
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
        return result
    if with_svg:
        result["svg"] = render_svg(result["text"])
        if not result["svg"]:
            result["hint"] = "Строка не помещается в QR — сократите реквизиты"
    return result
