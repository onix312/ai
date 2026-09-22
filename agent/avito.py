"""Авито-слежка ассистента (18.15): поиск, разбор, варианты ответа.

Почему этот модуль внутри агента, а не в коннекторе.

Авито — внешняя площадка, но слежка за ней — личное дело владельца на его
компьютере: запросы, фильтры и переписка не должны уезжать в базу заказов.
Поэтому всё здесь: своя таблица `avito_watches`, парсинг без зависимостей,
варианты ответа через локальный рантайм модели (как `files.ask`).

Границы:
  * сеть — обычный urllib с таймаутом и User-Agent, без loopback-проверки:
    Авито — внешний сайт, и `panel_client.loopback_ok` к нему неприменим;
  * парсинг — stdlib `html.parser` плюс эвристики по `data-marker`:
    верстка Авито меняется, поэтому парсер ищет несколько признаков и
    возвращает то, что нашёл, с причиной, если ничего не нашлось;
  * модель — как в `model.py`: если рантайм недоступен, варианты ответа
    собираются из шаблонов, а не падают.
"""

from __future__ import annotations

import html
import re
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any

AVITO_BASE = "https://www.avito.ru"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) NOZZA-Assistant/18.15"
TIMEOUT_SEC = 15
MAX_HTML = 2 * 1024 * 1024

# ---------------------------------------------------------------------------
# Сеть
# ---------------------------------------------------------------------------

