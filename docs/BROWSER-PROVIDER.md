# Browser Provider: расширение и DevTools

Browser Provider даёт Luma структурированный read-only контекст Chromium через
локальное расширение либо Chrome DevTools Protocol (CDP). Это отдельный слой от Desktop
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

Расширение обращается только к `127.0.0.1:8799` и требует ключ подключения.
Его запросы принимаются только с `chrome-extension://` и только с этого
компьютера. Ключ показывается на локальной странице `/browser/setup` и
сохраняется в профиле расширения. Страницы сайтов не могут получить ключ или
выполнить запрос к мосту от имени расширения.

При использовании CDP Provider принимает endpoint только на loopback:

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

Browser Provider пока не умеет:

- кликать;
- вводить текст;
- submit форм;
- исполнять пользовательский JavaScript;
- читать cookies/localStorage;
- читать сетевые запросы;
- получать пароли.

Любые будущие browser.click/fill/submit должны быть отдельными write skills и
проходить обычный confirmation policy.

## Подключение расширения (Chrome, Edge, Яндекс Браузер)

1. Откройте страницу управления расширениями своего браузера и включите режим разработчика.
2. Установите распакованное расширение из папки `agent/browser_extension`.
3. Откройте `http://127.0.0.1:8799/browser/setup` при запущенной Luma.
4. Скопируйте ключ со страницы в окно расширения и выберите браузер.

После подключения навыки `browser.tabs`, `browser.page`, `browser.find` и
`browser.selection` работают через расширение. Если подключено несколько
браузеров, передайте параметр `browser` (`chrome`, `edge` или `yandex`) или
выберите вкладку по ID вида `edge:123` из `browser.tabs`.

Расширение читает только открытые вкладки и видимое содержимое обычных
`http`/`https` страниц. Клики, заполнение форм и отправка данных через этот
мост не реализованы.

## Подключение через DevTools

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
локальное расширение или 127.0.0.1 Chromium DevTools
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
