"""ТГ-посты ассистента (18.16): идеи, черновики, шаблоны, календарь, хештеги, поиск, экспорт.

Идеи И182, И184, И198, И200-И205: ассистент придумывает посты в Telegram-канал цеха из того, что знает
сам — недавних печатей, знаний цеха, фактов из документов, а не из воздуха.

Границы те же, что у `files.ask` и `knowledge.shop`:
  * факты — детерминированные (из индекса, из панели, из заметок);
  * модель — только для формулировки, числа не выдумывает;
  * без модели — шаблоны и список тем, а не отказ.
  * хештеги — локальный словарь (И202), без внешних API.
"""

from __future__ import annotations

import json
import random
import re
from typing import Any

TONE_PRESETS = {
    "деловой": "Спокойно, по-деловому, без эмодзи, с цифрами.",
    "дружелюбный": "Тепло, как будто пишете своим клиентам, с 1-2 эмодзи.",
    "техничный": "С деталями печати: материал, слой, время, для кого.",
    "продающий": "С призывом к действию, но без навязчивости.",
    "короткий": "1-2 предложения, суть и фото.",
    "история": "История одного заказа: от запроса до выдачи.",
}

# И202: локальный словарь хештегов
HASHTAG_DICT = {
    "3d": ["#3dпечать", "#3dprint"],
    "печать": ["#3dпечать", "#аддитивка"],
    "petg": ["#PETG", "#прочный"],
    "pla": ["#PLA", "#экопластик"],
    "адресник": ["#адресник", "#дляпитомцев"],
    "брелок": ["#брелок", "#сувенир"],
    "модель": ["#3dмодель", "#прототип"],
    "заказ": ["#назаказ", "#производство"],
    "цех": ["#цех", "#производство"],
    "москва": ["#москва", "#мск"],
    "спб": ["#спб", "#питер"],
    "игрушка": ["#игрушка", "#подарок"],
    "корпус": ["#корпус", "#деталь"],
    "шестерня": ["#шестерня", "#механика"],
}

BUILTIN_TEMPLATES = [
    {"name": "Подборка недели", "tone": "дружелюбный",
     "template": "На этой неделе печатали:\n{fact}\n\n{detail}\n\nХотите так же — пишите, посчитаем за 10 минут.",
     "vars": ["fact", "detail"]},
    {"name": "Кейс", "tone": "техничный",
     "template": "Кейс: {topic}\nМатериал: {material}, слой 0.2, время {time}\n\n{fact}\n{detail}",
     "vars": ["topic", "material", "time", "fact", "detail"]},
    {"name": "Продающий", "tone": "продающий",
     "template": "Ищете {topic}? Мы уже делаем такое.\n{fact}\nЦена от {price}. Напишите — скинем расчёт.",
     "vars": ["topic", "fact", "price"]},
]

DEFAULT_IDEAS = [
    "Что напечатали на этой неделе — подборка 3-5 изделий с фото",
    "Материал недели: чем PETG отличается от PLA на практике",
    "Как мы считаем цену — из чего складывается стоимость",
    "За кулисами цеха: как готовится стол перед печатью",
    "Кейс: адресники для питомцев — от модели до готового",
    "Топ-3 ошибки в моделях от клиентов и как их избежать",
    "Новинка: новый цвет/материал на полке",
    "Отзыв клиента и что мы сделали",
    "Лайфхак: как подготовить STL чтобы напечаталось дешевле",
    "Итог месяца: сколько напечатали, что было сложного",
    "Вакансия/помощь: ищем руки на сборку/упаковку",
    "Акция недели: скидка на конкретную позицию",
]

TOPIC_TEMPLATES = {
    "деловой": (
        "В цехе на этой неделе:\n{fact}\n\n{detail}\n\nГотовы взять ваши задачи — пишите в личку или на сайте."
    ),
    "дружелюбный": (
        "Привет! 👋\n\n{fact}\n\n{detail}\n\nЕсли нужно такое же — напишите, посчитаем за 10 минут 🙂"
    ),
    "техничный": (
        "Кейс печати: {topic}\n\nМатериал: {material}\nСлой: 0.2 мм, заполнение 20%\nВремя: {time}\n\n{detail}\n\nМодель готовим сами, если нужно."
    ),
    "продающий": (
        "Ищете {topic}? Мы уже печатаем такое.\n\n{fact}\n{detail}\n\nЦена от {price} — напишите, скинем точный расчёт и сроки."
    ),
    "короткий": (
        "{fact} {detail}"
    ),
    "история": (
        "История заказа: {topic}\n\nКлиент пришёл с {source}. Мы {action}.\nРезультат: {fact}\n\n{detail}"
    ),
}


