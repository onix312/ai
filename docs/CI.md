# Проверки и CI PrintFlow

Единая локальная команда:

```bash
python scripts/check.py
```

Она запускает Ruff (если установлен), компиляцию Python, `node --check` для
всего JavaScript, unit-тесты и `git diff --check`. Быстрый вариант без
unit-тестов:

```bash
python scripts/check.py --quick
```

Готовый шаблон GitHub Actions хранится в `docs/ci-workflow.yml`. Сейчас он не
активирован, потому что GitHub App среды разработки не имеет разрешения
`workflows`. После выдачи доступа файл можно вручную скопировать в
`.github/workflows/ci.yml`; тогда каждый push/PR будет выполнять три контура:

1. статические проверки и целостность на Python 3.12 + Node.js 22;
2. все unit-тесты на Python 3.10, 3.12 и 3.14;
3. дымовой запуск HTTP-сервера и основных маршрутов.

До активации обязательным источником результата остаётся локальная команда
`python scripts/check.py`. Dependabot еженедельно проверяет Python-пакеты
(`.github/dependabot.yml`).
