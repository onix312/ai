"""Намерения без модели (18.22): обучение, личные дела, мелкие расчёты.

Мозг (`brain.py`) разбирает команды компьютеру; здесь — всё, что делает
помощника личным и обучаемым:

  * `teach_command` — урок: «научись: когда я говорю «X» — делай Y»,
    «когда я спрашиваю X, отвечай Y», ««телега» — это телеграм», «забудь
    команду X», «чему ты научился», «что ты не понял»;
  * `correction` — поправка после ошибки: «нет, я имел в виду Y»;
  * `personal_intent` — напоминания, списки, цели, привычки, расходы,
    дневник, «мой день»;
  * `util_answer` — дни до даты, день недели, перевод единиц, монетка.

Функции получают уже очищенную фразу (`brain.normalize_phrase`) и ничего не
исполняют: результат — план навыка из реестра или готовый ответ. Исполнение,
проверки и подтверждение — там же, где у всех навыков.
"""
from __future__ import annotations

import datetime as dt
import random
import re
from typing import Any

from . import when
from .personal import CATEGORIES, list_name, split_items

_RANDOM = random.SystemRandom()
_Q = r"[«\"“„']"
_QE = r"[»\"”“']"
_NOT_Q = r"[^»\"”“]"
_SAY = r"(?:говорю|скажу|пишу|напишу|произношу|прошу|спрашиваю|спрошу)"
_HEAD = r"(?:(?:научись|выучи|запомни|учись|новая\s+команда)(?:\s+команду)?\s*[:,—–-]?\s*)"

# (выражение, явный урок, подсказка «это синоним»). Неявный — ««X» — это Y» без
# «научись»: такая фраза бывает и фактом («Рыжик» — это клиент из Питера), поэтому
# урок из неё получается, только если Y — понятная команда или короткий синоним.
_TEACH_RES = (
    (re.compile(r"^" + _HEAD + r"?(?:когда|если)\s+я\s+(?:тебе\s+)?" + _SAY + r"\s+" + _Q + r"(?P<x>" + _NOT_Q + r"+?)" + _QE
               + r"\s*[,:—–-]*\s*(?:то\s+|значит\s+|это\s+значит\s+)?(?P<y>.+)$", re.IGNORECASE), True, False),
    (re.compile(r"^" + _HEAD + r"?(?:когда|если)\s+я\s+(?:тебе\s+)?" + _SAY
                + r"\s+(?P<x>.+?)\s*(?:,|—|–|:|\s-\s|\s+то\s+|\s+значит\s+)\s*(?P<y>.+)$", re.IGNORECASE), True, False),
    (re.compile(r"^" + _HEAD + _Q + r"(?P<x>" + _NOT_Q + r"+?)" + _QE
                + r"\s*(?:[,:—–-]+\s*|\s+)(?:это\s+|значит\s+|означает\s+)?(?P<y>.+)$", re.IGNORECASE), True, False),
    (re.compile(r"^" + _Q + r"(?P<x>" + _NOT_Q + r"+?)" + _QE + r"\s*(?:[—–-]\s*это|[—–-]|значит|означает|это)\s+(?P<y>.+)$",
                re.IGNORECASE), False, False),
    (re.compile(r"^на\s+(?:фразу|слово|вопрос|команду)\s+" + _Q + r"?(?P<x>" + _NOT_Q + r"+?)" + _QE
                + r"?\s*[,:—–-]?\s*(?P<y>(?:отвечай|ответь|говори|скажи)\s+.+)$", re.IGNORECASE), True, False),
    (re.compile(r"^под\s+" + _Q + r"?(?P<x>" + _NOT_Q + r"+?)" + _QE
                + r"?\s+я\s+(?:имею|подразумеваю)\s+(?:в\s+виду\s+)?(?P<y>.+)$", re.IGNORECASE), True, True),
)
_SPLIT_HEAD = re.compile(r"^" + _HEAD + r"?(?:когда|если)\s+я\s+(?:тебе\s+)?" + _SAY + r"\s+(?P<rest>.+)$", re.IGNORECASE)
_ANSWER_Y = re.compile(r"^(?:отвечай|ответь|говори|скажи|отвечаешь|говоришь)\s*[:,—–-]?\s*(?P<a>.+)$", re.IGNORECASE)
_DO_Y = re.compile(r"^(?:делай|сделай|выполняй|выполни|делаешь|ты\s+делаешь|запускай|я\s+имею\s+в\s+виду|имею\s+в\s+виду|"
                   r"это\s+значит|значит)\s*[:,—–-]?\s*", re.IGNORECASE)
_ALIAS_Y = re.compile(r"^(?:я\s+)?(?:имею\s+в\s+виду|подразумеваю)\s+", re.IGNORECASE)
_FORGET_RE = re.compile(r"^(?:забудь|разучись|удали|сотри)\s+(?:команду|фразу|выученное|урок|слово|синоним)\s+" + _Q
                        + r"?(?P<x>" + _NOT_Q + r"+?)" + _QE + r"?$", re.IGNORECASE)
_LIST_RE = re.compile(r"(?:чему\s+ты\s+(?:уже\s+)?(?:научился|выучился|обучился|научилась|выучилась)|"
                      r"что\s+ты\s+(?:уже\s+)?(?:выучил|выучила|знаешь\s+из\s+уроков)|(?:твои|мои)\s+(?:команды|уроки)|"
                      r"покажи\s+(?:выученное|обучение|уроки)|список\s+выученного)", re.IGNORECASE)
_UNKNOWN_RE = re.compile(r"(?:что|чего|какие\s+фразы)\s+ты\s+(?:так\s+и\s+)?не\s+(?:понял|поняла|понимаешь)", re.IGNORECASE)
_HABITS_RE = re.compile(r"(?:что\s+ты\s+(?:за\s+мной\s+|обо\s+мне\s+)?заметил\w*|что\s+я\s+обычно\s+(?:делаю|прошу)|"
                        r"мои\s+(?:рабочие\s+)?привычки\s+на\s+компьютере|мой\s+распорядок)", re.IGNORECASE)

