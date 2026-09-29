# NOZZA — handoff и roadmap после Verification Engine 1.0

> **Актуализация 29.09.2026, Native UI 2.0 Phase 1:** Voice Engine 3 Phase 6 уже в `main`.
> Текущая ветка переводит нативный интерфейс на identity Люмы, делает Voice Orb
> живым индикатором listening/thinking/speaking и добавляет backend-level
> **STOP ALL** с tray/control-center кнопкой и глобальным hotkey. STOP ALL
> отменяет model turns, TTS и pending confirmations, выключает listening,
> ставит Task Engine на паузу после текущего atomic step и блокирует новые
> skills до явного Resume. Следом: Native UI 2.0 Phase 2 — richer live
> transcript/status surface, task activity и более цельный Control Center.


> **Актуализация 29.09.2026, Native UI 2 Phase 1:** Voice 3 Phase 6 уже в `main`.
> Текущая ветка переводит PySide6 интерфейс на identity **Люмы**, оживляет Orb
> и добавляет единый latched **STOP ALL**: TTS/model/voice/pending/tasks
> останавливаются через backend primitive, новые actions блокируются до Resume.
> Поверх него работают tray, Control Center и global hotkey
> `Ctrl+Alt+Shift+Space`. Следом: richer Quick Panel + streaming conversation
> surface + voice diagnostics.


> **Актуализация 29.09.2026, Voice 3 Phase 6:** Люма и Phase 5 уже в `main`.
> Текущая ветка добавляет adaptive acoustic echo gate: во время собственного
> TTS Люма обучает только фон утечки динамика, поднимает VAD-порог над ним и не
> пускает подавленные speaker frames в ASR pre-roll. Сильный человеческий
> всплеск проходит, а text-level echo rejection остаётся вторым барьером.
> Следом: Native UI 2.0 и optional HQ local TTS; true AEC имеет смысл, когда
> TTS backend сможет отдавать PCM reference stream.


> **Актуализация 29.09.2026, Voice 3 Phase 5:** Phase 4 уже в `main`.
> Новое имя помощника — **Люма**, primary wake-word — `люма`.
> Старые `ноза/нозза/nozza/noza` временно сохранены как legacy wake aliases.
> Phase 5 добавляет wake-word barge-in во время TTS и text-level echo rejection,
> чтобы Люма не реагировала на собственный голос. Следом: acoustic echo
> suppression → optional HQ local TTS → Native UI 2.0.


> **Актуализация 29.09.2026, Voice 3 Phase 4:** Phase 3 уже в `main`.
> Текущая ветка добавляет Dynamic Vocabulary: локальные имена приложений,
> проектов, принтеров, клиентов, целей и выученных команд мягко корректируют
> финальный ASR без hard grammar. Неизвестная обычная речь остаётся open-vocabulary.
> Следом: усиление wake-word/echo suppression → optional HQ local TTS.


> **Актуализация 29.09.2026, Voice 3 Phase 3:** Phase 2 уже в `main`.
> Текущая ветка добавляет sentence-level streaming TTS для свободных voice
> ответов. Ранний TTS разрешён только при пустом `skill`, поэтому NOZZA не
> произносит «готово» до фактического executor result. Stop token по-прежнему
> закрывает модель, текущий TTS и очередь следующих предложений.
> Следом: dynamic vocabulary → wake-word/echo suppression → optional HQ local TTS.


> **Актуализация 29.09.2026, Voice 3 Phase 2:** Phase 1 уже в `main`.
> Текущая ветка добавляет barge-in до уровня модели: stop во время thinking
> отменяет только активный voice generation, не сохраняет оборванный ответ в
> историю и возвращает голосовой цикл в listening. Следом: streaming TTS,
> dynamic vocabulary и усиление wake-word/echo suppression.


> **Актуализация 29.09.2026, Voice 3:** Local Trusted Mode 1.0 уже в `main`.
> Текущая ветка — **Voice Engine 3.0 Phase 1**: incremental Vosk ASR, partial
> transcript только для UI, VAD pre-roll и live mic level в Voice Orb.
> После этой фазы: interruption model generation → streaming TTS → dynamic
> vocabulary → усиление wake-word/echo suppression.


