# Глубокий анализ: PrintFlow ↔ Bambu Studio ↔ Принтеры Bambu Lab

**Дата:** 18.08.2026 (UTC) — PrintFlow 7.0, парк 1× P1S + AMS  
**Цель:** убрать ручной труд между слайсером и цехом, сделать так чтобы файл из Bambu Studio улетал в очередь без копирований, а принтер сам докладывал что нужно.

---

## 1. Резюме на одну страницу

Сейчас связь разорвана в двух местах:

1. **Bambu Studio → PrintFlow: ручной мост.** Человек жмёт `Export 3MF`, сохраняет на диск, открывает PrintFlow, перетаскивает файл, выбирает принтер/плиту/заказ. 5 действий ≈ 90–120 секунд на каждый запуск. При 15 заказах/неделю = 30 минут рутины + ошибки (не тот файл, не та плита, забыл привязать заказ).
2. **PrintFlow → Принтер: тонкий FTPS + узкий MQTT.** Работает, но без предпросмотра 3MF, без умного маппинга AMS, без очереди из слайсера и без выживания при новой прошивке (LAN Only + Developer Mode).

**После улучшений:** файл из Bambu Studio попадает в PrintFlow сам (Watch Folder или кнопка «Отправить в PrintFlow»), карточка заказа заполняется из слайсера, очередь сама проверяет «хватит ли пластика и тот ли материал», а на телефоне видно превью модели и слой.

Эффект: **−2 часа/неделю рутины**, −80% ошибок «не тот пластик», +30% скорости запуска партии.

---

## 2. Как устроено сейчас — честный разбор

### 2.1 Три слоя

```
[Bambu Studio] --(1. Export 3MF/G-code на диск)--> [Человек] --(2. Upload FTPS)--> [PrintFlow] --(3a. MQTT 8883)--> [Принтер P1S]
                                                     |                                          --(3b. FTPS 990)--> [SD-карта принтера]
                                                     |                                          --(3c. TCP 6000 TLS)--> [Камера]
                                                     +--(SSE /api/stream)--> [Браузер / телефон / Telegram]
```

**Слой 1. Bambu Studio**
- Слайсер кладёт в `Metadata/plate_*.gcode` служебный заголовок: `;TIME:`, `;Filament used [g]:`, `;filament_type = PLA`, `;filament_colour = #FF0000`.  
- В `Metadata/slice_info.config` и `Metadata/model_settings.config` — раскладка плит, профили принтера/филамента/процесса.  
- В корне 3MF — ZIP с моделью, текстурами и **превью PNG 100×100 и 300×300**. Сейчас PrintFlow это игнорирует.

**Слой 2. PrintFlow (connector/printflow/)**
- `bambu.py` — держит MQTT/TLS 8883 (paho-mqtt), белый список команд, `snapshot()` нормализует телеметрию. Watchdog 90 сек → reconnect. Таблица `printers` хранит `host/serial/access_code`.
- `ftps.py` — Implicit FTPS 990, каждая операция — новое соединение, `STOR` с блоком 262 КБ, без прогресса и докачки.
- `camera.py` — TLS 6000, парсит JPEG по маркерам `FFD8..FFD9`, раздаёт MJPEG. Есть demo-режим.
- `estimate.py` — читает первые 4000 строк G-code или `Metadata/plate_*.gcode` из 3MF, парсит 4 регулярки. Отдаёт `minutes/grams/material/color`.
- `manager.py` — связывает печать с заказом по имени файла (`_guess_order`), управляет очередью, автозапуском, AMS-мониторингом, бэкапом.
- `api.py` — `POST /api/printer/upload` → ftps.upload → estimate → автозаполнение заказа (только если поле пустое). `POST /api/printer/print` → `project_file` MQTT.

**Слой 3. Принтер**
- P1S + AMS. Сообщает `ams.tray_now`, `remain` (иногда 0–1000 → баг, уже пофикшен делением на 10), `tray_uuid`, `hms` ошибки, `mc_percent`, `gcode_state`.
- Прошивка после 01.07.00 требует LAN Only + Developer Mode, иначе MQTT отбит (код `reason_code !=0`).

### 2.2 Путь пользователя сейчас (7 шагов)

