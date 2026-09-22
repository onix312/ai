"""ТГ-посты ассистента (18.15): идеи и черновики из фактов цеха.

Идея И182: ассистент придумывает посты в Telegram-канал цеха из того, что знает
сам — недавних печатей, знаний цеха, фактов из документов, а не из воздуха.

Границы те же, что у `files.ask` и `knowledge.shop`:
  * факты — детерминированные (из индекса, из панели, из заметок);
  * модель — только для формулировки, числа не выдумывает;
  * без модели — шаблоны и список тем, а не отказ.
"""

from __future__ import annotations

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