> **Актуализация 29.09.2026:** Provider Architecture 1.0, Persona Engine 1.0,
> Autonomy Levels 1.0 и Proactivity/Event Engine 1.0 уже в `main`.
> Большой Security 2.0 отложен: NOZZA работает как локальный доверенный
> ассистент. Текущая ветка — **Local Trusted Mode 1.0**: обратимые локальные
> write/system действия выполняются без лишнего подтверждения, а confirmation
> остаётся на внешних, физических и необратимых границах.
> После этого основной приоритет: **Voice Engine 3.0 → Native UI 2.0 →
> Packaging / NOZZA.exe → постепенный Core refactor**.


> **Актуализация 28.09.2026:** после первоначального handoff в `main` уже
> реализованы Replan Engine 1.0, Memory 2.0 и Desktop Perception 2.0.
> Текущий этап этой ветки — Browser Provider 1.0. После него следующий
> архитектурный приоритет: Provider Architecture cleanup, затем Persona Engine,
> Autonomy Levels и Proactivity/Event Engine.
> Provider Architecture 1.0 — текущая ветка: Browser + Desktop уже мигрируются
> из executor в provider registry; после завершения следующий продуктовый слой —
> Persona Engine.


Эта ветка создана как точка продолжения для следующей версии агента.
Главная идея проекта: NOZZA — отдельный локальный AI-человек в ПК, а PrintFlow — один из её providers.

## Уже сделано в main

### Native PySide6 UI
- Tray
- Voice Orb
- Quick Panel
- Full Control Center
- Разговор / Сегодня / Задачи / Память / Обучение / Навыки / Журнал / Настройки
- UI является клиентом loopback API и не содержит Brain
- старый /ui оставлен как fallback

### Voice Engine 2.0
- always-on локальный wake word «Ноза»
- VAD
- follow-up разговор без повторного wake word
- barge-in для остановки TTS
- защита от self-echo
- interruptible TTS
- auto-retry микрофона
- live voice state в Tray и Voice Orb

### Task Engine 1.0
- persistent задачи в SQLite
- goal + ordered skill steps
- progress / pause / resume / cancel
- confirmation gating
- restart recovery
- interrupted task не повторяет шаг молча
- каждый шаг выполняется только через Agent.run_skill
- Native UI показывает задачи и прогресс

### Planner 1.0
- общая цель -> preview plan
- LLM предлагает только существующие skills
- skill/params проходят повторную серверную валидацию
- opaque draft id
- approve/discard
- approve создаёт Task Engine task
- опасные шаги всё равно требуют обычных confirmations

### Verification Engine 1.0
- post-action verification
- evidence у шага
- статусы verified / assumed / failed
- состояние проверки видно в Native UI

## Что делаем сейчас: Replan Engine 1.0

Цель: добавить цикл Goal -> Plan -> Execute -> Observe -> Verify -> Replan.

Если verification = failed, NOZZA должна не просто остановиться, а построить безопасный preview изменения оставшейся части плана.

### Новый модуль
Предлагаемый файл: agent/replanner.py

Вход:
- goal задачи
- snapshot Task Engine
- failed/current step
- verification evidence
- remaining steps
- доступный skill catalog

Выход: только replan draft, без выполнения действий.

### Replan draft
- opaque id
- task_id
- reason
- summary
- replace_from
- old remaining tail
- proposed steps
- risk/confirm metadata
- TTL

### Обязательная повторная валидация
Каждый новый шаг должен пройти:
- skill exists
- availability
- params schema
- denied meta-skills
- max steps
- risk / confirmation metadata

### Task Engine mutation
Нужна безопасная операция replace_remaining(task_id, replace_from, new_steps, reason).

Правила:
- только paused/failed task
- done steps immutable
- менять можно только незавершённый хвост
- replan reason сохраняется
- после применения task остаётся paused
- продолжение только по явной команде человека

### Автоматизация v1
Автоматически разрешено только построить replan preview после failed verification.

Автоматически запрещено:
- применять replan
- повторять irreversible step
- запускать retry loop
- менять done history

### Native UI
В разделе Задачи нужна отдельная карточка:
- причина, почему старый план перестал работать
- старый хвост
- новый хвост
- risk badges
- confirmation badges
- Принять новый план
- Оставить как есть

### API
- GET /replans
- POST /replans

Операции:
- preview
- approve
- discard