def _clean(text: str, limit: int = 800) -> str:
    return " ".join(str(text or "").split())[:limit]


def _facts_text(facts: list[dict[str, Any]] | None) -> str:
    if not facts:
        return ""
    lines = []
    for f in facts[:5]:
        v = _clean(f.get("value") or f.get("text") or "")
        if v:
            lines.append(v)
    return " · ".join(lines)[:400]


def _tg_config() -> tuple[str, str]:
    """Токен и чат из окружения. Токен не пишется в журнал."""
    import os
    token = str(os.environ.get("PRINTFLOW_TG_BOT_TOKEN") or os.environ.get("TG_BOT_TOKEN") or "").strip()
    chat = str(os.environ.get("PRINTFLOW_TG_CHAT_ID") or os.environ.get("TG_CHAT_ID") or "").strip()
    return token, chat


def post_to_telegram(text: str, chat_id: str = "", token: str = "") -> dict[str, Any]:
    """Отправить текст в ТГ-канал через Bot API. Возвращает dict с ok/reason."""
    import json
    import urllib.parse
    import urllib.request

    body = str(text or "").strip()
    if not body:
        return {"ok": False, "reason": "Пустой текст поста"}

    cfg_token, cfg_chat = _tg_config()
    use_token = str(token or cfg_token).strip()
    use_chat = str(chat_id or cfg_chat).strip()

    if not use_token:
        return {"ok": False, "reason": "Не задан токен ТГ-бота: PRINTFLOW_TG_BOT_TOKEN"}
    if not use_chat:
        return {"ok": False, "reason": "Не задан чат/канал: PRINTFLOW_TG_CHAT_ID"}

    url = f"https://api.telegram.org/bot{use_token}/sendMessage"
    payload = {"chat_id": use_chat, "text": body[:3500], "parse_mode": "HTML",
               "disable_web_page_preview": True}
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json; charset=utf-8",
                                          "User-Agent": "NOZZA-Assistant/18.15"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 — внешний Bot API по запросу владельца
            raw = resp.read(1024 * 1024).decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"ТГ не ответил: {exc.__class__.__name__}: {exc}"}
    try:
        j = json.loads(raw or "{}")
    except Exception:
        return {"ok": False, "reason": "ТГ ответил не JSON"}
    if not j.get("ok"):
        desc = str(j.get("description") or "")[:300]
        return {"ok": False, "reason": f"ТГ ошибка: {desc or 'неизвестно'}"}
    return {"ok": True, "reason": "", "message_id": (j.get("result") or {}).get("message_id")}


def generate_ideas(context: str = "", limit: int = 8,
                   model_status: dict[str, Any] | None = None) -> dict[str, Any]:
    """Придумать темы для ТГ-постов."""
    limit = max(1, min(20, int(limit or 8)))
    ctx = _clean(context, 400)

    # Если есть модель — просим её расширить список
    if model_status and model_status.get("ok"):
        from . import model as model_mod
        prompt = (
            "Ты SMM для цеха 3D-печати. Придумай темы для Telegram-канала цеха.\n"
            f"Контекст цеха: {ctx or 'печать на заказ, адресники, мелкосерийка, PETG/PLA'}\n"
            f"Нужно {limit} тем, каждая — 1 строка, конкретно, без воды.\n"
            "Формат: нумерованный список."
        )
        ans = model_mod.complete(prompt, url=model_status.get("url", ""),
                                 name=model_status.get("model", ""))
        if ans.get("ok") and ans.get("text"):
            lines = []
            for ln in ans["text"].splitlines():
                ln = re.sub(r"^\d+[\).]\s*", "", ln.strip()).strip()
                if len(ln) >= 8:
                    lines.append(ln)
            if lines:
                # дополняем дефолтными если мало
                while len(lines) < limit:
                    cand = random.choice(DEFAULT_IDEAS)
                    if cand not in lines:
                        lines.append(cand)
                return {"ok": True, "ideas": lines[:limit], "model": ans.get("model", ""),
                        "reason": "", "source": "model"}

    # fallback — дефолтные + контекст
    ideas = DEFAULT_IDEAS[:]
    random.shuffle(ideas)
    if ctx:
        ideas = [f"{ctx}: {i.lower()}" if random.random() < 0.3 else i for i in ideas]
    return {"ok": True, "ideas": ideas[:limit], "model": "", "reason": "",
            "source": "template",
            "hint": "Модель недоступна — показаны заготовки. Запустите рантайм для живых идей."}