_CORRECTION_RE = re.compile(
    r"^(?:нет|неа|не\s+то|не\s+так|неправильно|неверно|ошибка|ты\s+ошибся|ты\s+ошиблась|не\s+это|я\s+не\s+это\s+\w+)"
    r"(?:[\s,.!:—–-]+(?:я\s+)?(?:же\s+)?(?P<mark>имел\w*\s+в\s+виду|хотел\w*|просил\w*|говорил\w*|сказал\w*|"
    r"надо\s+было|нужно\s+было|надо|нужно|а)?\s*[:,—–-]?\s*(?P<y>.*))?$", re.IGNORECASE)
_THIS_MEANS_RE = re.compile(r"^(?:это\s+значит|это\s+означает|значит|означает|то\s+есть|это|я\s+имел\w*\s+в\s+виду|"
                            r"имею\s+в\s+виду|имел\w*\s+в\s+виду|научись|сделай\s+так)\s*[:,—–-]?\s*(?P<y>.+)$",
                            re.IGNORECASE)
CANCEL_RE = re.compile(r"^(?:отмена|отмени|не\s+надо|ничего|неважно|забудь|проехали|отбой|хватит|стоп|никак|нет)$",
                       re.IGNORECASE)
_MULTI_SPLIT = re.compile(r"\s*(?:,\s*(?:а\s+)?потом|;\s*|\s+и\s+потом\s+|\s+потом\s+|\s+затем\s+|,\s*затем\s+|"
                          r",\s*|\s+и\s+(?=(?:открой|откройте|включи|выключи|запусти|поставь|сделай|покажи|громкость|"
                          r"звук|закрой|сверни|разверни|переключи|напомни|добавь|запиши|пауза|найди|тише|громче|"
                          r"прибавь|убавь|нажми|введи|запомни|забудь|скажи|озвучь|засеки|останови|разложи|прочитай|"
                          r"вырубь|замьют|выведи)))\s*", re.IGNORECASE)


def teach_command(phrase: str) -> dict[str, Any] | None:
    """Урок, забывание или просьба показать выученное. None — это не про обучение."""
    text = phrase.strip()
    low = text.casefold().replace("ё", "е")
    match = _FORGET_RE.match(text)
    if match:
        return {"op": "forget", "phrase": match.group("x").strip(" .,!?")}
    if _LIST_RE.search(low):
        return {"op": "list"}
    if _UNKNOWN_RE.search(low):
        return {"op": "unknown"}
    if _HABITS_RE.search(low):
        return {"op": "habits"}
    for pattern, explicit, alias in _TEACH_RES:
        match = pattern.match(text)
        if match:
            lesson = _lesson(match.group("x"), match.group("y"))
            if lesson:
                lesson["explicit"] = explicit
                lesson["alias_hint"] = bool(lesson.get("alias_hint") or alias)
            return lesson
    match = _SPLIT_HEAD.match(text)
    if match:
        return {"op": "teach", "phrase": "", "meaning": "", "split": match.group("rest").strip(), "explicit": True}
    return None


def _lesson(x: str, y: str) -> dict[str, Any] | None:
    phrase = x.strip(" .,!?:—–-«»\"")
    meaning = y.strip(" .!?")
    if not phrase or not meaning:
        return None
    answer = _ANSWER_Y.match(meaning)
    if answer and not re.match(r"^(?:скажи|говори)\s+вслух", meaning, re.IGNORECASE):
        return {"op": "answer", "phrase": phrase, "answer": answer.group("a").strip(" «»\"")}
    alias = bool(_ALIAS_Y.match(meaning))
    meaning = _ALIAS_Y.sub("", meaning)
    meaning = _DO_Y.sub("", meaning).strip(" «»\"")
    return {"op": "teach", "phrase": phrase, "meaning": meaning, "alias_hint": alias}


def split_meaning(meaning: str) -> list[str]:
    """«Открой Telegram и поставь громкость 30» → два шага (не больше восьми)."""
    parts = [part.strip(" .,;") for part in _MULTI_SPLIT.split(meaning) if part and part.strip(" .,;")]
    return parts[:8] or [meaning]


def correction(phrase: str) -> dict[str, Any] | None:
    match = _CORRECTION_RE.match(phrase.strip())
    if not match:
        return None
    return {"meaning": (match.group("y") or "").strip(" .!?«»\""), "marked": bool(match.group("mark"))}


def this_means(phrase: str) -> str:
    """«Это значит громкость 30» → «громкость 30»; иначе пусто."""
    match = _THIS_MEANS_RE.match(phrase.strip())
    return match.group("y").strip(" .!?«»\"") if match else ""


# ---------------------------------------------------------------------------
# Личные дела
# ---------------------------------------------------------------------------

_REMIND_RE = re.compile(r"\b(?:напомни\w*|напоминай|напоминани[ея])\b", re.IGNORECASE)
_REMIND_LIST_RE = re.compile(r"(?:мои|какие|все|список|покажи|есть\s+ли)\s+(?:у\s+меня\s+)?(?:мои\s+)?напоминани|"
                             r"что\s+ты\s+(?:мне\s+)?(?:должен\s+)?напомн|о\s+ч[её]м\s+(?:ты\s+)?(?:мне\s+)?напомнишь|"
                             r"^напоминания\??$", re.IGNORECASE)
_REMIND_CANCEL_RE = re.compile(r"^(?:отмени|удали|убери|сотри|отключи)\s+(?:все\s+)?напоминани\w*\s*"
                               r"(?:про|о|об|насчет|насчёт)?\s*(?P<q>.*)$", re.IGNORECASE)
_SNOOZE_RE = re.compile(r"^(?:отложи(?:\s+напоминание)?|напомни\s+(?:позже|попозже)|позже|попозже)"
                        r"(?:\s+(?:на|еще\s+на|ещё\s+на)\s+(?:(?P<n>" + when.NUM + r")\s*)?(?P<u>мин\w*|час\w*)?)?$",
                        re.IGNORECASE)
