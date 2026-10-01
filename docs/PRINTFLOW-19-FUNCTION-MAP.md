# PrintFlow 19 — function map

> Цель: одна карта, по которой можно понять, **что есть в PrintFlow, где живёт источник истины и как локальный AI взаимодействует с каждой функцией**.
>
> Этот документ описывает целевую структуру PrintFlow 19 поверх реально существующих доменов. Он не создаёт второй набор сущностей и не заменяет API/схему БД.

## 1. Экранная карта

```mermaid
flowchart LR
    H[Человек] --> T[Сегодня]
    H --> O[Заказы]
    H --> P[Принтеры]
    H --> S[Склад]
    H --> C[Клиенты]
    H --> F[Финансы]
    H --> A[Аналитика]
    H --> X[Инструменты]
    H --> G[Настройки / Система]

    S --> S1[Товары]
    S --> S2[Стеллаж]
    S --> S3[Пластик]
    S --> S4[Партии]
    S --> S5[Склады]
    S --> S6[Операции]

    N[PrintFlow AI rail] -. контекст .-> T
    N -. контекст .-> O
    N -. контекст .-> P
    N -. контекст .-> S
    N -. контекст .-> C
    N -. контекст .-> F
    N -. контекст .-> A
    N -. контекст .-> X
    N -. контекст .-> G
```

### Первый уровень

| Раздел | Главный вопрос человека | Основные действия |
| --- | --- | --- |
| Сегодня | Что происходит и что мне делать следующим? | открыть проблему, открыть заказ/станок, обсудить с AI |
| Заказы | Где находится каждый заказ в жизненном цикле? | создать, подготовить, сменить этап, выдать, разобрать долг |
| Принтеры | Что делает парк и нужен ли оператор? | pause/resume/stop, камера, AMS, очередь |
| Склад | Что есть, чего не хватает и что нужно произвести? | товар, перемещение, партия, инвентаризация |
| Клиенты | Кто покупал, что обещали и кому нужно написать? | карточка клиента, история, aftercare, B2B |
| Финансы | Сколько денег есть, сколько должны и что надо оплатить? | платёж, проводка, дебиторка, налог, отчёт |
| Аналитика | Где теряются деньги/время и что изменилось? | drill-down к заказу, товару, принтеру |
| Инструменты | Что нужно напечатать/сгенерировать для работы? | ценники, этикетки, формы, QR, материалы |
| Настройки / Система | Всё ли подключено и правильно настроено? | устройства, интеграции, backup, update, diagnostics |

## 2. Главный бизнес-поток

```mermaid
flowchart LR
    I[Заявка / intake] --> O[Order]
    O --> Q[Оценка / цена]
    Q --> PAY[Оплата / долг]
    O --> R[Резерв готового]
    O --> PREP[Подготовка производства]

    PREP --> J[Print Job]
    PREP --> B[Batch]
    B --> J
    J --> PR[Printer]
    PR --> QC[Результат / QC]

    QC -->|готовая продукция| WH[Stock]
    QC -->|под заказ| FUL[Выдача]
    WH -->|розничная выкладка| SH[Shelf retail zone]
    SH --> SALE[Продажа]
    WH --> FUL

    PAY --> FIN[Finance ledger]
    SALE --> FIN
    FUL --> FIN
    FUL --> AC[Aftercare]
    AC --> CL[Client history]
```

Правило v19: экран может показывать удобное представление этого потока, но **не создаёт новую бизнес-сущность**, если нужная уже существует.

## 3. Источники истины

```mermaid
flowchart TB
    NOM[nomenclature] --> VAR[nom_variants]
    NOM --> SPEC[specs / spec_items]
    NOM --> PRICE[prices]
    NOM --> OI[order_items]
    NOM --> SM[stock_moves]
    NOM --> B[batches]
    NOM --> SI[shelf_items]

    ORD[orders] --> OI
    ORD --> PJ[print_jobs]
    ORD --> PAY[payments]
    ORD --> TX[transactions]
    ORD --> DEF[defects]
    ORD --> PHOTO[order_photos]

    B --> PJ
    PJ --> USE[filament_usage / cost]
    PJ --> DEF

    DOC[documents] --> SM
    WH[warehouses] --> SM
    SI --> SLM[shelf_moves]
    SI --> SHZONE[warehouse kind=shelf]
    SLM -. синхронная операция .-> SHZONE

    PAY --> TX
    ACC[accounts] --> TX
```