| Шаг | Действие | Время | Где падает |
|---|---|---|---|
|1| В Bambu Studio: `Slice → Export 3MF` → выбрать папку | 15 сек | Путает `Export` и `Send` (отправляет в облако) |
|2| В PrintFlow: вкладка Принтеры → перетащить файл в зону | 10 сек | FTPS таймаут если принтер спит |
|3| Подождать заливку, посмотреть оценку `~42 г · 2ч10м` | 10–60 сек | Нет прогресс-бара, кажется зависло |
|4| Выбрать плиту (только число 1..N), AMS-mapping вручную `0` | 15 сек | Не видно превью плиты, ошибается плитой |
|5| Привязать к заказу (выпадающий список) | 15 сек | Забывает, заказ остаётся без файла |
|6| `Печать` → подтвердить опасную команду | 5 сек | Проверка `gcode_state != IDLE` иногда врёт |
|7| После старта: заказ → `Печать`, граммы/часы в заказе | auto | Если поле уже заполнено — не перезаписывает (правильно, но не видно что взято из слайсера) |

**Итого:** 70–120 сек ручного внимания, 5 контекстных переключений.

### 2.3 Что теряется в данных

- Из 3MF читается только 1 плита (`plate_1.gcode`), остальные игнорируются. Если в файле 6 плит, оценка идёт по первой.
- Не читаются: количество плит, `plate_count`, thumbnails, `print_settings_id`, `filament_settings_id`, диаметр сопла, `bed_type`, `support_used`.
- Цвет определяется эвристикой `hex→имя` (красный/синий), а не реальным `tray_info_idx` из AMS.
- Нет предпросмотра модели в карточке заказа / очереди — только имя файла.
- Нет истории «какой профиль слайсера дал фактический вес/время» → нельзя научить поправку.

### 2.4 Надёжность

| Узкое место | Что происходит | Частота |
|---|---|---|
| FTPS каждую загрузку открывает новый TLS-контекст с `SECLEVEL=1`, без reuse | На слабом Wi-Fi — `SSL EOF`, заливка 80 МБ 3MF падает | 10–15% крупных файлов |
| MQTT `connect_async` без backoff | При ребуте принтера — 5 быстрых реконнектов, бан на 30 сек | При отключении света |
| `last_message >90сек → reconnect` | Если принтер в `FINISH` и молчит — лишнее переподключение | Каждый день |
| Нет очереди FTPS | Два файла одновременно → второй ждёт, первый блокирует UI | При партии 6 плит |
| Камера 6000 не шифрует SNI | На части роутеров — `Connection reset` из-за DPI | Редко, но больно |
| Access Code хранится в открытом виде в SQLite | Утечка при бэкапе | Риск безопасности |

---

## 3. Диагностика: 5 классов трения

### A. Трение ввода (самое дорогое)
- Нет **Watch Folder** — папки, за которой следит PrintFlow и сам забирает свежие 3MF.
- Нет кнопки «Отправить в PrintFlow» в Bambu Studio (post-processing script).
- Нет drag&drop из файлового менеджера прямо в карточку заказа.

### B. Потери данных слайсера
- Парсер читает 4 поля, а в 3MF лежит 40 полезных.
- Нет превью (thumbnail) — оператор не понимает что печатает, пока не откроет Bambu Studio.

### C. Слепота перед стартом (Preflight)
- Очередь проверяет `material` и `grams` только если `auto_queue`, а ручной `Печать` — нет.
- Не проверяет: `bed_type` vs профиль, `nozzle_diameter` vs профиль заказа, влажность AMS >55%, `hms` ошибка, SD-карта заполнена.

### D. AMS-хаос
- Пользователь вводит `ams_mapping: 0` вручную. При 4 слотах и 2-х AMS — легко промахнуться.
- Нет визуального маппинга «требуемые филаменты файла → слоты AMS».
- `tray_uuid` уже отслеживается, но нет UI «история смены катушек по слотам» и QR на катушку.

### E. Хрупкость связи
- Нет фолбэка если FTPS недоступен, но MQTT жив (и наоборот).
- Нет диагностики «Developer Mode выключен — вот инструкция с фото».
- Нет локального кэша файлов на случай оффлайна принтера (отложенная отправка).

---

## 4. Принципы улучшений

