# Ручные кейсы FarmLoop / конвейер — 18.12.1

> 2605 тестов зелёные, поэтому критичные сценарии проверяются вручную.

## 1. Шкала слоёв (layers.js)

### bbox fallback
- **Кейс**: /api/gcode/layers возвращает `bbox: null` или `[0,0,0,0]` во время building.
- **Ожидание**: канва рисует рамку 256×256, сетка 50мм видна, нет деления на 0.
- **Проверка**: `let box = bbox&&len4?bbox:null; if !box||!every finite → [0,0,256,256]; if !(box2>box0)||!(box3>box1) → fallback`.

### Y-инверсия (front=0)
- **Кейс**: модель с Y=0 спереди стола.
- **Ожидание**: `Y = cssH - (oy + (y-box1)*scale)` — front-left = (0, plate_h) внизу кадра, совпадает с bed_projection.
- **Проверка**: сравнить с `test_bed_projection.py::test_plate_corners_order`.

### follow / strip
- **Кейс**: total=0, current=0, selected=0.
- **Ожидание**: strip не падает с Infinity: `cols = max(1, min(n, floor(cssW)||1))`, `per=n/cols`, `from/to` отдельно, `h=(s/max(1,k))/max*...`, `isPast = current>0`, `mark` guard `layer<=0 return`.
- **Проверка**: перетащить ползунок при `current=0` — highlight не появляется, полоска рисуется.

## 2. Конвейер (conveyor.js)

### dropzone
- Перетащить STL в зону — класс `dragover`, после drop класс снят, файл уходит в `/api/library/upload`.
- Клик по зоне / кнопка «Выбрать» — открывает `file_input`.
- 3MF/G-code → `/api/estimate/upload` → `handleGcodeReady`.

### AMS
- `parseAmsSlot`: 'A1'→1, 'AMS 1 slot 2'→2, '3'→3, 254→254, '16'→null.
- `filterUsableSpools`: archived=true и remaining<=0 отфильтрованы.
- `autoSelectSpool`: выбирает только из usable, по материалу lower-case.
- `ams_mapping`: `[slotNum]` где slotNum из parseAmsSlot, не строка 'A1'.

### /api/jobs/enqueue
- cycles=1 → одно задание, без события farmloop.
- cycles=5 + farmloop flag → 5 заданий одной транзакцией, событие `farmloop: Серия конвейера...`, `job_ids` в payload.
- cycles>100 → clamp 100, cycles<1 → 1.

## 3. Тестовая очистка стола (routes_farmloop)

- `FREE_STATES = (IDLE, FINISH)`.
- `_pick_free_printer`: перебирает пул, первый свободный, остальные — в `blocked` с причинами.
  - `«P1S» — не подключен`, `«P1S» — занят (статус: RUNNING)`.
- Без `printer_id` → auto_selected=true, берёт свободный, не первый в словаре.
- С `printer_id` занятого → 400 с причиной конкретного станка.
- Пустой пул → `Принтеров нет: добавьте станок...`.
- Все заняты → `Нет свободного станка: ...`.

## 4. Пустой стол

- `preflight_block_bed=true` + `inspect_bed_clear(frame, ref, threshold)`:
  - Нет `bed_reference.jpg` → `bed_no_ref` info, не блокирует.
  - Нет кадра → пропускаем, не блокируем (слепая ячейка).
  - ratio>threshold → block `bed_not_clear`.
- `bed_watch_enabled` + `bed_reference.jpg` + `bed_watch_threshold` (6.0):
  - FINISH → `watch_bed` считает diff, если >threshold → guard event, `_bed_cleared=False`.
- `watch_bed` FarmLoop приоритет:
  - `farmloop_auto_next=true`, sensor=camera/both, ratio<=camera_threshold (6.0) → сразу `cleared=True`, event farmloop, `part_removed` → следующий цикл.
  - Раньше dead-code: `if ratio>threshold` содержал `if farm_auto && ratio<=cam_thresh` при обоих 6.0 → unreachable. Фикс вынесен до threshold.

## 5. Разметка стола

- bed projection: front-left = (0, plate_h) внизу кадра, homography corners order.
- layers.js сетка 50мм: линии `Math.ceil(box0/50)*50` до box2, stroke `rgba(148,163,184,.18)`.

## 6. AMS склад

- `pick_warehouse_spool`: archived=0, remaining>0, case-insensitive material, приоритет in_ams + verified + remaining.
- `accounting.pick_spool`: парсит 'A1' через `_parse_ams_slot`, фильтрует remaining>0, archived=0, fallback exact match.
- `spool filter` в UI: `remaining_grams>0`.
- `cleanup_ams_phantoms`: дубли `printer_id+ams_slot` (cnt>1) → архивирует старые, фантомы с `material=''` или `remaining<=0` → архив.
- `auto-map`: `/api/printer/ams/auto-map` → `auto_ams_map(required, trays)` → mapping с -1 для неназначенных.

## 7. Ручной прогон (чеклист)

1. Загрузить STL через dropzone → Plan → Slice с галочкой FarmLoop → файл `*.farmloop.gcode` + событие в Истории.
2. Enqueue серия 3 → 3 задания в очереди, одно событие farmloop.
3. Тестовая очистка: 2 принтера (RUNNING, IDLE) без printer_id → выбирается IDLE, auto_selected true.
4. Тестовая очистка: выбрать RUNNING → 400 с причиной.
5. Пустой стол: удалить bed_reference.jpg → preflight не блокирует, info `bed_no_ref`.
6. Пустой стол: поставить bed_reference.jpg, threshold 6%, камера пустая (diff 2%) → cleared, автоследующий стартует.
7. Пустой стол: камера с деталью (diff 20%) → block, уведомление «Снимите деталь».
8. Слои: открыть печать без bbox → рамка 256, сетка, без Infinity.
9. AMS: завести катушку A1 с 0г → не появляется в списке конвейера, не подбирается в watch/enqueue.
10. AMS: слот 'A1' в базе → pick_spool по 'A1' находит её как 1.