### Канонические сущности

- **Товар:** `nomenclature`.
- **Вариация:** `nom_variants`.
- **Цена:** `prices`; старый `catalog.price` — совместимое зеркало.
- **Состав заказа:** `order_items`.
- **Заказ:** `orders`.
- **Задание печати:** `print_jobs`.
- **Серийное производство:** `batches`, связанное с `print_jobs.batch_id`.
- **Остаток обычного склада:** сумма `stock_moves`.
- **Стеллаж:** `shelf_items + shelf_moves`, синхронизированный с отдельной warehouse-zone `kind='shelf'`.
- **Оплата:** `payments`.
- **Финансовый факт:** `transactions`.
- **Клиент:** `customers`.

### Legacy, который нельзя снова делать источником

- `catalog` — зеркало/совместимость вокруг `nomenclature`.
- `orders.product` — краткое отображение; состав заказа живёт в `order_items`.
- `shelf_items.qty` — быстрый снимок retail-регистра; изменяется только журналируемой операцией.
- `orders.paid/prepaid` — совместимость; факт оплаты живёт в `payments`.

## 4. Склад и Стеллаж

Стеллаж **не является ещё одним названием общего склада**.

```mermaid
flowchart LR
    HOME[Обычный склад] -->|transfer| RETAIL[Стеллаж / retail zone]
    RETAIL -->|transfer back| HOME
    RETAIL -->|sale| CUSTOMER[Покупатель]
    RETAIL -->|writeoff| LOSS[Списание]
    RETAIL -->|inventory| FACT[Фактический пересчёт]

    HOME -. stock_moves .-> LEDGER[Единый складской регистр]
    RETAIL -. shelf_moves + shelf warehouse .-> LEDGER
```

Инварианты:

1. Остаток существующей позиции Стеллажа нельзя править карточкой.
2. Любое изменение количества имеет движение.
3. Со склада на Стеллаж и обратно — атомарные переносы.
4. Архивирование не уничтожает историю и запрещено при ненулевом остатке.
5. Продажи считаются по фактической цене строки продажи, а не по сегодняшней цене карточки.
6. Резервированный остаток нельзя незаметно унести со Стеллажа.

## 5. Производство

```mermaid
flowchart LR
    MODEL[Файл / модель] --> SLICE[Slice / preflight]
    SLICE --> JOB[print_jobs]
    PLAN[План пополнения] --> BATCH[batches]
    BATCH --> JOB
    ORDER[order_items] --> PREP[Production preparation]
    PREP --> JOB
    JOB --> AMS[Материал / spool mapping]
    JOB --> PRINTER[Printer]
    PRINTER --> TELE[Telemetry / HMS / camera]
    PRINTER --> RESULT[Done / failed]
    RESULT --> COST[Фактическая себестоимость]
    RESULT --> STOCK[Приход готового]
    RESULT --> DEFECT[Defect recovery]
```

Первый уровень UI: текущая печать, прогресс, оставшееся время, проблема и безопасное действие. Камера, AMS, файлы, ТО и журнал — детали выбранного принтера.

## 6. Финансы

```mermaid
flowchart TB
    PAYMENT[payments] --> TX[transactions]
    ORDER[orders] --> DEBT[receivables / debts]
    TX --> ACC[accounts / balances]
    TX --> PNL[P&L]
    TX --> TAX[tax report]
    JOB[print_jobs actual cost] --> PNL
    STOCK[stock value] --> BI[Business insights]
    DEBT --> BI
    TAX --> BI
    ACC --> BI
```

Первый уровень финансов v19:

- **Доступно сейчас** — счета/кассы.
- **К получению** — дебиторка.
- **Просрочено** — дебиторка старше порога.
- **К уплате** — налоги/взносы.
- **Результат бизнеса** — прибыль и маржа.
- **Требует решения** — финансовые исключения.