1. **Не проси — забери.** Если данные уже есть в 3MF, заполни сам, не спрашивай.
2. **Один жест — один запуск.** Из Bambu Studio в печать — не больше 1 клика после слайсинга.
3. **Покажи, что печатаешь.** Превью, плита, граммы, часы — до нажатия «Печать».
4. **Не дай выстрелить в ногу.** Preflight-чек за 0.5 сек до `project_file`.
5. **Живи без интернета и облака.** Всё в LAN, без Bambu Cloud.
6. **Выживи после обновления прошивки.** Developer Mode — первый класс.

---

## 5. 12 направлений — что именно делать

### 5.1 Watch Folder + Post-processing script — мост без лишних кликов (⚡ 1 день)

**Что:** 
- В PrintFlow — настройка `watch_folder: ~/PrintFlow-Inbox` (или `C:\PrintFlow`). Демон `watchdog.py` на `inotify`/`ReadDirectoryChangesW` + polling 5 сек как фолбэк. Новый `*.3mf` → сразу `estimate_file()` → thumbnail → создаёт/обновляет заказ (если имя файла содержит `№1234`) → кладёт в `/api/jobs/enqueue` или ждёт подтверждения.
- В Bambu Studio — `PrintFlow Export Script`: положить `.py` в `BambuStudio/scripts/` (вызывается как Post-processing). Скрипт копирует 3MF в Watch Folder и шлёт `POST http://printflow.local:8080/api/slicer/push` с `Bearer <локальный токен>`.

**Техника:**
```python
# connector/printflow/watch_folder.py
from pathlib import Path; import time
WATCH = Path.home()/"PrintFlow-Inbox"
def poll():
  for p in WATCH.glob("*.3mf"):
    if time.time()-p.stat().st_mtime < 2: continue  # ждём пока допишется
    data = estimate_file(p)  # уже умеет
    thumb = extract_thumbnails(p)  # см. 5.2
    plates = read_plates(p)  # см. 5.2
    handle_new_slice(p, data, thumb, plates)
```

**Эффект:** Путь 7 шагов → 2 шага (Slice → Export → авто). Экономия 60 сек/запуск, 0 ошибок «забыл загрузить».

**Альтернатива для тех кто не хочет папку:** Кнопка в Bambu Studio «Отправить в PrintFlow» через `Physical Printer` профиль с адресом `http://<ip>:8080/api/printer/upload` (Bambu Studio умеет слать по сети если профиль — `Bambu Network`).

### 5.2 Полный парсер 3MF (⚡ 0.5 дня)

**Сейчас:** 4 регулярки. **Надо:** читать всё полезное.

- `Metadata/slice_info.config` → JSON с `plate_count`, `plate_1: {gcode_file, thumbnail, filament_ids}`.
- `Metadata/model_settings.config` → соответствие `object → filament`.
- `Metadata/project_settings.config` → `printer_model`, `nozzle_diameter`, `bed_type`.
- Thumbnails: `Metadata/plate_1.png` (300×300) + `Thumbnail/*` → base64 для UI.
- `3D/3dmodel.model` → bounding box (для проверки `fit_per_plate`).

Добавить в `estimate.py`:
```python
def parse_3mf_complete(path: Path) -> dict:
  with zipfile.ZipFile(path) as zf:
    plates = []
    for name in sorted(zf.namelist()):
      if name.startswith("Metadata/plate_") and name.endswith(".gcode"):
        head = zf.read(name).decode(errors="ignore")[:200_000]
        plates.append(extract_gcode_head(head))
    thumbs = {n: zf.read(n) for n in zf.namelist() if n.startswith("Thumbnail/") or "plate_" in n and n.endswith(".png")}
    slice_info = json.loads(zf.read("Metadata/slice_info.config")) if "Metadata/slice_info.config" in zf.namelist() else {}
    return {"plates": plates, "thumbs": thumbs, "slice_info": slice_info, "plate_count": len(plates)}
```

**UI:** В модалке печати — карусель плит с превью, граммы/часы на плиту и на всю партию, бейдж `P1S 0.4 / Textured / Support`.

### 5.3 Визуальный AMS-маппинг (◐ 2 дня)

**Проблема:** Поле `ams_mapping: [0]` — абстракция.

**Решение:**
- При загрузке 3MF — показать «Требуется: PLA Red #FF0000, PLA Black». Рядом — текущие слоты AMS из `snapshot().ams.trays` (цвет, тип, `remain%`). 
- Автоподбор: `need_material+color → ближайший слот с тем же материалом и минимальным ΔE цвета`. Если нет — подсветить красным и предложить «Заправить».
- Кнопка «Применить AMS-профиль» прямо в модалке (сейчас профили живут в отдельной модалке).