def draft_post(topic: str, tone: str = "дружелюбный",
               facts_text: str = "", source: str = "",
               model_status: dict[str, Any] | None = None) -> dict[str, Any]:
    """Сгенерить черновик поста."""
    topic_clean = _clean(topic, 200)
    if not topic_clean:
        return {"ok": False, "text": "", "reason": "Пустая тема поста"}
    tone_clean = str(tone or "дружелюбный").strip().lower()
    if tone_clean not in TONE_PRESETS:
        tone_clean = "дружелюбный"

    facts_clean = _clean(facts_text, 600)
    source_clean = _clean(source, 200)

    # Модель
    if model_status and model_status.get("ok"):
        from . import model as model_mod
        prompt = (
            "Ты SMM для цеха 3D-печати NOZZA. Напиши пост в Telegram-канал.\n"
            f"Тема: {topic_clean}\n"
            f"Тон: {tone_clean} ({TONE_PRESETS[tone_clean]})\n"
            f"Факты цеха (используй только их, не выдумывай числа): {facts_clean or 'нет'}\n"
            f"Источник/повод: {source_clean or 'повседневная работа'}\n"
            "Правила:\n"
            "1. 2-3 абзаца, 500-800 знаков.\n"
            "2. Русский, живо, без канцелярита.\n"
            "3. В конце — мягкий призыв: написать/заказать.\n"
            "4. Если тон дружелюбный — 1-2 эмодзи уместно.\n"
            "5. Не придумывай цены и сроки, если их нет в фактах.\n"
        )
        ans = model_mod.complete(prompt, url=model_status.get("url", ""),
                                 name=model_status.get("model", ""))
        if ans.get("ok") and ans.get("text"):
            text = ans["text"].strip()[:2000]
            # проверка чисел как в files.ask — если модель выдумала числа, предупредим
            # но текст не трогаем
            warn = ""
            if facts_clean:
                from .model import numbers_checked
                chk = numbers_checked(text, facts_clean)
                if chk.get("warning"):
                    warn = chk["warning"]
            return {"ok": True, "text": text, "topic": topic_clean, "tone": tone_clean,
                    "model": ans.get("model", ""), "reason": "", "source": "model",
                    "facts": facts_clean, "warning": warn}

    # fallback — шаблон
    tmpl = TOPIC_TEMPLATES.get(tone_clean, TOPIC_TEMPLATES["дружелюбный"])
    fact = facts_clean or f"На этой неделе печатали: {topic_clean.lower()}"
    detail = source_clean or "Фото и видео — в карусели. Детали — в личке."
    material = "PETG" if "петг" in topic_clean.lower() or "petg" in topic_clean.lower() else "PLA/PETG"
    time_est = "4-6 часов" if "адресник" in topic_clean.lower() else "от 2 часов"
    price = "от 300 ₽"
    action = "подобрали материал и профиль, напечатали тестовую партию и согласовали цвет"
    text = tmpl.format(topic=topic_clean, fact=fact, detail=detail,
                       material=material, time=time_est, price=price,
                       source=source_clean or "запросом в ТГ", action=action)
    return {"ok": True, "text": text[:2000], "topic": topic_clean, "tone": tone_clean,
            "model": "", "reason": "", "source": "template",
            "facts": facts_clean,
            "hint": "Модель недоступна — черновик из шаблона. Запустите рантайм для живого текста."}


# ---------------------------------------------------------------------------
# 18.16: шаблоны, хештеги, поиск, экспорт, календарь, идеи (И198, И200-И205)
# ---------------------------------------------------------------------------

def _extract_vars(template: str) -> list[str]:
    return sorted(set(re.findall(r"\{(\w+)\}", str(template or ""))))


def save_template(name: str, tone: str, template_text: str) -> dict[str, Any]:
    name_c = _clean(name, 200)
    if not name_c:
        return {"ok": False, "reason": "Пустое имя шаблона"}
    txt = str(template_text or "").strip()
    if not txt:
        return {"ok": False, "reason": "Пустой текст шаблона"}
    tone_c = str(tone or "").strip().lower()
    if tone_c and tone_c not in TONE_PRESETS:
        tone_c = "дружелюбный"
    vars_list = _extract_vars(txt)
    return {"ok": True, "name": name_c, "tone": tone_c, "template_text": txt[:8000],
            "vars": vars_list}