_ABOUT_RE = re.compile(r"^(?:мне\s+)?(?:про|о|об|обо|насчет|насчёт)\s+", re.IGNORECASE)

_LIST_WORDS = (r"(?P<list>покуп\w*|продукт\w*|дел\w*|задач\w*|книг\w*|фильм\w*|кино|сериал\w*|подарк\w*|подарков|"
               r"идей|иде[ия]|желани\w*|лекарств\w*|вещ\w*)")
_LIST_ADD_RES = (
    re.compile(r"^(?:добавь|запиши|внеси|закинь|положи|занеси)\s+(?P<items>.+?)\s+(?:в|во)\s+(?:мой\s+|мои\s+)?"
               r"(?:список\s+)?" + _LIST_WORDS + r"$", re.IGNORECASE),
    re.compile(r"^(?:добавь|запиши|внеси|закинь|занеси)\s+(?:в|во)\s+(?:мой\s+)?(?:список\s+)?" + _LIST_WORDS
               + r"\s*[:,—–-]?\s+(?P<items>.+)$", re.IGNORECASE),
    re.compile(r"^(?:в\s+)?(?:список\s+)?(?P<list>покупки|дела)\s*[:—–-]\s*(?P<items>.+)$", re.IGNORECASE),
)
_BUY_RE = re.compile(r"^(?:надо|нужно|не\s+забыть|купи\s+мне|мне\s+надо)\s+купить\s+(?P<items>.+)$", re.IGNORECASE)
_LIST_SHOW_RES = (
    re.compile(r"(?:что|чего)\s+(?:у\s+меня\s+)?(?:есть\s+)?(?:в|во)\s+(?:моем\s+|моём\s+)?(?:списке\s+)?"
               + _LIST_WORDS + r"\b", re.IGNORECASE),
    re.compile(r"(?:покажи|открой|прочитай|какой|мой)\s+(?:мой\s+)?список\s+" + _LIST_WORDS, re.IGNORECASE),
)
_LIST_BUY_SHOW_RE = re.compile(r"^что\s+(?:мне\s+)?(?:нужно\s+|надо\s+)?купить\??$", re.IGNORECASE)
_LISTS_ALL_RE = re.compile(r"^(?:мои\s+списки|какие\s+у\s+меня\s+(?:есть\s+)?списки|покажи\s+(?:мои\s+|все\s+)?списки|"
                           r"списки)$", re.IGNORECASE)
_LIST_REMOVE_RE = re.compile(r"^(?:вычеркни|удали|убери|сотри)\s+(?P<item>.+?)(?:\s+из\s+(?:списка\s+)?"
                             + _LIST_WORDS + r")?$", re.IGNORECASE)
_BOUGHT_RE = re.compile(r"^(?:я\s+)?(?:уже\s+)?(?:купил|купила|купили|взял|взяла)\s+(?P<item>[^0-9]+?)$", re.IGNORECASE)
_LIST_CLEAR_RE = re.compile(r"^(?:очисти|удали|сотри|обнули)\s+(?:весь\s+)?список\s+" + _LIST_WORDS + r"$",
                            re.IGNORECASE)

_GOAL_ADD_RES = (
    re.compile(r"^(?:моя\s+|новая\s+)?цель\s*(?:[:—–-]|\s+это)\s*(?P<body>.+)$", re.IGNORECASE),
    re.compile(r"^(?:поставь|добавь|запиши|создай|заведи)\s+(?:мне\s+)?(?:новую\s+)?цель\s*[:—–-]?\s*(?P<body>.+)$",
               re.IGNORECASE),
    re.compile(r"^(?:моя\s+)?цель\s+(?P<body>.*\d.*)$", re.IGNORECASE),
)
_GOAL_LIST_RE = re.compile(r"(?:мои|какие\s+у\s+меня|как\s+(?:там\s+)?мои|покажи(?:\s+мои)?)\s+цел|прогресс\s+по\s+цел|"
                           r"как\s+(?:идут|продвигаются)\s+(?:мои\s+)?цел|^цели\??$", re.IGNORECASE)
_GOAL_REMOVE_RE = re.compile(r"^(?:удали|убери|сотри|отмени)\s+цель\s+(?P<goal>.+)$", re.IGNORECASE)
_GOAL_PLUS_RE = re.compile(r"^(?:\+|плюс)\s*(?P<n>" + when.NUM + r")\s*(?:к\s+|в\s+)?(?:цели\s+)?(?P<goal>.*)$",
                           re.IGNORECASE)
_GOAL_ADD_PROGRESS_RE = re.compile(r"^(?:добавь|отметь|запиши|засчитай)\s+(?P<n>" + when.NUM + r")\s*(?P<unit>[а-яa-z]+)?"
                                   r"\s+(?:к|в|по)\s+цели\s*(?P<goal>.*)$", re.IGNORECASE)
_GOAL_SET_RE = re.compile(r"^(?:прогресс|по\s+цели)\s+(?:по\s+)?(?:цели\s+)?(?P<goal>.+?)\s*[:—–-]?\s*"
                          r"(?P<n>\d+(?:[.,]\d+)?)\s*[а-я%₽]*$", re.IGNORECASE)
_DID_RE = re.compile(r"^(?:я\s+)?(?:(?:сегодня|уже|еще|ещё)\s+)*(?P<verb>прочитал\w*|прочел|прочла|пробежал\w*|проехал\w*|"
                     r"проплыл\w*|прошел|прошла|прошли|сделал\w*|отложил\w*|накопил\w*|выучил\w*|написал\w*|сбросил\w*|"
                     r"скинул\w*|посмотрел\w*|решил\w*|отжался|отжалась|подтянулся|подтянулась|выпил\w*|заработал\w*|"
                     r"продал\w*|напечатал\w*|собрал\w*|закрыл\w*)\s+(?:ещ[её]\s+)?(?P<n>" + when.NUM
                     + r")\s+(?P<unit>[а-яa-z%]+)", re.IGNORECASE)