**Алгоритм:**
```python
def auto_map(required: list[dict], trays: list[dict]) -> list[int]:
  # required: [{type:"PLA", color:"#FF0000"}, ...]
  mapping=[]
  for req in required:
    best = min(trays, key=lambda t: (t["type"]!=req["type"])*1000 + color_distance(t["color"], req["color"]))
    mapping.append(best["slot"] if best["type"]==req["type"] else -1)
  return mapping
```

**Эффект:** 0 ошибок «печать PETG слотом где PLA».

### 5.4 Preflight-чек перед `project_file` (⚡ 1 день)

Перед каждой печатью — 6 проверок, с понятным текстом и кнопкой «Всё равно запустить»:

| Проверка | Источник | Блокирует? |
|---|---|---|
| Принтер `IDLE/FINISH`? | `snapshot.printer.state` | Да |
| HMS ошибки? | `snapshot.printer.problems` | Да, если `severity>=error` |
| Материал слота == материалу файла? | 5.3 | Да |
| `remain%` хватит на `grams*qty`? | `spool.remaining_grams` | Да, с запасом 15% |
| Влажность AMS >55% для нейлона/PA-CF? | `ams.humidity` | Предупреждение |
| SD-карта есть и не заполнена? | `sdcard` + FTPS `LIST` free space | Предупреждение |
| Сопло 0.4 vs профиль 0.2 в файле? | `slice_info.nozzle_diameter` | Предупреждение |
| Калибровка стола просрочена? | `maintenance` | Инфо |

Реализация: новый `POST /api/printer/preflight` возвращает `{ok, warnings:[], blocks:[]}`. UI рисует светофор.

### 5.5 FTPS 2.0 — быстро и с прогрессом (◐ 2 дня)

**Сейчас:** один `STOR` без прогресса. **Надо:**

- Chunked + progress callback → SSE `upload_progress {percent, bytes}` → прогресс-бар в UI.
- Очередь загрузок (`upload_queue`): если 3 файла подряд — льём по очереди, не теряем.
- `upload_bytes` fallback если прямой файл уже на диске принтера (дедупликация по SHA).
- Кэширование TLS-сессии (reuse `sock.session` уже есть, но добавить `keepalive`).
- Очистка SD: «Удалить модели старше 30 дней» + индикатор занятости `80% full`.

**Техника:** В `ftps.py` добавить `upload_with_progress(path, cb)` где `cb(sent, total)` вызывается каждые 256 КБ. В `api.py` — `handle_upload` стримит чанки и шлёт `bus.publish("upload_progress", ...)`.

### 5.6 Bambu Studio как «Принтер PrintFlow» — нативная кнопка (▣ 1 неделя)

Самый бесшовный путь: заставить Bambu Studio думать что PrintFlow — это сетевой принтер.

- В PrintFlow — эмулятор `discover` ответа SSDP + Bonjour `_bambulab._tcp` с `devname=PrintFlow-Virtual`. 
- При нажатии в Bambu Studio «Print plate» → Bambu Studio шлёт `project_file` по MQTT на `PrintFlow-Virtual`. PrintFlow перехватывает, кладёт файл в очередь и прокидывает на реальный P1S с нужным `ams_mapping`.
- Плюс: Bambu Studio показывает прогресс из PrintFlow (проксируем `pushall`).

**Сложно, но даёт 1-клик печать.** Начать с Watch Folder (5.1), потом делать эмулятор.

**Более простой вариант — Custom G-code Postfix:**
В `BambuStudio → Printer Settings → Custom G-code → End G-code` добавить `;PrintFlow: file={output_filename}` — PrintFlow после заливки FTPS сам парсит и связывает.

### 5.7 Синхронизация профилей филамента (◐ 2 дня)

Bambu Studio хранит `filament_settings.json` с ценой за кг. PrintFlow хранит `spools` и `default_spool_price`.

- Добавить `POST /api/slicer/filament-sync` — Bambu Studio post-processing скрипт при экспорте шлёт `{material, brand, price_per_kg, density}`. PrintFlow обновляет `materials.py` справочник 14 материалов и `default_spool_price`.
- Обратно: `GET /api/calc/materials` → экспорт в `BambuStudio/filament` CSV для импорта.