## Definition of Done для Replan Engine 1.0
1. failed verification создаёт replan preview
2. preview валидируется registry
3. approve заменяет только незавершённый хвост
4. done steps immutable
5. опасные новые steps сохраняют обычные confirmations
6. crash/restart не запускает replan автоматически
7. draft имеет TTL
8. draft одноразовый
9. UI показывает diff
10. есть unit tests
11. есть API contract tests
12. есть Windows PySide6 smoke
13. CI полностью зелёный
14. только после этого merge в main

## Что делать после Replan Engine

### 1. Memory 2.0
Разделить память на:
- Working Memory
- Episodic Memory
- Semantic Memory
- User Model

Добавить provenance для каждой памяти:
- source
- created_at
- confidence
- observed_count
- explicit / observed / inferred

NOZZA не должна выдавать догадку за факт.

### 2. Desktop Perception 2.0
Единый слой восприятия ПК.
Приоритет источников:
structured OS state -> UI Automation -> OCR -> screenshot -> vision model.

Нужно:
- active window
- UI tree
- controls/buttons
- focused element
- screenshot on demand
- OCR fallback
- visual model только когда структурированных данных недостаточно

### 3. Browser Provider
Критически важный следующий provider.

NOZZA должна видеть:
- tabs
- URL
- title
- visible text
- DOM/accessibility
- links
- buttons
- forms
- selected text

И уметь:
- объяснить страницу
- сравнить вкладки
- заполнить форму
- найти условия
- сохранить страницу в память

### 4. Provider Architecture
Целевая структура:
- providers/windows
- providers/browser
- providers/files
- providers/printflow
- providers/telegram
- providers/personal

Каждый capability описывает:
- name
- params
- risk
- reversible
- confirmation
- verifier

### 5. Persona Engine
Стабильная личность NOZZA:
- name
- voice
- speaking style
- verbosity
- humor
- initiative
- preferred address
- relationship style

Persona не имеет права переписывать safety policy.

### 6. Autonomy Levels
Level 0 Observer
Level 1 Assistant
Level 2 Operator
Level 3 Agent
Level 4 Autopilot

При этом каждый capability имеет собственный предел автономности.

### 7. Proactivity / Event Engine
Источники:
- reminders
- deadlines
- task state
- PrintFlow events
- calendar
- app state
- routines

Нужен attention budget:
- silent
- important only
- balanced
- active

### 8. Security 2.0
Нужно:
- Secret Vault
- Sensitive Window deny-list
- Guest Mode
- Global Panic hotkey
- остановка active task / TTS / UI automation / queued autonomous work

### 9. Voice Engine 3.0
- streaming ASR
- partial transcripts
- better echo cancellation
- более качественный wake-word detector
- dynamic vocabulary
- names/projects vocabulary
- streaming TTS
- emotion/intent voice modes
- interruption не только TTS, но и model generation

### 10. Native UI 2.0
- animated orb по уровню микрофона
- richer task timeline
- replan diff
- memory editor
- provenance viewer
- activity graph
- provider status
- autonomy settings
- privacy controls
- guest mode
- panic button
- first-run wizard

### 11. Packaging / NOZZA.exe
- reproducible build
- signed executable
- updater
- Windows startup
- tray autostart
- local crash reporting
- log rotation
- settings migrations
- model installer/download wizard
- clean uninstall

### 12. Assistant Core refactor
Постепенно перейти к структуре:
- assistant/core
- assistant/memory
- assistant/voice
- assistant/perception
- assistant/planner
- assistant/tasks
- assistant/providers
- assistant/safety
- assistant/ui

PrintFlow должен стать provider, а не центром Assistant.
Не делать big-bang rewrite. Переносить модуль за модулем с compatibility imports.

## Рекомендуемый порядок после текущей ветки
1. Replan Engine 1.0
2. Memory 2.0
3. Desktop Perception 2.0
4. Browser Provider
5. Provider Architecture cleanup
6. Persona Engine
7. Autonomy Levels
8. Proactivity/Event Engine
9. Security 2.0
10. Voice Engine 3.0
11. Native UI 2.0
12. Packaging / NOZZA.exe
13. постепенный Assistant Core refactor

## Главный архитектурный инвариант
Replanner, Planner, UI и будущие providers не получают собственный обходной execution path.

Любое реальное действие должно идти:
Replan/Planner -> Task Engine -> Agent.run_skill -> validation -> confirmation policy -> executor.

## Правило работы с GitHub
feature branch -> implementation -> tests -> PR -> all CI green -> self-review -> squash merge to main.

Не лить незавершённые или красные изменения в main.