Банк, СБП, кассы, импорт, налоговый реестр и закрытие месяца остаются источниками/операциями внутри Финансов, а не отдельными концепциями верхнего уровня.

## 7. Клиенты

```mermaid
flowchart LR
    C[Customer] --> O[Orders]
    C --> CONV[Conversations]
    C --> MEM[Business notes]
    O --> AFTER[Aftercare]
    C --> RFM[RFM / repeat]
    C --> B2B[B2B]
    CONV --> O
    AFTER --> C
```

Карточка клиента должна собирать историю, а не дублировать заказ: покупки, долги, разговоры, обратную связь и поводы вернуться.

## 8. Luma → PrintFlow AI

PrintFlow AI — **доменный интерфейс внутри панели**. Оркестрация остаётся у Luma.

```mermaid
sequenceDiagram
    participant U as Human
    participant UI as PrintFlow screen
    participant L as Luma
    participant N as PrintFlow AI domain
    participant API as PrintFlow API / DB

    UI->>N: view + selected entity + filters
    U->>L: natural language request
    L->>N: ask/action through loopback
    N->>API: read authoritative facts
    API-->>N: facts
    N-->>L: allowed action contract
    L-->>U: plan / confirmation if required
    U->>L: confirm
    L->>N: execute catalog action
    N->>API: mutation
    API-->>N: result
    N->>API: deterministic readback
    N-->>L: verified / pending / failed
    L-->>U: outcome with evidence
```

### Action contract v19

Каждое доступное Luma действие PrintFlow содержит:

- `domain`
- `risk`
- `reversible`
- `verification`
- `confirm`
- server-owned `method/path/params`

Luma не строит произвольный PrintFlow URL. Разрешён только action из server catalog.

### Текущие домены action catalog

- `production`
- `orders`
- `customers`
- `finance`
- `analytics`
- `inventory`
- `retail`
- `system`
- `global`

## 9. Контекст AI

UI-context эфемерный и не является памятью:

```
view
sub
entity_type
entity_id
filters
dirty
updated_at
```

Примеры:

- экран Printers + `printer:p1s` → «поставь его на паузу»;
- экран Order + `order:ord_123` → «выдай его в долг»;
- экран Shelf + `shelf_item:shf_42` → «верни две штуки на склад».

При смене экрана выбранная сущность сбрасывается. Контекст не пишется в SQLite memory.

## 10. Что должно остаться вторичным

Эти функции важны, но не должны конкурировать с ежедневной работой в основной навигации:

- банковский импорт;
- отдельная страница СБП;
- мобильная касса;
- ценники / labels / marketing generators;
- TV / wall / kiosk;
- диагностические журналы;
- техническая warehouse turnover table;
- AMS engineering controls;
- обновления / backup;
- экспериментальные niche tools.

Они доступны из соответствующего домена или «Инструменты / Настройки».

## 11. Запрещённые архитектурные возвраты

При развитии v19 не делать:

1. второй каталог товаров рядом с `nomenclature`;
2. второй остаток рядом с журналом движений;
3. ручное изменение stock quantity без документа/операции;
4. отдельный AI brain для PrintFlow, конкурирующий с Luma;
5. action AI, который обходит server catalog;
6. «успешно» после физической команды без readback/verified state;
7. отдельные верхнеуровневые экраны для каждого способа оплаты;
8. удаление исторических бизнес-сущностей там, где нужен архив.

## 12. Приоритет завершения PrintFlow 19

```mermaid
flowchart LR
    P0[Data integrity] --> AI[Action contracts]
    AI --> CTX[Screen context]
    CTX --> VERIFY[Action verification]
    VERIFY --> SHELL[Independent shell]
    SHELL --> TODAY[Today]
    TODAY --> PRN[Printers]
    PRN --> FIN[Finance]
    FIN --> ORD[Orders]
    ORD --> STOCK[Stock + Shelf]
    STOCK --> CRM[Clients]
    CRM --> ANALYTICS[Analytics]
    ANALYTICS --> TOOLS[Tools / Settings]
    TOOLS --> CLEANUP[Remove / merge legacy UI]
```

На каждом шаге: сохранить существующий backend contract, добавить regression test, только затем убирать legacy UI.