_HABIT_ADD_RES = (
    re.compile(r"^(?:новая|добавь|заведи|создай|начни\s+отслеживать)\s+привычк\w*\s*[:—–-]?\s*(?P<body>.+)$",
               re.IGNORECASE),
    re.compile(r"^(?:хочу|буду)\s+(?:привыкнуть|приучиться|приучить\s+себя)\s+(?P<body>.+)$", re.IGNORECASE),
    re.compile(r"^(?:моя\s+)?привычка\s*[:—–-]\s*(?P<body>.+)$", re.IGNORECASE),
)
_HABIT_LIST_RE = re.compile(r"(?:мои|какие\s+у\s+меня|как\s+(?:там\s+)?мои|покажи(?:\s+мои)?)\s+привычк(?!\w*\s+на\s+компьютер)|"
                            r"серии\s+привычек|^привычки\??$", re.IGNORECASE)
_HABIT_REMOVE_RE = re.compile(r"^(?:удали|убери|брось|бросаю|перестань\s+отслеживать)\s+привычку\s+(?P<habit>.+)$",
                              re.IGNORECASE)
_HABIT_MARK_RE = re.compile(r"^(?:отметь|отмечаю|засчитай)\s+(?:привычку\s+)?(?P<what>.+)$", re.IGNORECASE)
_HABIT_DONE_RE = re.compile(r"^(?P<what>.+?)\s+(?:сделан\w*|выполнен\w*)$", re.IGNORECASE)
_PAST_RE = re.compile(r"^(?:я\s+)?(?:(?:сегодня|уже)\s+)*(?P<verb>[а-яё]+(?:л|ла|ли|лся|лась))\s+(?P<what>.+)$",
                      re.IGNORECASE)

_EXPENSE_RE = re.compile(r"^(?:я\s+)?(?P<day>сегодня\s+|вчера\s+)?(?:потратил\w*|заплатил\w*|отдал\w*|оплатил\w*|"
                         r"купил\w*|расход|трата|минус|спустил\w*)\s+(?P<rest>.+)$", re.IGNORECASE)
_AMOUNT_RE = re.compile(r"(?<![\w.])(?P<n>\d{1,3}(?:[\s ]\d{3})+|\d+(?:[.,]\d{1,2})?)\s*(?P<k>к|тыс\w*|т\.?р\.?)?"
                        r"\s*(?:руб\w*|р\.?|₽)?(?![а-яa-z\d])", re.IGNORECASE)
_EXPENSE_REPORT_RE = re.compile(r"(?:сколько\s+(?:я\s+)?(?:всего\s+)?(?:потратил\w*|трачу|ушло\s+денег)|"
                                r"(?:мои\s+)?(?:личные\s+)?(?:расходы|траты)\s+(?:за|в|на|по)\b|"
                                r"(?:покажи|какие)\s+(?:мои\s+)?(?:личные\s+)?(?:расходы|траты)|"
                                r"^(?:мои\s+)?(?:личные\s+)?(?:расходы|траты)$)", re.IGNORECASE)
_CATEGORY_RE = re.compile(r"[,\s]+(?:категория|в\s+категорию|по\s+категории)\s+(?P<cat>[а-яa-z ]{2,30})$", re.IGNORECASE)

_DIARY_ADD_RE = re.compile(r"^(?:запиши\s+в\s+дневник|в\s+дневник|дневник|заметка\s+в\s+дневник|запись\s+в\s+дневник)"
                           r"\s*[:,—–-]?\s*(?P<text>.+)$", re.IGNORECASE)
_MOOD_RE = re.compile(r"^(?:у\s+меня\s+)?(?:сегодня\s+)?(?:настроение|самочувствие)\s*[:—–-]?\s*(?P<m>.+)$", re.IGNORECASE)
_MOOD_READ_RE = re.compile(r"(?:мо[её]\s+настроение|настроение\s+за\s+(?:неделю|месяц))", re.IGNORECASE)
_DIARY_READ_RE = re.compile(r"^(?:что\s+я\s+(?:писал\w*|записывал\w*)(?:\s+в\s+дневник\w*)?|покажи\s+(?:мой\s+)?дневник|"
                            r"(?:мой\s+)?дневник\s+за|прочитай\s+(?:мой\s+)?дневник|мой\s+дневник)\s*(?P<when>.*)$",
                            re.IGNORECASE)
_TODAY_RE = re.compile(r"^(?:(?:покажи|расскажи|скажи|какой)\s+)?(?:доброе\s+утро|мой\s+день|что\s+у\s+меня\s+(?:на\s+)?сегодня|мои\s+дела(?:\s+на\s+сегодня)?|"
                       r"какие\s+у\s+меня\s+(?:дела|планы)(?:\s+на\s+сегодня)?|план\s+на\s+(?:сегодня|день)|"
                       r"что\s+на\s+сегодня|утренняя\s+сводка|сводка\s+дня)$", re.IGNORECASE)


def _plan(skill: str, params: dict[str, Any], why: str) -> dict[str, Any]:
    return {"skill": skill, "params": {key: value for key, value in params.items() if value not in (None, "")},
            "why": why}


def amount_of(text: str) -> tuple[float | None, tuple[int, int] | None]:
    match = _AMOUNT_RE.search(text)
    if not match:
        return None, None
    raw = re.sub(r"[\s ]", "", match.group("n")).replace(",", ".")
    try:
        value = float(raw)
    except ValueError:
        return None, None
    if match.group("k"):
        value *= 1000
    return value, match.span()


