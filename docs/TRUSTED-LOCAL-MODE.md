# Local Trusted Mode 1.0

NOZZA работает локально на компьютере владельца. Поэтому подтверждение не
должно появляться на каждом обратимом действии и превращать ассистента в
последовательность кнопок «Разрешить».

## Правило по умолчанию

Local Trusted Mode включён архитектурно по умолчанию:

- read — сразу;
- own — сразу;
- soft — сразу;
- write — сразу, если действие локальное и обратимое;
- system — сразу, если действие не относится к опасной группе;
- irreversible — только после подтверждения.

Risk остаётся частью canonical skill registry. Он нужен Autonomy, Planner,
журналу и UI, но больше не является автоматическим синонимом confirmation.

## Что всё ещё требует подтверждения

В v1 явная граница такая:

- `panel.do` — действие PrintFlow, потому что каталог панели может вести к
  физической печати или финансовому действию;
- `tg.post` — внешняя публикация от имени владельца;
- `system.power` — sleep/restart/shutdown и другие power actions;
- `system.install` — установка пакетов/моделей;
- `screen.archive_erase` — необратимое стирание архива;
- `assistant.macro_run` — legacy macro может содержать вложенный опасный шаг.

Явное `confirm=True` в registry остаётся жёстким override.

## Что теперь выполняется сразу

Например: `window.click`, `window.type`, `window.snap`, `window.close`,
`system.hotkey`, `system.autostart`, `system.watchdog_arm`,
`files.tidy_apply`, `list.clear`, `goal.remove`, `habit.remove`,
`voice.listen`, `voice.dictate`.

Они по-прежнему проходят canonical pipeline:

```text
Brain / Planner / Task
  -> Autonomy
  -> skill params / availability
  -> Local Trusted confirmation policy
  -> Runner / Provider
```

Trusted Mode убирает лишнее подтверждение, но не создаёт обход Autonomy,
параметров, capability checks или provider boundaries.

## Выученные навыки

Раньше любая выученная цепочка всегда спрашивала подтверждение.

Теперь безопасная цепочка запускается сразу, а если в ней есть опасный шаг,
подтверждение сохраняется. Поэтому «рабочий режим» может действительно быть
одной командой.

## Следующие шаги

После Local Trusted Mode основной roadmap:

1. Voice Engine 3.0
2. Native UI 2.0
3. Packaging / NOZZA.exe
4. постепенный Assistant Core refactor

Большой Security 2.0 с Guest Mode и сложной permission-системой не является
текущим приоритетом. Сохраняются дешёвые базовые защиты: loopback API, границы
файлов, секреты вне prompt и будущий STOP ALL.