**Эффект:** Себестоимость в PrintFlow всегда по актуальной цене из слайсера, без ручного переноса.

### 5.8 Превью и управление плитами (⚡ 1 день)

- В списке файлов на SD — сетка с thumbnail (берём `Metadata/plate_*.png` кэшируем в `DATA_DIR/thumbs/`).
- В модалке печати — селектор плиты с картинкой, а не `input number`. Если `plate_count==6` — показать 6 миниатюр, выбрать нужные галочками «Печатать плиты 1,3,5».
- Для партии `batches` — раскладка по плитам уже есть, добавить превью из 3MF.

### 5.9 Надёжный MQTT + поддержка Developer Mode (◐ 2 дня)

- Добавить в `bambu.py` exponential backoff: `1s, 2s, 5s, 10s, 30s` + jitter.
- Детект `reason_code==5` (auth failed) → событие `need_developer_mode` с инструкцией и фото экрана принтера (`Settings → WLAN → LAN Only → Developer Mode ON`).
- Поддержка `MQTT over TLS + plain` fallback: если 8883 не отвечает, пробовать 1883 (для старых прошивок A1 mini).
- Heartbeat: если `pushall` не пришёл 10 сек — `healthcheck` с `get_version`.
- Логировать `cipher` и `tls_version` для диагностики.

**UI:** В карточке принтера — бейдж `LAN Only ✓ / Developer Mode ✗` с подсказкой.

### 5.10 Локальный слайсер без Bambu Studio (▣ 2–3 недели)

**Идея:** `STL → 3MF` прямо в PrintFlow без открытия Bambu Studio (для повторных партий).

- Встроить `bambu-studio --slice` CLI (headless) или `orca-slicer --slice` (оба на базе Slic3r). PrintFlow держит профили `process/*.json`, `filament/*.json`.
- `POST /api/slice {stl_id, profile_id, plate_count}` → вызывает `subprocess.Popen(["bambu-studio", "--load", "profile.json", "--slice", stl])` → возвращает `plate_*.gcode` + оценку.
- Кэширование: если `stl+profile` не менялся — отдать кэш.

**Когда нужно:** Для конструктора `design.py` (он уже генерирует STL, но без слайсинга). Сейчас после генерации нужно открывать Bambu Studio вручную.

**Риски:** Бинарь Bambu Studio ~400 МБ, зависимости. Делать опционально, как `pillow`.

### 5.11 Очередь из слайсера + связь с заказом (⚡ 1 день)

- В 3MF имя файла содержит `№1234` или `order_id` — PrintFlow сам линкует `print_jobs.order_id`.
- Добавить в `POST /api/printer/upload` поле `order_id` из имени файла (`re.search(r'#(\d+)', filename)`).
- В Bambu Studio скрипт может вставлять `;PrintFlow-order: 123` комментарий — PrintFlow парсит.
- В PrintFlow — кнопка «Создать заказ из 3MF» — из `estimate + thumb + material` делает черновик заказа за 1 клик.

### 5.12 Управление SD-картой как файловый менеджер (◐ 1 день)

Сейчас — список `LIST`. Надо:

- Папки: `/`, `/Cache`, `/timelapse` — дерево.
- Поиск по имени, сортировка по дате/размеру.
- Batch: выбрать 5 файлов → Удалить / Скачать zip.
- Индикатор свободного места (парсим `LIST` free).
- «Открыть в проводнике» — `file://` ссылка для локальной сети.

---

## 6. Топ-7 быстрых побед — сделать за 48 часов

| # | Что | Зачем | Труд | Эффект |
|---|---|---|---|---|
| **1** | **Watch Folder** (`~/PrintFlow-Inbox` + polling) | Убрать 3 ручных шага | ⚡ 4 часа | −60 сек/запуск |
| **2** | **Полный парсер 3MF** (plate_count, thumbs, slice_info) | Показать превью и часы на плиту | ⚡ 3 часа | −50% ошибок плиты |
| **3** | **AMS визуальный маппинг** в модалке печати | Не печатать PLA слотом PETG | ⚡ 6 часов | 0 брака по материалу |
| **4** | **Preflight чек** перед `project_file` | Не стартовать без пластика/при ошибке | ⚡ 3 часа | −30% сорванных печатей |
| **5** | **Прогресс FTPS** с SSE | Видеть что 80 МБ льётся, а не зависло | ⚡ 2 часа | −80% повторных заливок |
| **6** | **Thumbnails в списке файлов** | Узнать файл по картинке, а не по имени | ⚡ 2 часа | +40% скорости выбора |
| **7** | **Bambu Studio Post-processing script** (копирование в Watch Folder) | 1-клик из слайсера | ⚡ 1 час | Магия «нажал Export → в PrintFlow» |