def parse_goal(body: str, today: dt.date) -> dict[str, Any]:
    """«прочитать 12 книг до конца года» → {title, target 12, unit «книг», deadline 31.12}."""
    text = " ".join(body.split()).strip(" .!")
    deadline = None
    found = when.target_date(text, today, new_year_is_first=False)
    if found:
        deadline, (start, end) = found
        text = (text[:start] + " " + text[end:]).strip()
    text = " ".join(text.split()).strip(" ,.")
    target, unit = 0.0, ""
    match = re.search(r"(?P<n>\d+(?:[.,]\d+)?(?:\s?000)*|" + when.NUM + r")\s*(?P<unit>%|₽|[а-яa-z]+)?", text, re.IGNORECASE)
    if match:
        raw = match.group("n").replace(" ", "")
        target = when.number(raw) or 0.0
        unit = (match.group("unit") or "").casefold()
        if unit.startswith(("руб", "₽")):
            unit = "₽"
        elif unit.startswith("тыс"):
            target, unit = target * 1000, "₽"
    return {"title": text[:160], "target": target, "unit": unit[:20],
            "deadline": deadline.isoformat() if deadline else ""}


_MONTHS_NOM = ("январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь",
               "ноябрь", "декабрь")
_MONTH_IN = (r"январ\w*", r"феврал\w*", r"март\w*", r"апрел\w*", r"ма[ея]", r"июн\w*", r"июл\w*", r"август\w*",
             r"сентябр\w*", r"октябр\w*", r"ноябр\w*", r"декабр\w*")


def period(text: str, today: dt.date) -> tuple[str, dt.date, dt.date]:
    """«за неделю», «вчера», «в сентябре» → (подпись, первый день, последний день)."""
    low = str(text or "").casefold().replace("ё", "е")
    if "сегодня" in low:
        return "сегодня", today, today
    if "вчера" in low:
        day = today - dt.timedelta(days=1)
        return "вчера", day, day
    if re.search(r"недел", low):
        if re.search(r"\bэт(?:ой|у)\s+недел", low):
            return "за эту неделю", today - dt.timedelta(days=today.weekday()), today
        return "за 7 дней", today - dt.timedelta(days=6), today
    if re.search(r"\bгод", low):
        return "за год", today.replace(month=1, day=1), today
    for index, stem in enumerate(_MONTH_IN):
        if re.search(r"\b(?:в|за)\s+" + stem + r"\b", low):
            year = today.year if index + 1 <= today.month else today.year - 1
            start = dt.date(year, index + 1, 1)
            end = (dt.date(year + 1, 1, 1) if index == 11 else dt.date(year, index + 2, 1)) - dt.timedelta(days=1)
            return f"за {_MONTHS_NOM[index]}", start, min(end, today)
    return "за этот месяц", today.replace(day=1), today