def fetch_html(url: str, timeout: float = TIMEOUT_SEC) -> tuple[str, str]:
    """Скачать страницу. Возвращает (html, reason). Пустой html — отказ."""
    raw_url = str(url or "").strip()
    if not raw_url:
        return "", "Пустой адрес"
    # Для тестов: file:// или путь к файлу
    if raw_url.startswith("file://"):
        try:
            path = urllib.parse.urlparse(raw_url).path
            # Windows file:// parsing
            if not path:
                path = raw_url[7:]
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return f.read(MAX_HTML), ""
        except OSError as exc:
            return "", f"Файл не читается: {exc.__class__.__name__}"
    if raw_url.startswith("/") or raw_url.startswith("./"):
        try:
            with open(raw_url, "r", encoding="utf-8", errors="replace") as f:
                return f.read(MAX_HTML), ""
        except OSError as exc:
            return "", f"Файл не читается: {exc.__class__.__name__}"

    # Внешний URL — без loopback проверки, но только https/http
    parsed = urllib.parse.urlsplit(raw_url)
    if parsed.scheme not in ("http", "https"):
        return "", f"Схема «{parsed.scheme}» не поддерживается"
    req = urllib.request.Request(raw_url, headers={"User-Agent": USER_AGENT,
                                                   "Accept-Language": "ru,en;q=0.8"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — внешний сайт по запросу владельца
            data = resp.read(MAX_HTML)
    except Exception as exc:  # noqa: BLE001 — сеть, любая ошибка — причина
        return "", f"Авито не ответил: {exc.__class__.__name__}: {exc}"
    try:
        return data.decode("utf-8", "replace"), ""
    except Exception:
        return data.decode("cp1251", "replace"), ""


# ---------------------------------------------------------------------------
# Парсинг
# ---------------------------------------------------------------------------

class _AvitoParser(HTMLParser):
    """Ищет объявления по признакам data-marker."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[dict[str, Any]] = []
        self._cur: dict[str, Any] | None = None
        self._depth = 0
        self._item_depth = 0
        self._in_title = False
        self._in_price = False
        self._buf = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        d = {k: (v or "") for k, v in attrs}
        marker = d.get("data-marker", "")
        # Начало карточки
        if marker == "item" or ("data-item-id" in d and tag == "div"):
            if self._cur is None:
                self._cur = {"external_id": d.get("data-item-id") or d.get("id") or "",
                             "title": "", "price": "", "url": "", "city": "", "snippet": ""}
                self._item_depth = self._depth
                self._buf = ""
        if self._cur is not None:
            # url
            if tag == "a" and d.get("href"):
                href = d["href"]
                if href.startswith("/") and not self._cur["url"]:
                    self._cur["url"] = href
                elif href.startswith("https://www.avito.ru") and not self._cur["url"]:
                    self._cur["url"] = href
                # title from attribute
                if (marker in ("item-title", "title") or "title" in d) and not self._cur["title"]:
                    t = d.get("title") or ""
                    if t and len(t) > 3:
                        self._cur["title"] = t.strip()
            # title text
            if marker in ("item-title", "title", "item-title-text"):
                self._in_title = True
                self._buf = ""
            # price
            if marker in ("item-price", "price", "item-price-text") or d.get("itemprop") == "price":
                self._in_price = True
                self._buf = ""
                # price from content attribute
                if d.get("content") and not self._cur["price"]:
                    self._cur["price"] = d["content"].strip()
            # city
            if marker in ("item-address", "address", "location"):
                if not self._cur["city"]:
                    # will capture in data
                    self._cur["_need_city"] = True  # type: ignore
        self._depth += 1

    def handle_endtag(self, tag: str) -> None:
        self._depth -= 1
        if self._in_title:
            if self._cur is not None and self._buf.strip():
                if not self._cur["title"]:
                    self._cur["title"] = self._buf.strip()[:300]
            self._in_title = False
            self._buf = ""
        if self._in_price:
            if self._cur is not None and self._buf.strip():
                if not self._cur["price"]:
                    self._cur["price"] = self._buf.strip()[:120]
            self._in_price = False
            self._buf = ""
        # конец карточки
        if self._cur is not None and self._depth <= self._item_depth:
            # финализируем
            cur = self._cur
            cur.pop("_need_city", None)
            # минимальная валидация
            if cur.get("title") or cur.get("url"):
                # нормализуем url
                url = cur.get("url") or ""
                if url.startswith("/"):
                    url = AVITO_BASE + url
                cur["url"] = url
                # external_id fallback
                if not cur.get("external_id"):
                    cur["external_id"] = url or cur.get("title") or ""
                self.items.append(cur)
            self._cur = None

    def handle_data(self, data: str) -> None:
        if self._in_title or self._in_price:
            self._buf += data
        if self._cur is not None and self._cur.get("_need_city"):  # type: ignore
            txt = data.strip()
            if txt and len(txt) < 120:
                # эвристика города: есть запятая или короткая строка
                if not self._cur["city"]:
                    self._cur["city"] = txt[:120]
                self._cur.pop("_need_city", None)  # type: ignore


def parse_listings(html_text: str) -> list[dict[str, Any]]:
    """Разобрать HTML страницы поиска Авито в список объявлений."""
    text = str(html_text or "")
    if not text.strip():
        return []

    # Попытка 1: HTMLParser по data-marker
    parser = _AvitoParser()
    try:
        parser.feed(text[:MAX_HTML])
    except Exception:
        pass
    items = [it for it in parser.items if (it.get("title") or it.get("url"))]

    # Попытка 2: JSON внутри страницы — ищем "title":"...","price":...
    if not items:
        # Ищем блоки с urlPath
        # Пример: "urlPath":"/moskva/bytovaya_tehnika/..." , "title":"..."
        pattern = re.compile(
            r'"urlPath"\s*:\s*"(?P<url>[^"]+)"[^}]{0,600}?"title"\s*:\s*"(?P<title>[^"]+)"'
            r'|"title"\s*:\s*"(?P<title2>[^"]+)"[^}]{0,600}?"urlPath"\s*:\s*"(?P<url2>[^"]+)"',
            re.DOTALL)
        for m in pattern.finditer(text):
            url = m.group("url") or m.group("url2") or ""
            title = m.group("title") or m.group("title2") or ""
            if url and title:
                url = html.unescape(url)
                title = html.unescape(title)
                if url.startswith("/"):
                    url = AVITO_BASE + url
                items.append({"external_id": url, "title": title[:300], "price": "",
                              "url": url, "city": "", "snippet": ""})
            if len(items) >= 50:
                break

    # Попытка 3: простые ссылки /..._<digits>
    if not items:
        link_re = re.compile(r'<a[^>]+href="(?P<href>/[^"]+_\d+)"[^>]*>(?P<title>[^<]{5,120})</a>')
        for m in link_re.finditer(text):
            href = m.group("href")
            title = html.unescape(m.group("title")).strip()
            url = AVITO_BASE + href if href.startswith("/") else href
            items.append({"external_id": url, "title": title[:300], "price": "",
                          "url": url, "city": "", "snippet": ""})
            if len(items) >= 30:
                break

    # Дедупликация по url
    seen: set[str] = set()
    uniq: list[dict[str, Any]] = []
    for it in items:
        key = str(it.get("url") or it.get("title") or "")[:600]
        if not key or key in seen:
            continue
        seen.add(key)
        # гарантируем поля
        it.setdefault("price", "")
        it.setdefault("city", "")
        it.setdefault("snippet", "")
        it.setdefault("external_id", it.get("url") or it.get("title") or "")
        uniq.append(it)
    return uniq[:50]


def build_search_url(query: str, city: str = "", category: str = "",
                     max_price: int = 0, min_price: int = 0) -> str:
    """Собрать URL поиска Авито из параметров слежки."""
    base = AVITO_BASE
    if city:
        # город как поддомен или путь: упрощаем — /city
        city_slug = re.sub(r"[^a-z0-9_-]+", "-", city.strip().lower()).strip("-")
        if city_slug:
            base = f"{AVITO_BASE}/{city_slug}"
    if category:
        cat = category.strip().strip("/")
        base = f"{base}/{cat}" if not base.endswith(f"/{cat}") else base
    params: dict[str, str] = {}
    if query:
        params["q"] = query.strip()
    if min_price and min_price > 0:
        params["pmin"] = str(int(min_price))
    if max_price and max_price > 0:
        params["pmax"] = str(int(max_price))
    if params:
        return f"{base}?{urllib.parse.urlencode(params)}"
    return base


def search(query: str, city: str = "", category: str = "",
           max_price: int = 0, min_price: int = 0,
           limit: int = 20) -> dict[str, Any]:
    """Одноразовый поиск: скачать и разобрать."""
    url = build_search_url(query, city, category, max_price, min_price)
    html_text, reason = fetch_html(url)
    if reason:
        return {"ok": False, "url": url, "listings": [], "count": 0, "reason": reason}
    listings = parse_listings(html_text)[:max(1, min(50, int(limit or 20)))]
    if not listings:
        return {"ok": True, "url": url, "listings": [], "count": 0,
                "reason": "На странице не нашлось объявлений — верстка Авито могла измениться или выдача пустая",
                "hint": "Попробуйте изменить запрос или откройте URL в браузере"}
    return {"ok": True, "url": url, "listings": listings, "count": len(listings),
            "reason": "", "hint": f"Найдено {len(listings)}"}


# ---------------------------------------------------------------------------
# Варианты ответа
# ---------------------------------------------------------------------------

REPLY_TONES = ("вежливый", "короткий", "подробный", "торг", "деловой")
REPLY_TEMPLATES = {
    "вежливый": [
        "Здравствуйте! Спасибо за сообщение. {context} Готов ответить на вопросы и показать товар вживую.",
        "Добрый день! {context} Подскажите, когда вам удобно созвониться или встретиться?",
        "Здравствуйте! {context} Товар на месте, состояние как в описании. Могу отправить доп. фото.",
    ],
    "короткий": [
        "{context} На месте. Когда заберёте?",
        "Да, актуально. {context}",
        "{context} Готов показать сегодня.",
    ],
    "подробный": [
        "Здравствуйте! {context} Покупал для себя, причина продажи — {reason}. Состояние отличное, есть чек. Могу показать в {city} или отправить доставкой.",
        "Добрый день! {context} Полный комплект, без скрытых дефектов. Торг уместен при осмотре. Пишите, договоримся.",
    ],
    "торг": [
        "Здравствуйте! {context} Цена обсуждаема, но в пределах разумного — уже ниже рынка. Готов скинуть {discount} при быстрой сделке.",
        "Добрый день! {context} Могу уступить немного, если заберёте сегодня.",
    ],
    "деловой": [
        "Добрый день! {context} Работаю как ИП, могу выдать чек/накладную. Товар в наличии на складе в {city}.",
        "Здравствуйте! {context} Оплата наличные/перевод, возможна доставка. Гарантия на проверку.",
    ],
}


def _template_replies(thread_text: str, intent: str, city: str = "") -> list[str]:
    context = " ".join(str(thread_text).split())[:200] or "По вашему запросу"
    reason = "перешёл на другое" if "почему" in thread_text.lower() else "не нужен"
    discount = "500 ₽" if "дорого" in thread_text.lower() else "немного"
    out: list[str] = []
    for tone in REPLY_TONES:
        for tmpl in REPLY_TEMPLATES[tone][:1]:
            txt = tmpl.format(context=context, reason=reason,
                              city=city or "городе", discount=discount)
            # интент добавляем
            if intent:
                txt = f"{txt} ({intent})"
            out.append(f"[{tone}] {txt}")
    return out[:6]


def suggest_replies(thread_text: str, intent: str = "", city: str = "",
                    model_status: dict[str, Any] | None = None) -> dict[str, Any]:
    """Предложить варианты ответа на сообщение Авито."""
    text = " ".join(str(thread_text or "").split()).strip()
    if not text:
        return {"ok": False, "replies": [], "reason": "Пустой текст переписки"}

    # Если модель доступна — зовём её, иначе шаблоны
    if model_status and model_status.get("ok"):
        from . import model as model_mod
        prompt = (
            "Ты помощник продавца на Авито. Придумай 3 варианта ответа на сообщение покупателя.\n"
            f"Переписка: \"\"\"{text[:1000]}\"\"\"\n"
            f"Намерение продавца: {intent or 'ответить вежливо и довести до сделки'}\n"
            f"Город: {city or 'не указан'}\n"
            "Правила:\n"
            "1. Коротко, по-русски, без лишней воды.\n"
            "2. Варианты: вежливый, короткий, с торгом.\n"
            "3. Не обещай того, чего нет в переписке.\n"
            "4. Каждый вариант с новой строки, нумерация 1. 2. 3.\n"
        )
        ans = model_mod.complete(prompt, url=model_status.get("url", ""),
                                 name=model_status.get("model", ""))
        if ans.get("ok") and ans.get("text"):
            lines = [ln.strip() for ln in ans["text"].splitlines() if ln.strip()]
            # чистим нумерацию
            cleaned = []
            for ln in lines:
                ln = re.sub(r"^\d+[\).]\s*", "", ln).strip()
                if len(ln) >= 10:
                    cleaned.append(ln)
            if cleaned:
                return {"ok": True, "replies": cleaned[:5], "model": ans.get("model", ""),
                        "reason": "", "source": "model"}
    # fallback — шаблоны
    replies = _template_replies(text, intent, city)
    return {"ok": True, "replies": replies, "model": "", "reason": "",
            "source": "template",
            "hint": "Модель недоступна — показаны шаблоны. Запустите рантайм для живых вариантов."}
