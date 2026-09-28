# Proactivity / Event Engine 1.0

Event Engine добавляет NOZZA инициативность, но в первом релизе намеренно не
получает права выполнять действия.

## Главная граница

Event Engine 1.0 может только:

- наблюдать безопасное локальное состояние;
- записать событие;
- решить, стоит ли прервать пользователя;
- создать notification;
- объяснить, почему notification был показан или подавлен.

Он **не вызывает skills** и не запускает Task Engine самостоятельно.

## Explicit и proactive

Есть два разных класса событий.

### Explicit commitments

Это то, что пользователь сам попросил заранее:

- reminder;
- focus timer;
- note with deadline;
- habit reminder.

Они продолжают работать через существующий `Personal.tick()` и не зависят от
Autopilot. Event Engine лишь зеркалит их в общий event journal.

Это значит, что режим `silent` не ломает фразу:

> «Напомни мне в 18:00 позвонить.»

### Proactive events

Это события, которые NOZZA сама считает полезными:

- срок цели сегодня или завтра;
- просроченная цель;
- Task Engine остановился с failed.

Такие события могут прервать пользователя только если глобальный уровень
Autonomy = `autopilot`.

## Attention budget

Настройки:

- `silent` — никаких инициативных interruptions;
- `important` — только important / urgent;
- `balanced` — normal и выше;
- `active` — low и выше.

Дополнительно:

- max non-urgent interruptions per hour;
- cooldown между событиями одного source/kind;
- quiet hours;
- urgent может пройти через quiet hours.

По умолчанию:

```text
mode                    balanced
max_nonurgent_per_hour  2
cooldown_minutes        30
quiet_start             22:00
quiet_end               08:00
```

При обычном default Autonomy = Agent proactive events будут журналироваться как
suppressed и не прерывать пользователя.

## Event journal

SQLite table:

`assistant_events`

Сохраняется:

- source;
- event_key;
- provider;
- kind;
- title/text;
- urgency;
- explicit flag;
- state;
- suppression reason;
- notification id;
- occurrences;
- payload;
- created_at / last_seen.

Повтор одного и того же event key не создаёт новую карточку, а увеличивает
`occurrences`.

Старые delivered/suppressed события старше 90 дней удаляются автоматически.

## Sources v1

### Personal goals

Event Engine смотрит active goals с deadline:

- tomorrow → normal;
- today → important;
- overdue → important.

### Task Engine

Failed task → important proactive event.

В v1 события задач только уведомляют. Они не делают retry и не применяют replan.

## Scheduler

Существующий scheduler остаётся один:

```text
Agent.tick()
  ├ Personal.tick()      explicit commitments
  ├ Event journal mirror
  └ EventEngine.tick()   proactive scans
```

Ошибка Event Engine ловится отдельно и не может сломать обычное напоминание.

## API

Agent loopback port:

```text
GET  /proactivity
POST /proactivity
GET  /events?limit=50&state=
```

Update:

```json
{
  "op": "update",
  "settings": {
    "mode": "balanced",
    "max_nonurgent_per_hour": 2,
    "cooldown_minutes": 30,
    "quiet_start": "22:00",
    "quiet_end": "08:00"
  }
}
```

Reset:

```json
{"op":"reset"}
```

## Native UI

Control Center → Настройки → Proactivity:

- mode;
- max non-urgent/hour;
- cooldown;
- quiet hours;
- save/reset;
- recent Event Engine decisions.

Журнал показывает и suppressed события, чтобы было понятно, почему NOZZA
промолчала.

## Safety invariants

1. Event Engine 1.0 не вызывает `Agent.run_skill`.
2. Explicit reminders не требуют Autopilot.
3. Inferred proactive delivery требует Autopilot.
4. Quiet hours не отменяют explicit reminders.
5. Event journal не хранит raw screenshots/audio.
6. Suppressed event не выполняет никаких side effects.
7. Future background actions должны проходить Autonomy + provider ceiling +
   canonical risk/confirmation.

## Следующий этап

После стабилизации notification-only Event Engine можно делать Event Engine 2.0:

- provider event adapters;
- PrintFlow changes;
- browser page watches;
- app/window events;
- optional background read-only tasks;
- только затем ограниченные background actions через Task Engine.