def personal_intent(phrase: str, personal: Any, now: dt.datetime) -> dict[str, Any] | None:
    """Фраза → план личного навыка или уточнение («Когда напомнить?»). None — не личное."""
    text = phrase.strip()
    low = text.casefold().replace("ё", "е")
    if not low:
        return None
    today = now.date()

    if _TODAY_RE.match(low):
        return _plan("me.today", {}, "мой день")

    # --- напоминания
    if _REMIND_LIST_RE.search(low):
        return _plan("reminder.list", {}, "напоминания")
    match = _REMIND_CANCEL_RE.match(low)
    if match:
        return _plan("reminder.cancel", {"text": match.group("q").strip()}, "напоминание: отменить")
    match = _SNOOZE_RE.match(low)
    if match and personal is not None and personal.reminders(("fired",), 1):
        unit = match.group("u") or "мин"
        hours = unit.startswith("час")
        count = (when.number(match.group("n")) if match.group("n") else None) or (1 if hours else 10)
        minutes = int(count * (60 if hours else 1))
        return _plan("reminder.snooze", {"minutes": minutes}, "напоминание: отложить")
    if _REMIND_RE.search(low):
        parsed = when.parse(text, now)
        if parsed:
            if parsed["past"]:
                return {"clarify": f"Это время уже прошло ({parsed['label']}). Когда напомнить?",
                        "awaiting": {"kind": "remind_when", "text": parsed["text"]}, "why": "напоминание: время прошло",
                        "suggestions": ["Через час", "Вечером", "Завтра утром"]}
            if len(parsed["text"]) < 2:
                return {"clarify": f"О чём напомнить {parsed['label']}?",
                        "awaiting": {"kind": "remind_text", "iso": parsed["iso"], "repeat": parsed["repeat"]},
                        "why": "напоминание: о чём"}
            return _plan("reminder.add", {"text": parsed["text"], "due": parsed["iso"], "repeat": parsed["repeat"]},
                         "напоминание")
        rest = when._TRIGGER_RE.sub(" ", text, count=1)
        rest = " ".join(rest.split()).strip(" ,.:;!?—–-")
        if not rest or _ABOUT_RE.match(rest):
            return None  # «напомни про Иванова» — это вопрос к памяти или панели, а не будильник
        rest = re.sub(r"^(?:мне|нам|что|чтобы)\s+", "", rest, flags=re.IGNORECASE)
        return {"clarify": f"Когда напомнить «{rest}»? Например: через час, вечером, завтра в 10.",
                "awaiting": {"kind": "remind_when", "text": rest}, "why": "напоминание: когда",
                "suggestions": ["Через час", "Вечером", "Завтра утром"]}

    # --- списки
    for pattern in _LIST_ADD_RES:
        match = pattern.match(text)
        if match:
            items = split_items(match.group("items"))
            if items:
                return _plan("list.add", {"list": list_name(match.group("list")), "items": ", ".join(items)},
                             "список: добавить")
    match = _BUY_RE.match(text)
    if match:
        items = split_items(match.group("items"))
        if items:
            return _plan("list.add", {"list": "покупки", "items": ", ".join(items)}, "покупки: добавить")
    match = _LIST_CLEAR_RE.match(low)
    if match:
        return _plan("list.clear", {"list": list_name(match.group("list"))}, "список: очистить")
    if _LISTS_ALL_RE.match(low):
        return _plan("list.show", {}, "списки")
    for pattern in _LIST_SHOW_RES:
        match = pattern.search(low)
        if match:
            return _plan("list.show", {"list": list_name(match.group("list"))}, "список")
    if _LIST_BUY_SHOW_RE.match(low):
        return _plan("list.show", {"list": "покупки"}, "покупки")
    match = _LIST_REMOVE_RE.match(text)
    if match and personal is not None and not re.match(r"^(?:напоминани|цель|привычк)", match.group("item"), re.I):
        name = list_name(match.group("list")) if match.group("list") else ""
        items = split_items(match.group("item"))
        if items and (name or all(_has_item(personal, item) for item in items)):
            return _plan("list.remove", {"list": name, "item": ", ".join(items)}, "список: вычеркнуть")

    # --- расходы
    match = _EXPENSE_RE.match(text)
    if match:
        rest = match.group("rest")
        category = ""
        cat = _CATEGORY_RE.search(rest)
        if cat:
            category, rest = cat.group("cat").strip(), rest[:cat.start()]
        value, span = amount_of(rest)
        if value and span:
            note = " ".join((rest[:span[0]] + " " + rest[span[1]:]).split())
            note = re.sub(r"^(?:на|за)\s+|\s+(?:на|за)$", "", note, flags=re.IGNORECASE).strip(" ,.")
            day = today - dt.timedelta(days=1) if (match.group("day") or "").strip().casefold() == "вчера" else None
            return _plan("expense.add", {"amount": value, "note": note, "category": category,
                                         "day": day.isoformat() if day else ""}, "расход")
    if _EXPENSE_REPORT_RE.search(low):
        category = ""
        for about in re.finditer(r"\bна\s+(?P<w>[а-яa-z]+)", low):
            word = about.group("w")
            if re.match(r"(?:этой|прошлой|неделю|неделе|месяц|год|сегодня|вчера)", word):
                continue
            for name, words in CATEGORIES:
                if word.startswith(name[:5]) or any(stem in word for stem in words):
                    category = name
                    break
            if category:
                break
        return _plan("expense.report", {"period": low, "category": category}, "расходы: итог")

    # --- цели
    match = _GOAL_SET_RE.match(low)
    if match and when.number(match.group("n")) is not None and not re.fullmatch(r"цел\w*", match.group("goal")):
        return _plan("goal.progress", {"goal": re.sub(r"^цел\w*\s+", "", match.group("goal")).strip(),
                                       "amount": when.number(match.group("n")), "absolute": True}, "цель: прогресс")
    if _GOAL_LIST_RE.search(low):
        return _plan("goal.list", {}, "цели")
    match = _GOAL_REMOVE_RE.match(text)
    if match:
        return _plan("goal.remove", {"goal": match.group("goal").strip()}, "цель: удалить")
    for pattern in _GOAL_ADD_RES:
        match = pattern.match(text)
        if match and len(match.group("body").strip()) >= 3:
            goal = parse_goal(match.group("body"), today)
            return _plan("goal.add", {"title": goal["title"], "target": goal["target"] or None, "unit": goal["unit"],
                                      "deadline": goal["deadline"]}, "цель: новая")
    match = _GOAL_ADD_PROGRESS_RE.match(low) or _GOAL_PLUS_RE.match(low)
    if match and when.number(match.group("n")) is not None:
        return _plan("goal.progress", {"goal": (match.group("goal") or "").strip(),
                                       "amount": when.number(match.group("n"))}, "цель: прогресс")
    match = _DID_RE.match(low)
    if match and personal is not None:
        goal = personal.goal_by_unit(match.group("unit"))
        amount = when.number(match.group("n"))
        if goal and amount is not None:
            return _plan("goal.progress", {"goal": goal["title"], "amount": amount}, "цель: прогресс")

    # --- привычки
    if _HABIT_LIST_RE.search(low):
        return _plan("habit.list", {}, "привычки")
    match = _HABIT_REMOVE_RE.match(text)
    if match:
        return _plan("habit.remove", {"habit": match.group("habit").strip()}, "привычка: удалить")
    for pattern in _HABIT_ADD_RES:
        match = pattern.match(text)
        if match:
            body = match.group("body").strip(" .!")
            # Час привычки — буквальный: «зарядка в 8» — это 08:00, даже если сказано в полдень.
            parsed = when.parse(body, now.replace(hour=0, minute=0, second=0))
            title = parsed["text"] if parsed and parsed["text"] else body
            remind = parsed["at"].strftime("%H:%M") if parsed and re.search(r"\d|утр|вечер|обед|ночь", body) else ""
            return _plan("habit.add", {"title": title, "remind_at": remind}, "привычка: новая")
    if personal is not None:
        match = _HABIT_MARK_RE.match(text) or _HABIT_DONE_RE.match(text)
        candidates = [match.group("what")] if match else []
        past = _PAST_RE.match(text)
        if past:
            candidates += [past.group("what"), f"{past.group('verb')} {past.group('what')}"]
        for what in candidates:
            habit = personal.find_habit(what)
            if habit:
                return _plan("habit.check", {"habit": habit["title"]}, "привычка: отметка")
        if past:
            reminder = personal.find_reminder(past.group("what"))
            if reminder and _close(past.group("what"), reminder["text"]):
                return _plan("reminder.done", {"id": reminder["id"]}, "напоминание: сделано")
    match = _BOUGHT_RE.match(text)
    if match and personal is not None:
        items = split_items(match.group("item"))
        if items and all(_has_item(personal, item, "покупки") for item in items):
            return _plan("list.remove", {"list": "покупки", "item": ", ".join(items)}, "покупки: куплено")

    # --- дневник
    match = _DIARY_ADD_RE.match(text)
    if match:
        body = match.group("text").strip()
        return _plan("diary.add", {"text": body, "mood": personal.mood_of(body) if personal is not None else 0},
                     "дневник")
    if _MOOD_READ_RE.search(low):
        return _plan("diary.read", {"day": "неделя"}, "настроение")
    match = _MOOD_RE.match(text)
    if match and personal is not None:
        mood = personal.mood_of("настроение " + match.group("m"))
        if mood:
            return _plan("diary.add", {"text": f"Настроение: {match.group('m').strip()}", "mood": mood}, "настроение")
    match = _DIARY_READ_RE.match(low)
    if match:
        return _plan("diary.read", {"day": match.group("when").strip(" ?")}, "дневник")
    return None


