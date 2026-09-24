"""Реестр навыков личного ассистента (18.21): навык — это данные, а не ветка кода.

Зачем реестр, если можно написать `if name == "files.search"`.

Ассистент компьютера растёт навыками: сегодня их сто четыре (список и числа
сверяет с `docs/НАВЫКИ.md` контракт `test_assistant_skills.py`). Если каждый навык добавлять
веткой в диспетчер, то к тридцатому навыку подтверждение, журнал и проверка
параметров разъедутся по трём местам — ровно так, как разошлись `api.py` и
`routes_*.py`. Поэтому здесь одно описание навыка, из которого берутся:

  * **доступность** — навык без нужной способности не предлагается модели вовсе
    и честно говорит причину (`agent.why`, идея И178);
  * **подтверждение** — признак берётся из реестра по риску, а не из запроса:
    вызывающая сторона не может его понизить (тот же приём, что в каталоге
    действий панели, `assistant.ACTIONS`);
  * **параметры** — имена и типы объявлены, мусор от модели не исполняется;
  * **журнал** — каждое исполнение пишется в свою базу (идея И138).

PrintFlow здесь — один из навыков (`panel.actions`, `panel.do`, `panel.ask`),
а не хозяин ассистента: агент ходит в панель по loopback и не знает её базы
(ADR-0004). Реестр не импортирует коннектор, а коннектор не импортирует агент.
"""
from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# Словари реестра
# ---------------------------------------------------------------------------

# Типы параметров. Их мало сознательно: чем меньше типов, тем меньше способов
# прислать в навык значение, которое он прочитает не так, как задумано.
PARAM_TYPES = ("text", "int", "float", "bool", "date", "path", "object", "oneof")

# Риск решает, нужно ли подтверждение человека.
#   read         — ничего не меняет;
#   own          — пишет только в свою базу ассистента (индекс, журнал, заметки, память);
#   soft         — мягкое действие удобства (18.21): открыть программу из белого
#                  списка, звук громче или тише, пауза музыки, свернуть или
#                  переключить окно. Обратимо одной фразой, данных не трогает, в
#                  чужое окно ничего не вводит — поэтому без окна «Подтвердить»;
#   write        — меняет чужое: файлы на диске, панель, ввод в чужое окно;
#   system       — меняет состояние компьютера (питание, микрофон, автозагрузка);
#   irreversible — откатить нельзя (удаление, стирание архива).
# Порядок — от мягкого к опасному: у выученного навыка риск — максимальный из шагов.
RISKS = ("read", "own", "soft", "write", "system", "irreversible")
CONFIRM_RISKS = ("write", "system", "irreversible")

# Хост: кто исполняет навык. `agent` — эта программа, `panel` — PrintFlow
# через loopback. Хост нужен, чтобы панель и ассистент говорили об одном навыке
# одинаково и не дублировали исполнение.
HOSTS = ("agent", "panel")

# Способности, которые навык может требовать. Имя способности совпадает с ключом
# `capabilities.detect()`, а причина отсутствия берётся оттуда же: навык не
# придумывает объяснение, он показывает то, что агент знает о себе.
CAPABILITIES = (
    "files",       # файловая система этого компьютера
    "sqlite",      # своя база ассистента
    "panel",       # PrintFlow отвечает на loopback
    "model",       # локальный рантайм модели отвечает
    "windows",     # управление окнами (только Windows)
    "uia",         # чтение элементов окна через UI Automation
    "ocr",         # распознавание текста на снимке
    "screen",      # снимок экрана
    "camera",      # вебкамера
    "speech_in",   # распознавание речи
    "speech_out",  # озвучивание ответа
    "printer",     # печать на обычном принтере
    "tray",        # иконка в трее
    "network",     # сеть для Авито/ТГ (18.15)
    "avito",       # Авито-слежка
    "tg",          # ТГ-посты
    "system",      # системные действия (18.17)
    "autostart",   # автозагрузка
    "process_list",# список процессов
    "audio_device",# аудиоустройства
    "clipboard",   # буфер обмена
    "region_shot", # скрин области
    "focus_timer", # таймер фокуса
    "file_watch",  # слежка за папками
    "preferences", # предпочтения владельца
    "whitelist",   # белый список приложений
    "macro",       # макросы
    "quick_open",  # быстрый поиск файлов
)