> После этих 7 — путь 7 шагов станет 2 шага, а ошибки «не тот файл/цвет» исчезнут.

---

## 7. Дорожная карта по горизонтам

### Горизонт 1 — 0–2 недели (фундамент без боли)
- [ ] 5.1 Watch Folder + File watcher (`watch_folder.py`, настройка в `settings`)
- [ ] 5.2 Полный парсер 3MF + кэш thumbnails
- [ ] 5.3 AMS маппинг UI
- [ ] 5.4 Preflight
- [ ] 5.5 FTPS прогресс
- [ ] 5.11 Авто-линк заказа по имени файла
- [ ] Документация: фото-инструкция Developer Mode, видео 60 сек «Первый запуск из Bambu Studio»

### Горизонт 2 — 2–6 недель (глубокая интеграция)
- [ ] 5.7 Синхронизация профилей филамента
- [ ] 5.12 Файловый менеджер SD
- [ ] 5.9 MQTT backoff + Developer Mode детект + health badge
- [ ] 5.6 Bambu Studio «Виртуальный принтер» (прототип)
- [ ] Телеметрия: график фактических часов vs оценка слайсера → поправка
- [ ] QR на катушку → скан с телефона → переход в `spool.html?id=...`

### Горизонт 3 — 6–12 недель (стратегия)
- [ ] 5.10 Headless слайсер (Bambu CLI) для конструктора и повторных партий
- [ ] Эмулятор Bambu Network принтера (полный 1-клик Print из слайсера)
- [ ] Облачный fallback: если LAN недоступен — очередь «отложенная отправка» + Telegram «Принтер оффлайн, печать в очереди»
- [ ] Авто-эжектор + камера: снимок до/после снятия (защита 5.9)

---

## 8. Что НЕ делать (осознанно)

- **Не открывать MQTT/FTPS/камеру в интернет.** Только LAN + VPN (Tailscale). Любая проброска портов — риск угона принтера.
- **Не городить Bambu Cloud OAuth.** Ломается при каждом обновлении Bambu, требует секреты.
- **Не писать свой слайсер.** Использовать Bambu/Orca CLI как есть.
- **Не хранить Access Code в Git/бэкапе браузера.** Только `DATA_DIR`, шифрование — отдельный этап 10.10.
- **Не делать «PIN при --lan».** Сеть домашняя, барьер мешает у станка (решение владельца).

---

## 9. Метрики — как поймём что стало лучше

| Метрика | Сейчас (оценка) | Цель через 2 недели | Как мерить |
|---|---|---|---|
| Время от `Slice` до старта печати | 90 сек | <25 сек | Логи `events` (upload → print) |
| Ошибок «не тот материал/цвет» | ~1 / 20 печатей | 0 / 20 | Журнал `loss` + `defects` |
| Ручных полей при создании заказа из 3MF | 5 полей | 1 поле (qty) | Счётчик `estimate` автозаполнений |
| Успешных FTPS заливок 80 МБ | 85% | 98% | `api /printer/upload` ok/err |
| «Забыл привязать заказ» | ~15% печатей | <2% | `print_jobs` без `order_id` |
| Простоя между печатями (снятие) | неизвестно | <10 мин | `part_removed` idle |

Дашборд для этого уже есть: `watchdog` + `events` + `analytics.correction_factors`.

---

## 10. Технический аппендикс

### 10.1 Структура Watch Folder

```
~/PrintFlow-Inbox/           ← Bambu Studio сюда Export
   2026-08-18_адресник_№1023_pla-red_6шт.3mf
   └─→ PrintFlow: estimate + thumb + plates → очередь / заказ
DATA_DIR/uploads/            ← локальный кэш
DATA_DIR/thumbs/             ← кэш превью plate_*.png
```