def _has_item(personal: Any, item: str, name: str = "") -> bool:
    from .personal import _best
    rows = (personal.list_items(name) if name else
            personal.store._rows("SELECT id, item FROM lists WHERE done=0"))
    return _best(rows, item, "item", floor=0.6) is not None


def _close(said: str, text: str) -> bool:
    from .personal import _overlap
    return _overlap(said, text) >= 0.5 or _overlap(text, said) >= 0.5


# ---------------------------------------------------------------------------
# Мелкие расчёты: даты, единицы, случайность
# ---------------------------------------------------------------------------

_UNITS: dict[str, tuple[str, float]] = {
    "мм": ("len", 0.001), "см": ("len", 0.01), "м": ("len", 1.0), "км": ("len", 1000.0),
    "дюйм": ("len", 0.0254), "фут": ("len", 0.3048), "ярд": ("len", 0.9144), "миля": ("len", 1609.344),
    "мг": ("mass", 1e-6), "г": ("mass", 0.001), "кг": ("mass", 1.0), "т": ("mass", 1000.0),
    "фунт": ("mass", 0.45359237), "унция": ("mass", 0.028349523125),
    "мл": ("vol", 0.001), "л": ("vol", 1.0), "галлон": ("vol", 3.785411784), "пинта": ("vol", 0.473176473),
    "сек": ("time", 1.0), "мин": ("time", 60.0), "час": ("time", 3600.0), "сут": ("time", 86400.0),
    "неделя": ("time", 604800.0),
    "б": ("data", 1.0), "кб": ("data", 1024.0), "мб": ("data", 1024.0 ** 2), "гб": ("data", 1024.0 ** 3),
    "тб": ("data", 1024.0 ** 4),
    "км/ч": ("speed", 1 / 3.6), "м/с": ("speed", 1.0), "миль/ч": ("speed", 0.44704),
}
_UNIT_WORDS = (
    ("миллиметр", "мм"), ("сантиметр", "см"), ("километр", "км"), ("метр", "м"), ("дюйм", "дюйм"), ("фут", "фут"),
    ("ярд", "ярд"), ("миль", "миля"), ("мил", "миля"), ("миллиграмм", "мг"), ("килограмм", "кг"), ("кило", "кг"),
    ("грамм", "г"), ("тонн", "т"), ("фунт", "фунт"), ("унци", "унция"), ("миллилитр", "мл"), ("литр", "л"),
    ("галлон", "галлон"), ("пинт", "пинта"), ("секунд", "сек"), ("минут", "мин"), ("час", "час"), ("сут", "сут"),
    ("дн", "сут"), ("день", "сут"), ("недел", "неделя"), ("килобайт", "кб"), ("мегабайт", "мб"), ("гигабайт", "гб"),
    ("терабайт", "тб"), ("байт", "б"), ("цельси", "c"), ("фаренгейт", "f"), ("кельвин", "k"),
)
_CONVERT_RE = re.compile(r"^(?:сколько\s+(?:будет\s+)?|переведи\s+|конвертируй\s+|посчитай\s+)?(?P<n>-?\d+(?:[.,]\d+)?)\s*"
                         r"(?P<a>°?[a-zа-я]+(?:\s*/\s*[a-zа-я]+)?)\.?\s+(?:в|во)\s+(?P<b>°?[a-zа-я]+(?:\s*/\s*[a-zа-я]+)?)"
                         r"\.?\??$", re.IGNORECASE)


def _unit(word: str) -> str:
    low = word.casefold().replace("ё", "е").replace(" ", "").strip(".°")
    if low in ("c", "с") and word.startswith("°") or low in ("градус", "градусов", "градуса"):
        return "c"
    if low in _UNITS or low in ("c", "f", "k"):
        return low
    aliases = {"кмч": "км/ч", "км/час": "км/ч", "мс": "м/с", "миль/час": "миль/ч", "mph": "миль/ч", "kg": "кг",
               "km": "км", "cm": "см", "mm": "мм", "m": "м", "gb": "гб", "mb": "мб", "kb": "кб", "tb": "тб",
               "lb": "фунт", "oz": "унция", "in": "дюйм", "ft": "фут", "mi": "миля", "l": "л", "ml": "мл"}
    if low in aliases:
        return aliases[low]
    for prefix, unit in _UNIT_WORDS:
        if low.startswith(prefix):
            return unit
    return ""


def _convert(value: float, source: str, target: str) -> float | None:
    temps = ("c", "f", "k")
    if source in temps and target in temps:
        celsius = value if source == "c" else ((value - 32) * 5 / 9 if source == "f" else value - 273.15)
        return celsius if target == "c" else (celsius * 9 / 5 + 32 if target == "f" else celsius + 273.15)
    if source not in _UNITS or target not in _UNITS or _UNITS[source][0] != _UNITS[target][0]:
        return None
    return value * _UNITS[source][1] / _UNITS[target][1]


def _fmt(value: float) -> str:
    if abs(value) >= 100 or abs(value - round(value)) < 1e-9:
        text = f"{value:,.0f}" if abs(value - round(value)) < 0.05 or abs(value) >= 1000 else f"{value:,.1f}"
    elif abs(value) >= 1:
        text = f"{value:,.2f}".rstrip("0").rstrip(".")
    else:
        text = f"{value:.4g}"
    return text.replace(",", " ").replace(".", ",")


_UNIT_TITLES = {"c": "°C", "f": "°F", "k": "K", "б": "Б", "кб": "КБ", "мб": "МБ", "гб": "ГБ", "тб": "ТБ",
                "сек": "с", "час": "ч"}