# Предел на объявленные шаги выученного навыка: сценарий из восьмидесяти шагов —
# это уже программа, а не навык, и подтверждать его человек не сможет осмысленно.
MAX_LEARNED_STEPS = 8
MAX_LEARNED_NAME = 48
MAX_TEXT_PARAM = 2000
MAX_OBJECT_PARAM = 32

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,40}\.[a-z][a-z0-9_]{0,40}$")
_LEARNED_RE = re.compile(r"^my\.[a-z][a-z0-9_]{1,40}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# ---------------------------------------------------------------------------
# Реестр: сто четыре живых навыка из каталога `docs/НАВЫКИ.md`
# ---------------------------------------------------------------------------
#
# Объявлены только те навыки, которые исполняются. Навык «в планах» живёт в
# справке, а не в реестре: кнопка, которая молча ничего не делает, — худшее, что
# может появиться в интерфейсе ассистента.
SKILLS: dict[str, dict[str, Any]] = {
    # --- PrintFlow как подсистема (И137, И1) -------------------------------
    "panel.actions": {
        "title": "Что умеет панель",
        "description": "Каталог действий PrintFlow: чтение и действия с подтверждением.",
        "host": "panel", "risk": "read", "params": {},
        "requires": ("panel",), "ideas": ("И137",),
        "doc": "Двадцать действий панели: двенадцать чтений и восемь с подтверждением.",
    },
    "panel.do": {
        "title": "Действие в панели",
        "description": "Выполнить действие PrintFlow из каталога: чтение сразу, деньги и печать через подтверждение.",
        "host": "panel", "risk": "write", "params": {"action": "text", "params": "object"},
        "requires": ("panel",), "ideas": ("И137",),
        "doc": "Адрес и метод берёт панель из своего каталога, а не из запроса.",
    },
    "panel.ask": {
        "title": "Вопрос по фактам базы",
        "description": "Ответ по заказам, клиентам, деньгам и парку со ссылками на записи базы.",
        "host": "panel", "risk": "read", "params": {"question": "text"},
        "requires": ("panel",), "ideas": ("И1",),
        "doc": "Числа в ответе сверяются с найденными фактами: расхождение видно как предупреждение.",
    },
    # --- файлы и знания (И142, И143, И144, И147, И148) ---------------------
    "files.index": {
        "title": "Индекс документов",
        "description": "Прочитать папки и построить индекс текстов: свои документы и знания цеха.",
        "host": "agent", "risk": "own", "params": {"folders": "text"},
        "requires": ("files", "sqlite"), "ideas": ("И142", "И148"),
        "doc": "Повторно читаются только изменённые файлы: сверяются размер, время и хеш.",
    },
    "files.search": {
        "title": "Поиск по своим файлам",
        "description": "Найти место в документах по словам: договор, прайс, переписка, профиль печати.",
        "host": "agent", "risk": "read", "params": {"query": "text", "limit": "int"},
        "requires": ("sqlite",), "ideas": ("И142", "И143"),
        "doc": "Ответ — пути и отрывки, а не пересказ: источник виден сразу.",
    },
    "files.recent": {
        "title": "Что я открывал недавно",
        "description": "Файлы, которые индекс видел последними: по времени изменения и по журналу.",
        "host": "agent", "risk": "read", "params": {"limit": "int"},
        "requires": ("sqlite",), "ideas": ("И143",),
        "doc": "Журнал доступа ассистент ведёт сам: за системой он не подсматривает.",
    },
    "files.ask": {
        "title": "Вопрос по своим документам",
        "description": "Ответ по содержимому файлов с цитатой и путём; без модели — найденные места.",
        "host": "agent", "risk": "read", "params": {"question": "text"},
        "requires": ("sqlite",), "ideas": ("И142",),
        "doc": "Модель не обязательна: без неё навык честно отдаёт отрывки и причину.",
    },
    "files.facts": {
        "title": "Факты из документа",
        "description": "Вытащить суммы, даты, телефоны и позиции из файла и показать, где они написаны.",
        "host": "agent", "risk": "own",
        "params": {"path": "path", "save": "bool"},
        "requires": ("files", "sqlite"), "ideas": ("И147",),
        "doc": "Разбор детерминированный: каждый факт хранит номер строки источника.",
    },
    "files.to_order": {
        "title": "Файл в заказ",
        "description": "Отдать файл в PrintFlow: модель, счёт, фото или документ — и получить черновик заказа.",
        "host": "agent", "risk": "read",
        "params": {"path": "path", "order": "text", "kind": "oneof:model|invoice|photo|document"},
        "requires": ("files", "panel"), "ideas": ("И144",),
        "doc": "Путь обязан лежать внутри разрешённых папок. Навык ничего не сохраняет: заказ сохраняет человек в панели.",
    },
    "files.tidy_plan": {
        "title": "План раскладки загрузок",
        "description": "Показать, какие файлы куда переедут и почему. Ничего не двигает.",
        "host": "agent", "risk": "read", "params": {"folder": "path"},
        "requires": ("files",), "ideas": ("И144",),
        "doc": "Человек читает план до перемещения: неожиданная раскладка видна заранее.",
    },
    "files.tidy_apply": {
        "title": "Разложить файлы по плану",
        "description": "Переместить файлы папки по делам: модели, счета, фото, документы, архивы.",
        "host": "agent", "risk": "write", "params": {"folder": "path"},
        "requires": ("files",), "ideas": ("И144",),
        "doc": "Только перемещения внутри той же папки и только после подтверждения: удаления нет.",
    },
    "knowledge.shop": {
        "title": "Знания цеха",
        "description": "Ответ по инструкциям, профилям печати и чек-листам из папки знаний.",
        "host": "agent", "risk": "read", "params": {"question": "text"},
        "requires": ("sqlite",), "ideas": ("И148",),
        "doc": "Папка знаний отделена от личных документов: поиск идёт только по ней.",
    },
    # --- день владельца (И174) ---------------------------------------------
    "day.briefing": {
        "title": "Утренний брифинг",
        "description": "План дня, тревоги, горящие сроки и долги одним текстом.",
        "host": "panel", "risk": "read", "params": {},
        "requires": ("panel",), "ideas": ("И174",),
        "doc": "Числа считаются в панели детерминированно: модель для брифинга не нужна.",
    },
    "day.summary": {
        "title": "Итог дня",
        "description": "Что заработано, что напечатано, что встало и какие долги остались.",
        "host": "panel", "risk": "read", "params": {"days": "int"},
        "requires": ("panel",), "ideas": ("И174",),
        "doc": "Тот же источник, что у панелей финансов и производства: вторая правда не появляется.",
    },
    # --- мета: честность, журнал, обучение (И178, И180) --------------------
    "agent.skills": {
        "title": "Что я умею",
        "description": "Список навыков с параметрами, риском и доступностью на этом компьютере.",
        "host": "agent", "risk": "read", "params": {},
        "requires": (), "ideas": ("И178", "И180"),
        "doc": "Недоступный навык показан с причиной, а не спрятан из списка.",
    },
    "agent.why": {
        "title": "Почему не получилось",
        "description": "Причина отказа навыка: нет способности, нет подтверждения, параметр не разобран.",
        "host": "agent", "risk": "read", "params": {"skill": "text"},
        "requires": ("sqlite",), "ideas": ("И178",),
        "doc": "Журнал хранит исход каждого вызова, поэтому причина берётся из факта, а не из догадки.",
    },
    "agent.journal": {
        "title": "Журнал действий на ПК",
        "description": "Что ассистент делал на этом компьютере: навык, параметры, окно, исход.",
        "host": "agent", "risk": "read", "params": {"limit": "int"},
        "requires": ("sqlite",), "ideas": ("И178",),
        "doc": "Журнал не удаляется навыком: стереть его можно только руками в файле базы.",
    },
    "agent.learn": {
        "title": "Новый навык словами",
        "description": "Собрать навык из шагов существующих: имя, описание, шаги — и сохранить в реестр.",
        "host": "agent", "risk": "write", "params": {"skill": "object"},
        "requires": ("sqlite",), "ideas": ("И180",),
        "doc": "Выученный навык всегда требует подтверждения: он исполняет чужие шаги подряд.",
    },
    # --- Авито: слежка и ответы (И181, И183, И185, И186) --------------------
    "avito.watch": {
        "title": "Слежка за Авито",
        "description": "Добавить запрос в слежку: что искать, город, цена — ассистент запомнит и будет проверять.",
        "host": "agent", "risk": "own",
        "params": {"query": "text", "city": "text", "category": "text",
                   "max_price": "int", "min_price": "int", "enabled": "bool"},
        "requires": ("sqlite", "avito"), "ideas": ("И181",),
        "doc": "Запрос сохраняется в свою базу ассистента, а не в панель: слежка — личное дело владельца.",
    },
    "avito.watches": {
        "title": "Мои слежки на Авито",
        "description": "Список запросов, за которыми следит ассистент: что, где, сколько нашёл.",
        "host": "agent", "risk": "read", "params": {"limit": "int"},
        "requires": ("sqlite",), "ideas": ("И181",),
        "doc": "Показывает все слежки и когда их проверяли в последний раз.",
    },
    "avito.search": {
        "title": "Поиск на Авито",
        "description": "Разовый поиск на Авито по запросу с фильтрами: город, цена, категория.",
        "host": "agent", "risk": "read",
        "params": {"query": "text", "city": "text", "category": "text",
                   "max_price": "int", "min_price": "int", "limit": "int"},
        "requires": ("network", "avito"), "ideas": ("И186",),
        "doc": "Скачивает страницу поиска и разбирает объявления без внешних библиотек.",
    },
    "avito.check": {
        "title": "Проверить Авито-слежки",
        "description": "Проверить все активные слежки на Авито и показать новые объявления.",
        "host": "agent", "risk": "own",
        "params": {"watch_id": "int", "only_new": "bool"},
        "requires": ("network", "sqlite", "avito"), "ideas": ("И185",),
        "doc": "Сравнивает с тем, что уже видел, и помечает новые. Без модели — только список.",
    },
    "avito.reply": {
        "title": "Варианты ответа на Авито",
        "description": "Придумать 3 варианта ответа покупателю на Авито: вежливый, короткий, с торгом.",
        "host": "agent", "risk": "read",
        "params": {"thread": "text", "intent": "text", "city": "text"},
        "requires": ("sqlite",), "ideas": ("И183",),
        "doc": "С моделью — живые варианты, без модели — шаблоны с тонами. Числа не выдумывает.",
    },
    "avito.listings": {
        "title": "Найденные объявления",
        "description": "Показать объявления, которые нашёл ассистент по слежкам.",
        "host": "agent", "risk": "read",
        "params": {"watch_id": "int", "limit": "int", "only_new": "bool"},
        "requires": ("sqlite",), "ideas": ("И181", "И185"),
        "doc": "Чтение из своей базы: что нашлось, когда и по какому запросу.",
    },
    # --- ТГ: посты и идеи (И182, И184) --------------------------------------
    "tg.draft": {
        "title": "Черновик поста в ТГ",
        "description": "Придумать пост в ТГ-канал цеха: тема, тон, факты — и сохранить в черновики.",
        "host": "agent", "risk": "own",
        "params": {"topic": "text", "tone": "oneof:деловой|дружелюбный|техничный|продающий|короткий|история",
                   "facts": "text", "source": "text"},
        "requires": ("sqlite", "tg"), "ideas": ("И182",),
        "doc": "С моделью — живой текст, без — шаблон. Факты не выдумываются.",
    },
    "tg.drafts": {
        "title": "Черновики ТГ-постов",
        "description": "Список черновиков постов в ТГ: тема, тон, статус.",
        "host": "agent", "risk": "read", "params": {"limit": "int", "status": "text"},
        "requires": ("sqlite",), "ideas": ("И182",),
        "doc": "Черновики лежат в своей базе ассистента: их можно показать, поправить, опубликовать руками.",
    },
    "tg.ideas": {
        "title": "Идеи постов в ТГ",
        "description": "Придумать темы для ТГ-канала цеха из того, что печатали и что спрашивают клиенты.",
        "host": "agent", "risk": "read",
        "params": {"context": "text", "limit": "int"},
        "requires": ("sqlite",), "ideas": ("И184",),
        "doc": "С моделью — живые идеи под контекст цеха, без — заготовки.",
    },
    "tg.post": {
        "title": "Опубликовать пост в ТГ",
        "description": "Отправить черновик поста в ТГ-канал через Bot API. Требует подтверждения.",
        "host": "agent", "risk": "write",
        "params": {"draft_id": "int", "text": "text", "chat": "text"},
        "requires": ("network", "sqlite", "tg"), "ideas": ("И188",),
        "doc": "Берёт текст из черновика или из параметра, отправляет в чат. Токен из настроек агента.",
    },
    # --- 18.16: архив переписок, связка, расписание, дедуп (И191, И193, И195-И197) ----
    "avito.threads": {
        "title": "Архив переписок Авито",
        "description": "Список сохранённых переписок с Авито: текст, варианты ответа, статус.",
        "host": "agent", "risk": "read",
        "params": {"limit": "int", "status": "text"},
        "requires": ("sqlite",), "ideas": ("И191",),
        "doc": "Переписки сохраняются локально, только текст для копирования, без авто-отправки.",
    },
    "avito.thread_save": {
        "title": "Сохранить переписку Авито",
        "description": "Сохранить текст переписки и варианты ответа в архив.",
        "host": "agent", "risk": "own",
        "params": {"thread": "text", "intent": "text", "city": "text", "status": "text"},
        "requires": ("sqlite",), "ideas": ("И191",),
        "doc": "Архив — своя база ассистента, без авто-ответа, только для копирования.",
    },
    "avito.to_order": {
        "title": "Объявление в заказ",
        "description": "Создать черновик заказа PrintFlow из объявления Авито.",
        "host": "agent", "risk": "write",
        "params": {"listing_id": "int", "url": "text", "title": "text", "price": "text", "city": "text"},
        "requires": ("panel", "sqlite"), "ideas": ("И193",),
        "doc": "Берёт данные объявления и через панель создаёт черновик заказа. Требует подтверждения.",
    },
    "avito.schedule": {
        "title": "Расписание проверки Авито",
        "description": "Задать интервал автопроверки слежки и уведомление в трее.",
        "host": "agent", "risk": "own",
        "params": {"watch_id": "int", "interval_hours": "int", "notify": "bool"},
        "requires": ("sqlite",), "ideas": ("И195", "И197"),
        "doc": "Интервал в часах, 0 — выкл. Уведомление — флаг для UI/трея.",
    },
    "avito.notify": {
        "title": "Уведомления Авито",
        "description": "Включить/выключить уведомления в трее для слежки.",
        "host": "agent", "risk": "own",
        "params": {"watch_id": "int", "enabled": "bool"},
        "requires": ("sqlite",), "ideas": ("И197",),
        "doc": "Флаг в базе, панель показывает бейдж о новых.",
    },
    "avito.dedup": {
        "title": "Дубли Авито по фото",
        "description": "Найти объявления с одинаковым фото-хешем.",
        "host": "agent", "risk": "read",
        "params": {"limit": "int", "image_hash": "text"},
        "requires": ("sqlite",), "ideas": ("И196",),
        "doc": "Хеш считается из первых 32КБ картинки локально, без ML.",
    },
    # --- 18.16: ТГ календарь, шаблоны, хештеги, поиск, экспорт, статистика (И198, И200-И205) ----
    "tg.schedule": {
        "title": "Запланировать пост ТГ",
        "description": "Добавить черновик в календарь публикаций.",
        "host": "agent", "risk": "own",
        "params": {"draft_id": "int", "planned_at": "text", "chat": "text"},
        "requires": ("sqlite",), "ideas": ("И198",),
        "doc": "Дата YYYY-MM-DD или YYYY-MM-DD HH:MM, статус planned.",
    },
    "tg.schedules": {
        "title": "Календарь ТГ-постов",
        "description": "Список запланированных постов.",
        "host": "agent", "risk": "read",
        "params": {"limit": "int", "status": "text"},
        "requires": ("sqlite",), "ideas": ("И198",),
        "doc": "Читает tg_schedule, показывает план и факты публикации.",
    },
    "tg.template_save": {
        "title": "Сохранить шаблон ТГ",
        "description": "Сохранить шаблон поста с переменными {fact} {topic} и т.д.",
        "host": "agent", "risk": "own",
        "params": {"name": "text", "tone": "text", "template": "text"},
        "requires": ("sqlite",), "ideas": ("И200",),
        "doc": "Переменные в фигурных скобках, тон опционально.",
    },
    "tg.templates": {
        "title": "Шаблоны ТГ-постов",
        "description": "Список шаблонов постов.",
        "host": "agent", "risk": "read",
        "params": {"limit": "int"},
        "requires": ("sqlite",), "ideas": ("И200",),
        "doc": "Шаблоны из своей базы + встроенные.",
    },
    "tg.template_apply": {
        "title": "Пост из шаблона",
        "description": "Применить шаблон к фактам и создать черновик.",
        "host": "agent", "risk": "own",
        "params": {"template_id": "int", "facts": "text", "topic": "text", "tone": "text"},
        "requires": ("sqlite", "tg"), "ideas": ("И200",),
        "doc": "Берёт шаблон, подставляет переменные, сохраняет черновик.",
    },
    "tg.hashtags": {
        "title": "Хештеги для ТГ",
        "description": "Подобрать хештеги к тексту поста локально.",
        "host": "agent", "risk": "read",
        "params": {"text": "text", "limit": "int"},
        "requires": (), "ideas": ("И202",),
        "doc": "Словарь цеха, без внешних API, без модели.",
    },
    "tg.search": {
        "title": "Поиск по черновикам ТГ",
        "description": "Найти черновики по словам в теме и тексте.",
        "host": "agent", "risk": "read",
        "params": {"query": "text", "limit": "int"},
        "requires": ("sqlite",), "ideas": ("И203",),
        "doc": "LIKE по lower(topic) и lower(text), без FTS5 чтобы без зависимостей.",
    },
    "tg.export": {
        "title": "Экспорт черновиков ТГ",
        "description": "Выгрузить черновики в md/json/txt для ручной правки.",
        "host": "agent", "risk": "read",
        "params": {"status": "text", "limit": "int", "format": "oneof:md|json|txt"},
        "requires": ("sqlite",), "ideas": ("И204",),
        "doc": "Возвращает текст, файл сохраняет панель/человек.",
    },
    "tg.stats": {
        "title": "Статистика ТГ",
        "description": "Конверсия идей → черновики → посты, календарь.",
        "host": "agent", "risk": "read",
        "params": {},
        "requires": ("sqlite",), "ideas": ("И205",),
        "doc": "Считает из tg_ideas, tg_drafts, tg_schedule.",
    },
    "tg.idea_save": {
        "title": "Сохранить идею ТГ",
        "description": "Сохранить идею поста в историю для подсчёта конверсии.",
        "host": "agent", "risk": "own",
        "params": {"context": "text", "idea": "text"},
        "requires": ("sqlite",), "ideas": ("И205",),
        "doc": "История идей в своей базе, связывается с черновиком при создании.",
    },
    "tg.ideas_history": {
        "title": "История идей ТГ",
        "description": "Список сохранённых идей с их статусом.",
        "host": "agent", "risk": "read",
        "params": {"limit": "int", "status": "text"},
        "requires": ("sqlite",), "ideas": ("И205",),
        "doc": "Идеи, которые превратились в черновики — used, остальные new.",
    },
    # --- 18.17: система (И206, И208, И209) + голос (И214) + буфер (И211) ---
    "system.autostart": {
        "title": "Автозагрузка",
        "description": "Включить/выключить автозапуск ассистента при входе в Windows.",
        "host": "agent", "risk": "write",
        "params": {"enabled": "bool", "app_name": "text"},
        "requires": ("autostart",), "ideas": ("И206",),
        "doc": "Пишет в HKCU Run, требует подтверждения и явного enabled: вызов без него ничего не меняет.",
    },
    "system.process_list": {
        "title": "Процессы",
        "description": "Список процессов с CPU/RAM: что жрёт ресурсы.",
        "host": "agent", "risk": "read",
        "params": {"limit": "int"},
        "requires": ("process_list",), "ideas": ("И208",),
        "doc": "Windows tasklist, Linux ps — без внешних зависимостей.",
    },
    "system.audio_device": {
        "title": "Аудио-устройства",
        "description": "Список устройств вывода звука и переключение.",
        "host": "agent", "risk": "own",
        "params": {"device_id": "text"},
        "requires": ("audio_device",), "ideas": ("И209",),
        "doc": "Читает реестр MMDevices, переключение через реестр.",
    },
    "system.volume": {
        "title": "Громкость",
        "description": "Узнать и изменить общую громкость 0-100: громче, тише, выключить или включить звук.",
        "host": "agent", "risk": "soft",
        "params": {"level": "int", "delta": "int", "mute": "oneof:on|off|toggle"},
        "requires": ("system",), "ideas": ("И156", "И302"),
        "doc": "Общий звук Windows через Core Audio (IAudioEndpointVolume, ctypes); без него — медиаклавиши. Мягкий риск: обратимо одной фразой.",
    },
    "system.display": {
        "title": "Экран и яркость",
        "description": "Информация о мониторах и яркости.",
        "host": "agent", "risk": "read",
        "params": {},
        "requires": ("system",), "ideas": ("И156",),
        "doc": "Заглушка для живой Windows: возвращает количество мониторов.",
    },
    "system.focus": {
        "title": "Не беспокоить",
        "description": "Включить режим фокуса на N минут: без уведомлений.",
        "host": "agent", "risk": "own",
        "params": {"minutes": "int"},
        "requires": ("system",), "ideas": ("И156",),
        "doc": "Флаг в базе + таймер, требует подтверждения для длительных.",
    },
    "system.power": {
        "title": "Питание",
        "description": "Блокировка, сон, перезагрузка, выключение, отмена и погасить монитор — с подтверждением.",
        "host": "agent", "risk": "system",
        "params": {"action": "oneof:lock|sleep|restart|shutdown|cancel|screen_off"},
        "requires": ("system",), "ideas": ("И156",),
        "doc": "LockWorkStation, SetSuspendState, shutdown /r и /s с минутой на отмену (shutdown /a). Действие называется явно: без него навык отказывает.",
    },
    "system.health": {
        "title": "Здоровье ПК",
        "description": "Процессор, память, диски, время работы и батарея — и что из этого тревожно.",
        "host": "agent", "risk": "read",
        "params": {},
        "requires": (), "ideas": ("И159",),
        "doc": "GetSystemTimes, GlobalMemoryStatusEx, GetDriveType в Windows; /proc в Linux. Без внешних зависимостей.",
    },
    "system.check": {
        "title": "Проверка зависимостей",
        "description": "Что установлено для полноценного ассистента: pip-пакеты, модели, tesseract, ollama.",
        "host": "agent", "risk": "read",
        "params": {},
        "requires": (), "ideas": ("И222",),
        "doc": "Читает capabilities + agent/install.check(), без сети.",
    },
    "system.install": {
        "title": "Автоустановка",
        "description": "Поставить недостающие pip-пакеты и модели речи для ассистента.",
        "host": "agent", "risk": "write",
        "params": {"what": "oneof:pip|models|full|requirements", "confirm_text": "text"},
        "requires": (), "ideas": ("И222",),
        "doc": "Ставит только то, чего нет: pip install + скачивание vosk small ru в ~/.printflow/models. Требует подтверждения и явного what: вызов без него ничего не ставит.",
    },
    "system.watchdog": {
        "title": "Надзор за процессами",
        "description": "Кто жив: пульс агента и панели, причина протухания, сколько раз падало.",
        "host": "agent", "risk": "read",
        "params": {"mode": "oneof:status|log|self_check"},
        "requires": (), "ideas": ("И263",),
        "doc": "Читает ~/.printflow/watchdog.json, watchdog_config.json и watchdog.log. Ничего не поднимает — это делает сам надзиратель.",
    },
    "system.watchdog_once": {
        "title": "Поднять упавших",
        "description": "Один проход надзора прямо сейчас: проверить пульс и поднять упавшие роли.",
        "host": "agent", "risk": "write",
        "params": {"roles": "text", "dry": "oneof:on|off", "confirm_text": "text"},
        "requires": (), "ideas": ("И263",),
        "doc": "Тот же проход, что делает надзиратель по таймеру, но по кнопке владельца. Подтверждение словом «поднять»: подъём процесса — действие, а не чтение.",
    },
    "system.watchdog_arm": {
        "title": "Вооружить надзор",
        "description": "Включить или выключить надзор за процессами и сохранить пороги срабатывания.",
        "host": "agent", "risk": "write",
        "params": {"enabled": "oneof:on|off", "interval": "int", "stale_sec": "int",
                   "max_restarts": "int", "confirm_text": "text"},
        "requires": (), "ideas": ("И263",),
        "doc": "Пишет настройки надзора в ~/.printflow/watchdog_config.json. Подъём процессов делает отдельный лёгкий процесс, а не панель: у надзирателя нет причин падать вместе с подопечным.",
    },
    # --- окна и ввод (Н16-Н26) -------------------------------------------
    "window.active": {
        "title": "Активное окно",
        "description": "Имя и класс активного окна.",
        "host": "agent", "risk": "read",
        "params": {},
        "requires": ("windows",), "ideas": ("И149",),
        "doc": "GetForegroundWindow через ctypes, только Windows.",
    },
    "window.list": {
        "title": "Список окон",
        "description": "Видимые окна с заголовками.",
        "host": "agent", "risk": "read",
        "params": {"limit": "int"},
        "requires": ("windows",), "ideas": ("И149",),
        "doc": "EnumWindows, показывает человеку, а не выбирает сам.",
    },
    "window.focus": {
        "title": "Фокус на окно",
        "description": "Вывести окно на передний план по названию программы или части заголовка.",
        "host": "agent", "risk": "soft",
        "params": {"title": "text"},
        "requires": ("windows",), "ideas": ("И152",),
        "doc": "Ищет по заголовку, имени процесса и синонимам («телеграм» → Telegram.exe), разворачивает свёрнутое и обходит запрет Windows на смену окна. Ввода в окно нет — мягкий риск.",
    },
    "window.text": {
        "title": "Текст окна",
        "description": "Заголовок и видимый текст элементов окна: поля ввода, надписи, кнопки.",
        "host": "agent", "risk": "read",
        "params": {"title": "text"},
        "requires": ("windows",), "ideas": ("И149",),
        "doc": "EnumChildWindows + WM_GETTEXT с таймаутом: зависшая программа не держит помощника.",
    },
    "window.controls": {
        "title": "Элементы окна",
        "description": "Кнопки, поля и надписи окна с классом и координатами.",
        "host": "agent", "risk": "read",
        "params": {"title": "text"},
        "requires": ("windows",), "ideas": ("И150",),
        "doc": "EnumChildWindows: класс, текст, прямоугольник элемента. Программы, которые рисуют интерфейс сами, отдают мало — это видно по счётчику.",
    },
    "window.click": {
        "title": "Клик",
        "description": "Клик по координатам экрана после подтверждения.",
        "host": "agent", "risk": "write",
        "params": {"x": "int", "y": "int"},
        "requires": ("windows",), "ideas": ("И150",),
        "doc": "SetCursorPos + SendInput, только после подтверждения и только с явными x и y.",
    },
    "window.type": {
        "title": "Ввод текста",
        "description": "Ввести текст в активное окно.",
        "host": "agent", "risk": "write",
        "params": {"text": "text"},
        "requires": ("windows",), "ideas": ("И151",),
        "doc": "Unicode SendInput, язык любой, с подтверждением.",
    },
    "window.snap": {
        "title": "Разложить окна",
        "description": "Разложить два окна по сетке 50/50.",
        "host": "agent", "risk": "write",
        "params": {"left_title": "text", "right_title": "text"},
        "requires": ("windows",), "ideas": ("И210",),
        "doc": "Рабочая область без панели задач, SetWindowPos через ctypes, требует подтверждения.",
    },
    # --- зрение (Н35-Н40, И212, И213) ------------------------------------
    "screen.shot": {
        "title": "Снимок экрана",
        "description": "Снимок экрана в PNG, никуда не уходит, только loopback.",
        "host": "agent", "risk": "read",
        "params": {"max_side": "int"},
        "requires": ("screen",), "ideas": ("И164",),
        "doc": "Pillow/mss, возвращается по loopback, на диске не сохраняется.",
    },
    "screen.region_shot": {
        "title": "Снимок области",
        "description": "Снимок области экрана по координатам.",
        "host": "agent", "risk": "read",
        "params": {"left": "int", "top": "int", "right": "int", "bottom": "int"},
        "requires": ("region_shot",), "ideas": ("И212",),
        "doc": "ImageGrab.grab(bbox), без сохранения на диск.",
    },
    "screen.describe": {
        "title": "Описать экран",
        "description": "Описание экрана локальной моделью, которая видит изображения; текстовая модель честно отказывает.",
        "host": "agent", "risk": "read",
        "params": {"max_side": "int", "question": "text"},
        "requires": ("screen", "model"), "ideas": ("И164",),
        "doc": "Снимок уходит только в локальную модель (loopback) и не сохраняется. Без видящей модели — отказ с советом, а не выдуманное описание.",
    },
    "screen.find": {
        "title": "Найти на экране",
        "description": "Найти текст на экране: распознаванием, если оно установлено, иначе в заголовках окон.",
        "host": "agent", "risk": "read",
        "params": {"text": "text"},
        "requires": ("screen",), "ideas": ("И167",),
        "doc": "rapidocr или pytesseract дают координаты слов; без них — заголовки окон с пометкой method.",
    },
    "screen.find_and_click": {
        "title": "Найти и кликнуть",
        "description": "Найти текст и кликнуть по нему (с подтверждением и whitelist).",
        "host": "agent", "risk": "write",
        "params": {"text": "text"},
        "requires": ("screen", "windows"), "ideas": ("И213",),
        "doc": "Ищет в заголовках, клик только после подтверждения и проверки whitelist.",
    },
    "screen.archive": {
        "title": "Архив экрана",
        "description": "Сохранить снимок в архив (только метаданные, без картинки).",
        "host": "agent", "risk": "own",
        "params": {"title": "text"},
        "requires": ("screen", "sqlite"), "ideas": ("И165",),
        "doc": "Картинка не сохраняется, только заголовок и хеш.",
    },
    "screen.archive_search": {
        "title": "Поиск по архиву экрана",
        "description": "Найти в архиве экрана по заголовку.",
        "host": "agent", "risk": "read",
        "params": {"query": "text", "limit": "int"},
        "requires": ("sqlite",), "ideas": ("И165",),
        "doc": "LIKE по title в screen_archive.",
    },
    "screen.archive_erase": {
        "title": "Стереть архив экрана",
        "description": "Стереть архив экрана (метаданные агента, не файлы) — необратимо, с подтверждением.",
        "host": "agent", "risk": "irreversible",
        "params": {},
        "requires": ("sqlite",), "ideas": ("И165",),
        "doc": "Только после подтверждения, откат невозможен.",
    },
    # --- голос (Н41-Н45, И214) -------------------------------------------
    "voice.listen": {
        "title": "Слушать",
        "description": "Слушать микрофон заданное число секунд и вернуть распознанный текст.",
        "host": "agent", "risk": "system",
        "params": {"seconds": "int"},
        "requires": ("speech_in",), "ideas": ("И163",),
        "doc": "sounddevice + vosk/faster-whisper. Микрофон закрыт по умолчанию (решение владельца): открывается только после подтверждения и только на названное число секунд.",
    },
    "voice.say": {
        "title": "Озвучить",
        "description": "Сказать текст вслух голосом системы (тон — для тревоги или отчёта).",
        "host": "agent", "risk": "own",
        "params": {"text": "text", "tone": "text"},
        "requires": ("speech_out",), "ideas": ("И162",),
        "doc": "SAPI в Windows, say в macOS, espeak-ng в Linux. Текст передаётся данными, а не командной строкой.",
    },
    "voice.dictate": {
        "title": "Диктовка",
        "description": "Диктовка в активное поле: послушать, распознать и ввести текст (с подтверждением).",
        "host": "agent", "risk": "write",
        "params": {"seconds": "int"},
        "requires": ("speech_in", "windows"), "ideas": ("И151",),
        "doc": "Слушает названное число секунд, распознаёт и вводит в активное окно — после подтверждения.",
    },
    "voice.note": {
        "title": "Голосовая заметка",
        "description": "Записать заметку голосом с напоминанием.",
        "host": "agent", "risk": "own",
        "params": {"text": "text", "due": "text"},
        "requires": ("sqlite",), "ideas": ("И168",),
        "doc": "Сохраняет в notes, использует существующую таблицу.",
    },
    "voice.command": {
        "title": "Голосовая команда",
        "description": "Короткая команда без мыши: фраза → понятый навык и параметры, без выполнения.",
        "host": "agent", "risk": "read",
        "params": {"text": "text"},
        "requires": (), "ideas": ("И163",),
        "doc": "Тот же разбор, что у мозга помощника (agent/brain.py): правила и контекст, без модели.",
    },
    "voice.profile": {
        "title": "Профиль голоса",
        "description": "Скорость, тон, громкость озвучки.",
        "host": "agent", "risk": "own",
        "params": {"speed": "text", "tone": "text", "volume": "int"},
        "requires": ("preferences",), "ideas": ("И214",),
        "doc": "Сохраняет в preferences: voice.speed/tone/volume.",
    },
    # --- буфер, таймеры, файлы, предпочтения (И211, И215, И217-И221) -----
    "clipboard.history": {
        "title": "История буфера",
        "description": "Последние тексты из буфера обмена.",
        "host": "agent", "risk": "read",
        "params": {"limit": "int"},
        "requires": ("clipboard", "sqlite"), "ideas": ("И211",),
        "doc": "Читает clipboard_history, дедуп по хешу.",
    },
    "clipboard.read": {
        "title": "Читать буфер",
        "description": "Прочитать текущий текст буфера и сохранить в историю.",
        "host": "agent", "risk": "own",
        "params": {},
        "requires": ("clipboard", "sqlite"), "ideas": ("И211",),
        "doc": "GetClipboardData через ctypes, только Windows.",
    },
    "clipboard.write": {
        "title": "Писать в буфер",
        "description": "Записать текст в буфер обмена.",
        "host": "agent", "risk": "own",
        "params": {"text": "text"},
        "requires": ("clipboard",), "ideas": ("И211",),
        "doc": "SetClipboardData через ctypes, с подтверждением на большие тексты.",
    },
    "scheduler.focus_timer": {
        "title": "Таймер фокуса",
        "description": "Запустить помодоро-таймер на N минут.",
        "host": "agent", "risk": "own",
        "params": {"minutes": "int", "note": "text"},
        "requires": ("focus_timer", "sqlite"), "ideas": ("И215",),
        "doc": "Сохраняет в focus_timers, end_at считается детерминированно.",
    },
    "scheduler.focus_list": {
        "title": "Таймеры фокуса",
        "description": "Список таймеров фокуса.",
        "host": "agent", "risk": "read",
        "params": {"limit": "int"},
        "requires": ("sqlite",), "ideas": ("И215",),
        "doc": "Чтение из focus_timers.",
    },
    "scheduler.focus_stop": {
        "title": "Стоп таймер",
        "description": "Остановить таймер фокуса.",
        "host": "agent", "risk": "own",
        "params": {"timer_id": "int"},
        "requires": ("sqlite",), "ideas": ("И215",),
        "doc": "Ставит status=stopped; без номера — последний запущенный таймер.",
    },
    "files.watch": {
        "title": "Слежка за папкой",
        "description": "Добавить папку в слежку: уведомлять о новых файлах.",
        "host": "agent", "risk": "own",
        "params": {"path": "path", "enabled": "bool"},
        "requires": ("file_watch", "sqlite"), "ideas": ("И217",),
        "doc": "Сохраняет в file_watches, проверка — по расписанию.",
    },
    "files.watches": {
        "title": "Мои слежки за папками",
        "description": "Список папок под наблюдением.",
        "host": "agent", "risk": "read",
        "params": {"limit": "int"},
        "requires": ("sqlite",), "ideas": ("И217",),
        "doc": "Чтение из file_watches.",
    },
    "files.quick_open": {
        "title": "Быстро открыть",
        "description": "Найти файл по имени и открыть: быстрый поиск без индекса.",
        "host": "agent", "risk": "read",
        "params": {"name": "text", "limit": "int"},
        "requires": ("files",), "ideas": ("И218",),
        "doc": "rglob по разрешённым папкам, без записи в индекс.",
    },
    "knowledge.preferences": {
        "title": "Предпочтения",
        "description": "Что ассистент помнит про владельца: тон, единицы, правила.",
        "host": "agent", "risk": "read",
        "params": {"key": "text"},
        "requires": ("preferences", "sqlite"), "ideas": ("И219",),
        "doc": "Чтение из preferences, ключ опционален — тогда весь список.",
    },
    "knowledge.preference_save": {
        "title": "Сохранить предпочтение",
        "description": "Сохранить правило/предпочтение владельца.",
        "host": "agent", "risk": "own",
        "params": {"key": "text", "value": "text"},
        "requires": ("preferences", "sqlite"), "ideas": ("И219",),
        "doc": "Пишет в preferences, ключ — латиница с точкой.",
    },
    "safety.whitelist": {
        "title": "Белый список",
        "description": "Список приложений, куда можно кликать и вводить.",
        "host": "agent", "risk": "read",
        "params": {"limit": "int"},
        "requires": ("whitelist", "sqlite"), "ideas": ("И220",),
        "doc": "Чтение из whitelist.",
    },
    "safety.whitelist_save": {
        "title": "Править белый список",
        "description": "Добавить/убрать приложение из белого списка.",
        "host": "agent", "risk": "write",
        "params": {"app_name": "text", "allowed": "bool"},
        "requires": ("whitelist", "sqlite"), "ideas": ("И220",),
        "doc": "Пишет в whitelist, требует подтверждения.",
    },
    "assistant.macro": {
        "title": "Макрос",
        "description": "Сохранить цепочку навыков как макрос и выполнить по имени.",
        "host": "agent", "risk": "own",
        "params": {"name": "text", "steps": "object", "description": "text"},
        "requires": ("macro", "sqlite"), "ideas": ("И221",),
        "doc": "Сохраняет в macros, шаги — массив {skill,params}, выполняется через agent.learn-логику.",
    },
    "assistant.macros": {
        "title": "Макросы",
        "description": "Список макросов.",
        "host": "agent", "risk": "read",
        "params": {"limit": "int"},
        "requires": ("sqlite",), "ideas": ("И221",),
        "doc": "Чтение из macros.",
    },
    "assistant.macro_run": {
        "title": "Запустить макрос",
        "description": "Выполнить макрос по имени.",
        "host": "agent", "risk": "write",
        "params": {"name": "text"},
        "requires": ("macro", "sqlite"), "ideas": ("И221",),
        "doc": "Берёт шаги из macros и выполняет подряд, с подтверждением.",
    },
    # --- 18.21: мозг помощника — программы, медиа, клавиши, окна, память ---
    "app.open": {
        "title": "Открыть программу, сайт или папку",
        "description": "Открыть программу из белого списка, сайт по адресу или папку внутри разрешённых — чужие исполняемые файлы не запускаются.",
        "host": "agent", "risk": "soft",
        "params": {"target": "text"},
        "requires": (), "ideas": ("И301",),
        "doc": "Блокнот, калькулятор, проводник, диспетчер задач, Bambu Studio, OrcaSlicer, Telegram, панель PrintFlow, Авито и другие известные имена; http(s)-адрес; папка загрузок и знаний. Без аргументов командной строки.",
    },
    "system.media": {
        "title": "Музыка и видео",
        "description": "Пауза, следующий или предыдущий трек, стоп, звук громче, тише или выключить — медиаклавишами для любого плеера.",
        "host": "agent", "risk": "soft",
        "params": {"action": "oneof:play_pause|next|prev|stop|mute|volume_up|volume_down"},
        "requires": ("system",), "ideas": ("И302",),
        "doc": "VK_MEDIA_* через SendInput: работает с браузером, Spotify, плеером Windows. Действие называется явно.",
    },
    "system.hotkey": {
        "title": "Сочетание клавиш",
        "description": "Нажать сочетание клавиш в активном окне (Ctrl+S, Win+D, Enter) — с подтверждением.",
        "host": "agent", "risk": "write",
        "params": {"keys": "text"},
        "requires": ("windows",), "ideas": ("И303",),
        "doc": "Разбор по-русски и по-английски («контрл с» = Ctrl+C). Выход из системы, Ctrl+Alt+Del и Alt+F4 не нажимаются никогда.",
    },
    "window.arrange": {
        "title": "Свернуть или развернуть окно",
        "description": "Свернуть, развернуть на весь экран или восстановить окно по названию программы.",
        "host": "agent", "risk": "soft",
        "params": {"action": "oneof:minimize|maximize|restore", "title": "text"},
        "requires": ("windows",), "ideas": ("И304",),
        "doc": "ShowWindow по найденному окну. Без названия — окно, с которым работает человек (окно помощника не трогается).",
    },
    "window.close": {
        "title": "Закрыть окно",
        "description": "Попросить программу закрыть окно: про несохранённое она спросит сама. С подтверждением.",
        "host": "agent", "risk": "write",
        "params": {"title": "text"},
        "requires": ("windows",), "ideas": ("И304",),
        "doc": "WM_CLOSE, а не завершение процесса: программа сохраняет данные по своим правилам. Без названия окна навык отказывает.",
    },
    "memory.remember": {
        "title": "Запомнить",
        "description": "Запомнить факт, правило или предпочтение владельца в памяти помощника.",
        "host": "agent", "risk": "own",
        "params": {"text": "text", "kind": "oneof:fact|preference|profile|person|rule", "subject": "text"},
        "requires": ("sqlite",), "ideas": ("И305",),
        "doc": "Таблица memories своей базы. Повтор не плодит дубль, новое имя владельца заменяет старое.",
    },
    "memory.recall": {
        "title": "Вспомнить",
        "description": "Найти в памяти помощника то, что владелец просил запомнить.",
        "host": "agent", "risk": "read",
        "params": {"query": "text", "limit": "int"},
        "requires": ("sqlite",), "ideas": ("И305",),
        "doc": "Совпадение основ слов («Марии» = «Мария»); без запроса — последние записи.",
    },
    "memory.forget": {
        "title": "Забыть",
        "description": "Стереть запись памяти по номеру или по словам — по просьбе владельца.",
        "host": "agent", "risk": "own",
        "params": {"what": "text"},
        "requires": ("sqlite",), "ideas": ("И305",),
        "doc": "Стирается только уверенное совпадение; при двух равных кандидатах — обе записи показываются, стирается совпавшая точнее.",
    },

}



# ---------------------------------------------------------------------------
# Чтение реестра
# ---------------------------------------------------------------------------

def get(name: str, learned: dict[str, dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """Навык по имени: встроенные и выученные в одном пространстве имён.

    Выученный навык не может перекрыть встроенный — имя `panel.do` остаётся
    панельным действием даже если кто-то попытался выучить навык с таким именем
    (`learn` такие имена отвергает).
    """
    key = str(name or "").strip().casefold()
    if key in SKILLS:
        return {"name": key, **SKILLS[key], "learned": False}
    if learned and key in learned:
        return {"name": key, **learned[key], "learned": True}
    return None


def names(learned: dict[str, dict[str, Any]] | None = None) -> list[str]:
    """Все имена навыков: встроенные, затем выученные по алфавиту."""
    return sorted(SKILLS) + sorted(learned or {})


def availability(skill: dict[str, Any], caps: dict[str, Any]) -> tuple[bool, str]:
    """Доступен ли навык на этом компьютере — и почему нет.

    Причина всегда из `capabilities`: навык не сочиняет объяснение, а показывает
    то, что агент знает о себе. Это и есть «честность как функция» (И178).
    """
    for need in tuple(skill.get("requires") or ()):
        if not caps.get(need):
            reason = str(caps.get(f"{need}_reason") or "").strip()
            return False, reason or f"Нет способности «{need}»"
    return True, ""


def confirm_required(skill: dict[str, Any]) -> bool:
    """Нужно ли подтверждение человека. Реестр, а не запрос, решает это.

    Правило: риск из `CONFIRM_RISKS` — подтверждение обязательно. Выученный
    навык подтверждается всегда, потому что он исполняет цепочку чужих шагов.
    """
    if skill.get("learned"):
        return True
    if bool(skill.get("confirm")):
        return True
    return str(skill.get("risk") or "read") in CONFIRM_RISKS


def risk_of(skill: dict[str, Any]) -> str:
    risk = str(skill.get("risk") or "read")
    return risk if risk in RISKS else "read"


def payload(skill: dict[str, Any], caps: dict[str, Any] | None = None) -> dict[str, Any]:
    """Навык для интерфейса и для модели: без внутренних полей, с доступностью."""
    available, reason = availability(skill, caps or {})
    return {
        "name": str(skill.get("name") or ""),
        "title": str(skill.get("title") or ""),
        "description": str(skill.get("description") or ""),
        "host": str(skill.get("host") or "agent"),
        "risk": risk_of(skill),
        "confirm": confirm_required(skill),
        "params": {str(k): str(v) for k, v in dict(skill.get("params") or {}).items()},
        "requires": list(skill.get("requires") or ()),
        "ideas": list(skill.get("ideas") or ()),
        "doc": str(skill.get("doc") or ""),
        "learned": bool(skill.get("learned")),
        "available": bool(available),
        "reason": str(reason),
    }


def catalog(caps: dict[str, Any], learned: dict[str, dict[str, Any]] | None = None
            ) -> list[dict[str, Any]]:
    """Весь реестр одним списком: встроенные навыки, затем выученные."""
    out = [payload({"name": name, **skill}, caps) for name, skill in sorted(SKILLS.items())]
    out += [payload({"name": name, **skill}, caps) for name, skill in sorted((learned or {}).items())]
    return out


def prompt(caps: dict[str, Any], learned: dict[str, dict[str, Any]] | None = None) -> str:
    """Каталог для модели: только доступные навыки, одним текстом.

    Недоступные в промпт не попадают вовсе — иначе модель уверенно предлагает
    то, что на этом компьютере не работает, и человек получает отказ после
    ожидания. Их показывает интерфейс, а не модель.
    """
    lines = []
    for row in catalog(caps, learned):
        if not row["available"]:
            continue
        params = ", ".join(f"{k}:{v}" for k, v in row["params"].items())
        mark = " — только с подтверждением" if row["confirm"] else ""
        lines.append(f"- {row['name']}: {row['title']}"
                     + (f" (параметры: {params})" if params else "")
                     + f" — {row['description']}{mark}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Параметры: разбор и отказ
# ---------------------------------------------------------------------------

def _clean_value(spec: str, value: Any) -> tuple[Any, str]:
    """Одно значение по типу. Возвращает (значение, причина отказа)."""
    kind, _, variants = str(spec).partition(":")
    if value is None:
        return None, ""
    if kind == "text":
        text = " ".join(str(value).split())
        return (text[:MAX_TEXT_PARAM], "") if text else (None, "")
    if kind == "int":
        try:
            return int(str(value).strip()), ""
        except (TypeError, ValueError):
            return None, f"«{value}» не целое число"
    if kind == "float":
        try:
            return float(str(value).strip().replace(",", ".")), ""
        except (TypeError, ValueError):
            return None, f"«{value}» не число"
    if kind == "bool":
        if isinstance(value, bool):
            return value, ""
        text = str(value).strip().casefold()
        if text in ("1", "true", "да", "yes", "on", "выполнить"):
            return True, ""
        if text in ("0", "false", "нет", "no", "off", "план"):
            return False, ""
        return None, f"«{value}» не похоже на да/нет"
    if kind == "date":
        text = str(value).strip()
        if not _DATE_RE.match(text):
            return None, f"«{value}» не дата вида ГГГГ-ММ-ДД"
        return text, ""
    if kind == "path":
        text = " ".join(str(value).split())[:MAX_TEXT_PARAM]
        if not text:
            return None, ""
        if "\x00" in text:
            return None, "путь содержит недопустимый символ"
        return text, ""
    if kind == "object":
        if isinstance(value, dict):
            if len(value) > MAX_OBJECT_PARAM:
                return None, f"структура больше {MAX_OBJECT_PARAM} полей"
            return value, ""
        return None, "нужна структура, а не строка"
    if kind == "oneof":
        text = str(value).strip().casefold()
        allowed = tuple(part.strip() for part in variants.split("|") if part.strip())
        if text not in allowed:
            return None, f"«{value}» не входит в список: {', '.join(allowed)}"
        return text, ""
    return None, f"неизвестный тип параметра «{spec}»"


def check_params(skill: dict[str, Any], raw: Any) -> tuple[dict[str, Any], list[str]]:
    """Параметры навыка: только объявленные имена, только разобранные значения.

    Неизвестные имена отбрасываются с предупреждением: модель, придумавшая
    `delete_all`, не должна получить исполнение. Ошибка в значении — отказ
    навыка с причиной, а не подстановка значения по умолчанию.
    """
    spec: dict[str, str] = {str(k): str(v) for k, v in dict(skill.get("params") or {}).items()}
    source = raw if isinstance(raw, dict) else {}
    out: dict[str, Any] = {}
    errors: list[str] = []
    for key, value in source.items():
        name = str(key).strip()
        if name not in spec:
            errors.append(f"Параметр «{name}» у навыка не объявлен — отброшен")
            continue
        clean, reason = _clean_value(spec[name], value)
        if reason:
            errors.append(f"{name}: {reason}")
            continue
        if clean is not None:
            out[name] = clean
    missing = [name for name, kind in spec.items()
               if name not in out and not kind.endswith("?") and name not in _OPTIONAL]
    if missing:
        errors.append("Не хватает параметров: " + ", ".join(sorted(missing)))
    return out, errors


# Параметры, которые навык умеет домыслить сам (папка по умолчанию, предел
# выдачи). Их отсутствие — не ошибка, а обычная работа: «индекс» без папки
# индексирует то, что задано в настройках агента.
_OPTIONAL = ("folders", "folder", "limit", "days", "save", "execute", "params",
             "question", "query", "skill", "path", "order", "kind", "action",
             "city", "category", "max_price", "min_price", "enabled",
             "watch_id", "only_new", "thread", "intent", "topic", "tone",
             "facts", "source", "context", "status", "draft_id", "chat", "text",
             "interval_hours", "notify", "image_hash", "planned_at", "name",
             "template", "template_id", "idea", "format", "listing_id", "price",
             "url", "title", "replies",
             "app_name", "device_id", "level", "minutes", "x", "y", "left_title",
             "right_title", "left", "top", "right", "bottom", "max_side",
             "seconds", "speed", "volume", "timer_id", "note", "key", "value",
             "steps", "description", "what", "confirm_text", "mode",
             "interval", "stale_sec", "max_restarts", "dry",
             # 18.21: у громкости всё необязательно — без параметров она читается;
             # у памяти и окон отказ с причиной делает сам обработчик.
             "delta", "mute", "target", "subject", "due", "roles")


# ---------------------------------------------------------------------------
# Обучение: новый навык словами (И180)
# ---------------------------------------------------------------------------

def learn(raw: Any, learned: dict[str, dict[str, Any]] | None = None) -> tuple[dict[str, Any] | None, str]:
    """Собрать навык из шагов существующих. Возвращает (навык, причина отказа).

    Ограничения сознательные:
      * имя — только `my.<что_делает>`: выученный навык не может занять имя
        встроенного и не может выглядеть как панельный;
      * шаги — только существующие навыки, и не другие выученные: цепочка
        цепочек непроверяема, а отказ в середине длинной цепочки необъясним;
      * риск — максимальный из шагов, подтверждение — всегда;
      * параметров у выученного навыка нет: он повторяет записанные шаги, а не
        принимает значения от модели. Так выученный навык не становится
        способом обойти проверку параметров встроенного.
    """
    if not isinstance(raw, dict):
        return None, "Навык описывается структурой, а не строкой"
    learned = learned or {}
    name = str(raw.get("name") or "").strip().casefold()
    if not _LEARNED_RE.match(name):
        return None, ("Имя выученного навыка должно быть вида my.короткое_название "
                      "(латиница, цифры, подчёркивание)")
    if len(name) > MAX_LEARNED_NAME:
        return None, f"Имя длиннее {MAX_LEARNED_NAME} символов"
    if name in learned:
        return None, f"Навык «{name}» уже выучен — назовите иначе или удалите старый"
    title = " ".join(str(raw.get("title") or "").split())[:120]
    if not title:
        return None, "Без названия навык не показать человеку"
    steps_raw = raw.get("steps")
    if not isinstance(steps_raw, list) or not steps_raw:
        return None, "Нужен непустой список шагов"
    if len(steps_raw) > MAX_LEARNED_STEPS:
        return None, f"Больше {MAX_LEARNED_STEPS} шагов: это программа, а не навык"

    steps: list[dict[str, Any]] = []
    order = 0
    for item in steps_raw:
        if not isinstance(item, dict):
            return None, f"Шаг {order + 1}: шаг описывается структурой"
        step_name = str(item.get("skill") or "").strip().casefold()
        base = SKILLS.get(step_name)
        if base is None:
            return None, (f"Шаг {order + 1}: навыка «{step_name}» нет в реестре"
                          if step_name not in learned
                          else f"Шаг {order + 1}: выученный навык не может звать другой выученный")
        params, errors = check_params({"params": base.get("params") or {}}, item.get("params"))
        if errors:
            return None, f"Шаг {order + 1} ({step_name}): " + "; ".join(errors)
        steps.append({"skill": step_name, "params": params})
        order += 1

    risk = RISKS[max(RISKS.index(risk_of(SKILLS[step["skill"]])) for step in steps)]
    requires: list[str] = []
    for step in steps:
        for need in SKILLS[step["skill"]].get("requires") or ():
            if need not in requires:
                requires.append(need)
    description = " ".join(str(raw.get("description") or "").split())[:240] or title
    skill = {
        "title": title,
        "description": description,
        "host": "agent",
        "risk": risk,
        "confirm": True,
        "params": {},
        "steps": steps,
        "requires": tuple(requires),
        "ideas": ("И180",),
        "doc": "Выучен владельцем: исполняет шаги подряд и всегда спрашивает подтверждение.",
    }
    return skill, ""


def validate() -> list[str]:
    """Самопроверка реестра. Её зовёт тест — и она же объясняет отказ (И178).

    Реестр, который сам себя не проверяет, расходится с исполнением незаметно:
    навык объявлен, а диспетчер про него не знает. Поэтому здесь те же правила,
    которые проверяет контракт `test_assistant_skills.py`.
    """
    problems: list[str] = []
    for name, skill in SKILLS.items():
        if not _NAME_RE.match(name):
            problems.append(f"{name}: имя не соответствует образцу «группа.навык»")
        for key in ("title", "description", "host", "risk", "params", "requires", "ideas", "doc"):
            if key not in skill:
                problems.append(f"{name}: нет поля «{key}»")
        if skill.get("host") not in HOSTS:
            problems.append(f"{name}: хост «{skill.get('host')}» неизвестен")
        if risk_of(skill) not in RISKS:
            problems.append(f"{name}: риск «{skill.get('risk')}» неизвестен")
        if risk_of(skill) in CONFIRM_RISKS and not confirm_required(skill):
            problems.append(f"{name}: риск требует подтверждения, а навык его не спрашивает")
        for need in skill.get("requires") or ():
            if need not in CAPABILITIES:
                problems.append(f"{name}: требует неизвестную способность «{need}»")
        for key, spec in dict(skill.get("params") or {}).items():
            kind = str(spec).partition(":")[0]
            if kind not in PARAM_TYPES:
                problems.append(f"{name}: параметр «{key}» имеет неизвестный тип «{spec}»")
        if not skill.get("ideas"):
            problems.append(f"{name}: не указано, из какой идеи навык вырос")
    return problems