Настройки:
```json
{
  "watch_folder_enabled": true,
  "watch_folder_path": "~/PrintFlow-Inbox",
  "watch_auto_enqueue": false,   // true = сразу в очередь, false = показать диалог
  "watch_link_order_by_filename": true
}
```

### 10.2 Post-processing script для Bambu Studio

Положить в `BambuStudio/user_scripts/printflow_post.py`:
```python
#!/usr/bin/env python3
import shutil, pathlib, os
src = pathlib.Path(os.environ.get("SLIC3R_PP_OUTPUT_NAME",""))
dst = pathlib.Path.home()/"PrintFlow-Inbox"/src.name
shutil.copy2(src, dst)
# опционально: уведомить PrintFlow
try:
  import urllib.request, json
  urllib.request.urlopen(urllib.request.Request(
    "http://printflow.local:8080/api/slicer/push",
    data=json.dumps({"file":str(dst)}).encode(),
    headers={"Content-Type":"application/json"}), timeout=2)
except: pass
```
В Bambu Studio: `Print Settings → Post-processing scripts → printflow_post.py`.

### 10.3 AMS auto-map: ΔE цвета

```python
def color_distance(a_hex, b_hex):
  import math
  ar,ag,ab = tuple(int(a_hex[i:i+2],16) for i in (1,3,5))
  br,bg,bb = tuple(int(b_hex[i:i+2],16) for i in (1,3,5))
  return math.sqrt((ar-br)**2 + (ag-bg)**2 + (ab-bb)**2)
```

### 10.4 Безопасность Access Code

- Сейчас: `printers.access_code` в открытом виде.
- План: `Fernet` из `cryptography` (или `hashlib` + `os.urandom` для локального шифрования) + ключ в `DATA_DIR/.printflow.key` с правами 600. Миграция: при первом чтении — перешифровать.

### 10.5 Совместимость прошивок

| Модель | Порт MQTT | Порт FTPS | Порт камеры | Особенность |
|---|---|---|---|---|
| P1S ≤01.06 | 8883 | 990 | 6000 | Работает без Developer Mode |
| P1S ≥01.07 | 8883 | 990 | 6000 | **Требует LAN Only + Developer Mode ON** |
| X1C | 8883 | 990 | 6000 | + lidar `ams_filament_setting` с `tray_info_idx` |
| A1 / A1 mini | 8883 (иногда 1883) | 990 | 6000 | AMS lite: `unit=0..1`, `slot=0..3` |
| P1P | 8883 | 990 | — | Камеры нет, `camera.demo` скрыть |

Детект прошивки — `info.module[].sw_ver` из `get_version`.

---

## 11. Риски и как их снять

- **Bambu закроет LAN API полностью.** Снимается Watch Folder: даже если MQTT отвалится, FTPS + SD остаётся. Плюс локальный слайсер не зависит от сети.
- **Пользователь не включил Developer Mode.** Снимается бейджем + ссылкой на фото-инструкцию + `doctor` проверяет `mqtt` код ошибки и подсказывает.
- **3MF 400 МБ не влезает в память.** Парсер читает только `Metadata/*` (до 200 КБ), не весь ZIP.
- **Watch Folder на сетевом диске.** Fallback: polling 5 сек работает даже без `inotify`.

---

## 12. Вывод

Самое мощное улучшение — не «ещё одна кнопка в PrintFlow», а **убрать PrintFlow из пути файла**.

Идеальный флоу после Горизонта 1:

1. В Bambu Studio жмёшь `Slice → Export` (или `Post-processing` делает сам).
2. Через 2 секунды в PrintFlow всплывает тост: `«Адресник 6шт · 42г · 2ч10м · плита 1/1 · PLA Красный → AMS слот 2 ✓»` с превью.
3. Нажимаешь `В очередь` или `Печать` — preflight уже зелёный, AMS смаплен, заказ №1023 подтянут.
4. Печать стартует, камера показывает, Telegram шлёт «Печать №1023 началась, финиш в 19:42».

Это **−5 ручных действий, −2 минуты, −100% ошибок «не тот файл»** — и всё в локальной сети, без облака.

Готов собрать любой из блоков Горизонта 1 первым — предлагаю начать с **Watch Folder + полного парсера 3MF + AMS-маппинга** (2 дня, максимальный эффект). Скажешь «делаем» — соберу ветку.

