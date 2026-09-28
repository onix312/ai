# Browser Provider 1.0

Browser Provider даёт NOZZA структурированный read-only контекст Chromium через
локальный Chrome DevTools Protocol (CDP). Это отдельный слой от Desktop
Perception: браузер читается как документ и дерево элементов страницы, а не как
набор пикселей.

## Возможности

- список вкладок;
- URL и title;
- видимый текст текущей страницы;
- видимые ссылки;
- кнопки;
- структура форм;
- выделенный текст;
- поиск фразы по видимому тексту.

Живые навыки:

- `browser.tabs`
- `browser.page`
- `browser.find`
- `browser.selection`

Все четыре имеют риск `read`.

## Безопасность

Provider принимает DevTools endpoint только на loopback:

- `127.0.0.1`
- `localhost`
- `::1`

Удалённый CDP намеренно запрещён.

Модель и пользователь не могут передать Provider произвольный JavaScript.
Внутри находится один фиксированный inspection script.

Формы возвращают только метаданные полей: имя, label, тип, placeholder,
required/disabled и факт заполненности для несекретных полей. Значения полей не
возвращаются. Для password/card/one-time-code полей не возвращается даже
placeholder/fill state.

Browser Provider 1.0 не умеет:

- кликать;
- вводить текст;
- submit форм;
- исполнять пользовательский JavaScript;
- читать cookies/localStorage;
- читать сетевые запросы;
- получать пароли.

Любые будущие browser.click/fill/submit должны быть отдельными write skills и
проходить обычный confirmation policy.

## Подключение

По умолчанию NOZZA ищет локальный DevTools endpoint:

```text
http://127.0.0.1:9222
```

Адрес можно изменить:

```text
NOZZA_BROWSER_CDP_URL=http://127.0.0.1:9222
```

Для совместимости также принимается `PRINTFLOW_BROWSER_CDP_URL`.

Сам браузер должен быть запущен с поддерживаемым им локальным remote debugging
режимом. NOZZA не открывает произвольный remote-debugging endpoint сама.

Если endpoint не отвечает, capability `browser` становится недоступной с
понятной причиной, но остальной Assistant продолжает работать.

## Выбор текущей вкладки

Provider сравнивает title вкладок с foreground window Windows. Если вкладка
одна, она может быть выбрана без дополнительного контекста. Если вкладок
несколько и совпадение с foreground window не найдено, Provider отказывается
угадывать и просит вывести нужную вкладку на передний план или передать
`target_id`.

## Архитектура

```text
Brain / Planner
      ↓
browser.* skill
      ↓
Agent.run_skill
      ↓
Runner / Browser Provider
      ↓
127.0.0.1 Chromium DevTools
```

Browser Provider не является отдельным execution path и не обходит skills
registry.

## Следующие этапы

После стабилизации read-only слоя можно добавлять:

1. browser navigation / activate tab как soft capability;
2. browser.fill как write skill с confirmation;
3. page memory с provenance;
4. сравнение нескольких вкладок;
5. site profiles;
6. Browser Provider в общей provider architecture.
