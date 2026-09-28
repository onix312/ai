# Provider Architecture 1.0

Provider Architecture отделяет интеграции от центрального executor.

До этого `agent/executor.py` одновременно был:

- policy entry point;
- skill dispatcher;
- Browser implementation adapter;
- Desktop Perception adapter;
- и постепенно становился владельцем всех будущих интеграций.

Provider Architecture оставляет executor одну важную работу: выполнить уже
проверенный skill через правильного владельца.

## Поток

```text
Brain / Planner / Task Engine
          ↓
      Agent.run_skill
          ↓
skills validation + confirmation
          ↓
      Runner.run
          ↓
 Provider Registry
    ├─ Browser Provider
    ├─ Desktop Provider
    ├─ PrintFlow Provider
    ├─ Personal Provider
    └─ далее Files / Telegram / Windows
```

Provider не обходит safety. Он получает только params, которые уже прошли
`skills.check_params`, availability и confirmation policy.

## Provider contract

Каждый provider объявляет:

- name;
- title;
- required capabilities;
- owned skills;
- reversible flag для каждого перенесённого skill;
- run(skill_name, params, runner).

Stateful provider получает `Runner` только после skill validation и
confirmation. Это позволяет использовать локальные store/panel зависимости, не
перенося policy внутрь provider.

Risk и confirmation остаются в canonical `agent/skills.py`. Provider Registry
показывает их рядом со своими skills, но не имеет права понижать риск.

## Что перенесено в v1

### Browser Provider

- browser.tabs
- browser.page
- browser.find
- browser.selection

### Desktop Provider

- desktop.observe

### PrintFlow Provider

- panel.actions
- panel.do
- panel.ask
- day.briefing
- day.summary

PrintFlow остаётся отдельным локальным сервисом. Provider получает текущий
`Runner.panel`, но не принимает решений о risk/confirmation самостоятельно.

### Personal Provider

- reminders
- lists
- goals
- habits
- expenses
- diary
- me.today
- learning skills

Personal Provider использует локальную SQLite ассистента через существующие
`personal_skills` handlers.

Эти skills больше не выбираются центральным handler-map `executor.py`.

## API

`GET /providers` возвращает:

- provider name/title;
- available;
- reason;
- required capabilities;
- список owned skills;
- risk;
- confirmation;
- reversible.

## Native UI

Control Center → Настройки показывает зарегистрированные providers, их
доступность и owned skills.

## Migration policy

Это намеренно постепенная миграция, не big-bang refactor.

Следующие кандидаты:

1. Files Provider;
2. Telegram Provider;
3. Windows Provider.

Миграция остаётся пошаговой: Provider Architecture 1.0 даёт единый контракт,
ownership и status API, а перенос тяжёлых интеграций делается отдельными
безопасными срезами.

Каждый перенос должен:

- сохранять имя skill;
- сохранять risk;
- сохранять confirmation;
- сохранять параметры;
- иметь provider ownership test;
- не менять публичный Brain/Task/Planner контракт.

## Invariants

1. Один skill принадлежит максимум одному provider.
2. Skill с `provider=` обязан иметь владельца в Provider Registry.
3. Provider не выполняет неподписанный/неизвестный skill.
4. Executor policy остаётся выше provider.
5. Provider не читает raw LLM output.
6. Provider не решает, нужно ли confirmation.
7. Ошибка provider остаётся обычным skill failure и не роняет Agent.