def apply_template(template_text: str, vars_dict: dict[str, str]) -> dict[str, Any]:
    txt = str(template_text or "")
    if not txt.strip():
        return {"ok": False, "text": "", "reason": "Пустой шаблон"}
    out = txt
    for k, v in (vars_dict or {}).items():
        out = out.replace(f"{{{k}}}", str(v or ""))
    # непокрытые переменные оставляем как есть, но предупреждаем
    missing = _extract_vars(out)
    return {"ok": True, "text": out[:8000], "missing": missing,
            "reason": f"Не заполнены: {', '.join(missing)}" if missing else ""}


def generate_hashtags(text: str, limit: int = 6) -> dict[str, Any]:
    """И202: локальный подбор хештегов по словарю."""
    q = _clean(text, 1000).lower()
    if not q:
        return {"ok": False, "hashtags": [], "reason": "Пустой текст"}
    found: list[str] = []
    for key, tags in HASHTAG_DICT.items():
        if key in q:
            for t in tags:
                if t not in found:
                    found.append(t)
    # добавим общие если мало
    if len(found) < 3:
        for t in ["#3dпечать", "#цех", "#назаказ"]:
            if t not in found:
                found.append(t)
    limit = max(1, min(12, int(limit or 6)))
    return {"ok": True, "hashtags": found[:limit], "reason": ""}


def search_drafts(query: str, drafts: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """И203: поиск по черновикам — если drafts переданы, фильтрует в памяти."""
    q = _clean(query, 200).lower()
    if not q:
        return {"ok": False, "results": [], "reason": "Пустой запрос"}
    if drafts is None:
        # поиск делает store, здесь только заглушка
        return {"ok": True, "results": [], "query": q, "reason": "Передайте список черновиков"}
    res = []
    for d in drafts:
        hay = f"{d.get('topic','')} {d.get('text','')}".lower()
        if q in hay:
            res.append(d)
    return {"ok": True, "results": res[:50], "count": len(res), "query": q}


def export_drafts_text(drafts: list[dict[str, Any]], fmt: str = "md") -> dict[str, Any]:
    """И204: экспорт черновиков в текст (md/json)."""
    fmt_c = str(fmt or "md").lower()
    if fmt_c not in ("md", "json", "txt"):
        fmt_c = "md"
    if not drafts:
        return {"ok": False, "text": "", "reason": "Нет черновиков для экспорта"}
    if fmt_c == "json":
        text = json.dumps(drafts, ensure_ascii=False, indent=2)[:20000]
        return {"ok": True, "text": text, "format": "json", "count": len(drafts)}
    # md / txt
    parts = []
    for d in drafts:
        topic = _clean(d.get("topic") or "", 200)
        tone = d.get("tone") or ""
        txt = _clean(d.get("text") or "", 2000)
        status = d.get("status") or "draft"
        at = d.get("at") or ""
        if fmt_c == "md":
            parts.append(f"## {topic} [{tone}] ({status}) {at}\n\n{txt}\n")
        else:
            parts.append(f"{topic} [{tone}] {status}\n{txt}\n---\n")
    out = "\n".join(parts)[:20000]
    return {"ok": True, "text": out, "format": fmt_c, "count": len(drafts)}


def schedule_post(draft_id: int, planned_at: str, chat: str = "") -> dict[str, Any]:
    """И198: добавить в календарь."""
    if not draft_id:
        return {"ok": False, "reason": "Не указан draft_id"}
    pa = _clean(planned_at, 30)
    if not pa:
        return {"ok": False, "reason": "Пустая дата публикации"}
    # простая проверка формата YYYY-MM-DD HH:MM или YYYY-MM-DD
    if not re.match(r"^\d{4}-\d{2}-\d{2}", pa):
        return {"ok": False, "reason": "Дата должна быть YYYY-MM-DD или YYYY-MM-DD HH:MM"}
    return {"ok": True, "draft_id": int(draft_id), "planned_at": pa,
            "chat": _clean(chat, 200), "status": "planned"}


def idea_to_draft_link(idea_text: str, draft_text: str) -> dict[str, Any]:
    """И205: связать идею с черновиком — эвристика по совпадению слов."""
    idea = _clean(idea_text, 500).lower()
    draft = _clean(draft_text, 1000).lower()
    if not idea or not draft:
        return {"ok": False, "linked": False, "reason": "Пустая идея или черновик"}
    # считаем общие слова
    iw = set(idea.split())
    dw = set(draft.split())
    common = iw & dw
    score = len(common) / max(1, len(iw))
    linked = score >= 0.3 or any(w in draft for w in idea.split()[:3])
    return {"ok": True, "linked": bool(linked), "score": round(score, 2),
            "common": list(common)[:10]}
