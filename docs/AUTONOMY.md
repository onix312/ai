# Autonomy Levels 1.0

Autonomy Levels отделяет два разных вопроса:

1. **что NOZZA умеет** — это skills/providers;
2. **насколько далеко ей разрешено действовать** — это autonomy policy.

Autonomy никогда не отменяет существующий risk/confirmation слой.

## Уровни

| Level | Что разрешено |
| --- | --- |
| Observer | Читать состояние и отвечать. Любые изменения блокируются. |
| Assistant | Одиночные пользовательские команды. Write/system/irreversible по-прежнему требуют confirmation. |
| Operator | Persistent Task Engine и многошаговые сценарии. |
| Agent | Planner / Verification / Replan pipeline. |
| Autopilot | Зарезервирован для будущего фонового Event Engine. |

По умолчанию используется **Agent**, чтобы обновление не отключало уже работающие Task/Planner сценарии.

## Enforcement

Проверка находится выше execution:

```text
Brain / API
    ↓
Autonomy Policy
    ↓
skills validation
    ↓
risk + confirmation
    ↓
Task / Runner / Provider
    ↓
executor
```

Для Task Engine каждый шаг дополнительно выполняется с context=`task`.
Planner/Replanner draft можно читать на более низком уровне, но approve требует
уровень Agent.

Если pending action был создан, а уровень потом понизили, confirmation
перепроверяет policy ещё раз. Старое разрешение не сохраняется.

Legacy endpoints `/click`, `/type`, `/key`, `/activate` также проходят
Autonomy Policy.

## Provider ceilings

Кроме глобального уровня есть потолок по provider.

Безопасные hard max по умолчанию:

- Browser: Autopilot
- Personal: Autopilot
- Desktop: Agent
- PrintFlow: Agent
- Core: Agent

Пользователь может только **понизить** потолок. Поднять provider выше hard max
через UI/API нельзя.

Это особенно важно для будущего Autopilot: например PrintFlow не получит
фоновое выполнение только потому, что глобальный уровень стал Autopilot.

## Persistence

Настройки хранятся в локальной SQLite ассистента через существующую таблицу
`preferences`:

- `autonomy.level`
- `autonomy.provider.browser`
- `autonomy.provider.desktop`
- `autonomy.provider.printflow`
- `autonomy.provider.personal`
- `autonomy.provider.core`

## API

Agent port:

```text
GET  /autonomy
POST /autonomy
```

Update:

```json
{
  "op": "update",
  "level": "operator",
  "providers": {
    "printflow": "assistant"
  }
}
```

Reset:

```json
{"op":"reset"}
```

## Native UI

Control Center → Настройки → Autonomy:

- глобальный уровень;
- ceilings Browser/Desktop/PrintFlow/Personal/Core;
- safe hard max автоматически ограничивает варианты;
- кнопки сохранить / сбросить.

## Invariants

- Autonomy не понижает risk.
- Autonomy не снимает confirmation.
- Persona не меняет Autonomy.
- Planner не может повысить Autonomy.
- Provider не может повысить Autonomy.
- Pending confirmation перепроверяется в момент выполнения.
- Observer не может менять систему через legacy raw endpoints.

## Следующий этап

Следующий слой roadmap — **Proactivity / Event Engine 1.0**. Он должен
использовать Autopilot только как policy gate, а не как замену confirmations.