_UNIT_FORMS = {"миля": ("миля", "мили", "миль"), "дюйм": ("дюйм", "дюйма", "дюймов"), "фут": ("фут", "фута", "футов"),
               "ярд": ("ярд", "ярда", "ярдов"), "фунт": ("фунт", "фунта", "фунтов"),
               "унция": ("унция", "унции", "унций"), "галлон": ("галлон", "галлона", "галлонов"),
               "пинта": ("пинта", "пинты", "пинт"), "неделя": ("неделя", "недели", "недель")}


def _unit_title(unit: str, value: float) -> str:
    forms = _UNIT_FORMS.get(unit)
    if not forms:
        return _UNIT_TITLES.get(unit, unit)
    if abs(value - round(value)) > 1e-9 or abs(value) >= 1e6:
        return forms[1]  # «3,11 мили», «45,5 фунта»
    return when.plural(int(round(value)), *forms)


def util_answer(phrase: str, now: dt.datetime) -> tuple[str, str] | None:
    """(ответ, что это было) для счёта дат, единиц и случайного выбора. None — не это."""
    low = phrase.casefold().replace("ё", "е").strip(" ?!.")
    today = now.date()
    match = re.search(r"(?:сколько\s+)?(?:дней|дня|суток)\s+(?:осталось\s+|остается\s+)?до\s+(?P<what>.+)$", low) \
        or re.search(r"^сколько\s+(?:осталось\s+)?до\s+(?P<what>.+)$", low)
    if match:
        found = when.target_date("до " + match.group("what"), today)
        if found:
            date = found[0]
            days = (date - today).days
            if days < 0:
                return f"{when.day_label(date, today).capitalize()} уже прошло — {-days} {when.plural(-days, 'день', 'дня', 'дней')} назад.", "даты"
            weeks, rest = divmod(days, 7)
            tail = (f" ({weeks} {when.plural(weeks, 'неделя', 'недели', 'недель')}"
                    + (f" и {rest} {when.plural(rest, 'день', 'дня', 'дней')}" if rest else "") + ")") if weeks else ""
            what = when.day_label(date, today)
            return f"До {what} — {days} {when.plural(days, 'день', 'дня', 'дней')}{tail}.", "даты"
    match = re.search(r"сколько\s+(?:дней|времени)\s+(?:прошло\s+)?(?:с|со)\s+(?P<what>.+)$", low)
    if match:
        date = when.any_date(match.group("what"), today)
        if date and date > today:
            date = date.replace(year=date.year - 1)
        if date:
            days = (today - date).days
            return f"С {when.day_label(date, today)} прошло {days} {when.plural(days, 'день', 'дня', 'дней')}.", "даты"
    match = re.search(r"(?:какой|какая)\s+день\s+недели\s+(?:будет\s+|был\s+|было\s+)?(?P<what>.+)$", low)
    if match and not re.fullmatch(r"(?:сегодня|сейчас|завтра)", match.group("what")):
        date = when.any_date(match.group("what"), today)
        if date:
            return f"{when.day_label(date, today).capitalize()} — {when.WEEKDAYS[date.weekday()]}.", "даты"
    match = re.search(r"какое\s+(?:будет\s+)?(?:число|дата)\s+(?:будет\s+)?через\s+(?P<n>" + when.NUM
                      + r")\s+(?P<u>дн\w*|день|недел\w*|месяц\w*)", low)
    if match:
        count = when.number(match.group("n")) or 0
        unit = match.group("u")
        days = count * (7 if unit.startswith("недел") else (30 if unit.startswith("месяц") else 1))
        date = today + dt.timedelta(days=round(days))
        return f"Через {when.number(match.group('n')):g} {unit} будет {date.day} {when.MONTHS[date.month - 1]} {date.year}, {when.WEEKDAYS[date.weekday()]}.", "даты"
    match = _CONVERT_RE.match(low)
    if match:
        source, target = _unit(match.group("a")), _unit(match.group("b"))
        value = float(match.group("n").replace(",", "."))
        result = _convert(value, source, target) if source and target else None
        if result is not None:
            return (f"{_fmt(value)} {_unit_title(source, value)} = {_fmt(result)} "
                    f"{_unit_title(target, round(result, 2))}.", "единицы")
    if re.fullmatch(r"(?:подбрось|брось|кинь|подкинь)\s+монет\w*|орел\s+или\s+решка", low):
        return ("Орёл." if _RANDOM.random() < 0.5 else "Решка."), "монетка"
    if re.fullmatch(r"(?:брось|кинь|подбрось)\s+(?:кубик|кости|кость)", low):
        return f"Выпало {_RANDOM.randint(1, 6)}.", "кубик"
    match = re.search(r"(?:случайн\w*|рандомн\w*)\s+число(?:\s+от\s+(?P<a>-?\d+))?(?:\s+до\s+(?P<b>-?\d+))?", low)
    if match:
        low_value = int(match.group("a") or 1)
        high_value = int(match.group("b") or (100 if not match.group("a") else low_value + 99))
        if low_value > high_value:
            low_value, high_value = high_value, low_value
        return f"Случайное число от {low_value} до {high_value}: {_RANDOM.randint(low_value, high_value)}.", "случайность"
    if re.fullmatch(r"(?:скажи\s+)?да\s+или\s+нет", low):
        return ("Да." if _RANDOM.random() < 0.5 else "Нет."), "случайность"
    match = re.match(r"^(?:выбери|помоги\s+выбрать|что\s+выбрать)\s*[:,]?\s*(?P<opts>.+)$", phrase.strip(" ?!."),
                     re.IGNORECASE)
    if match and re.search(r"\s+или\s+|,", match.group("opts")):
        options = [part.strip(" .,?!") for part in re.split(r"\s*,\s*(?:или\s+)?|\s+или\s+", match.group("opts"))
                   if part.strip(" .,?!")]
        if len(options) >= 2:
            return f"Выбираю: {_RANDOM.choice(options)}.", "случайность"
    return None
