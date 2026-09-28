#!/usr/bin/env python3
"""Единая локальная проверка PrintFlow, совпадающая с CI.

Запуск из корня репозитория::

    python scripts/check.py          # полный набор, включая unit-тесты
    python scripts/check.py --quick  # синтаксис, lint и целостность файлов
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from html.parser import HTMLParser
from urllib.parse import urlsplit, parse_qs

ROOT = Path(__file__).resolve().parents[1]


def run(label: str, command: list[str], env: dict[str, str] | None = None) -> bool:
    print(f"\n==> {label}\n    {' '.join(command)}", flush=True)
    result = subprocess.run(command, cwd=ROOT, env=env)
    if result.returncode:
        print(f"FAIL: {label} (код {result.returncode})", file=sys.stderr)
        return False
    print(f"OK: {label}")
    return True


def check_data_icons() -> bool:
    """Б8: все data-icon="..." в HTML существуют в реестре PFIcons (icons.js)."""
    import re
    label = "data-icon в реестре PFIcons"
    print(f"\n==> {label}", flush=True)
    try:
        # Читаем реестр иконок из icons.js
        icons_js = (ROOT / "site" / "assets" / "icons.js").read_text(encoding="utf-8")
        # Ищем все ключи в объекте ICONS: key: '<svg...', без кавычек вокруг ключа
        registry = set(re.findall(r'^\s+([a-z0-9\-_]+)\s*:\s*\'', icons_js, re.MULTILINE))
        # Читаем все HTML-файлы в site/
        missing = []
        for html_file in (ROOT / "site").rglob("*.html"):
            content = html_file.read_text(encoding="utf-8")
            icons = re.findall(r'data-icon="([^"]+)"', content)
            for icon in icons:
                if icon not in registry:
                    missing.append(f"{html_file.relative_to(ROOT)}: {icon}")
        if missing:
            print(f"FAIL: {label} — не найдены иконки:", file=sys.stderr)
            for m in missing[:10]:
                print(f"  {m}", file=sys.stderr)
            return False
        print(f"OK: {label} (проверено {len(registry)} иконок)")
        return True
    except Exception as e:
        print(f"SKIP: {label} — {e}")
        return True  # не блокируем если icons.js нет


def check_view_routes() -> bool:
    """Every navigation target must resolve to a view or a declared alias."""
    import re
    label = "Маршруты навигации панели"
    print(f"\n==> {label}", flush=True)
    try:
        core = (ROOT / "site" / "assets" / "core.js").read_text(encoding="utf-8")
        html = (ROOT / "site" / "index.html").read_text(encoding="utf-8")
        views_match = re.search(r"const VIEWS = \{(.*?)\n\};\n/\* привычные синонимы", core, re.S)
        aliases_match = re.search(r"const VIEW_ALIASES = \{(.*?)\n\};", core, re.S)
        if not views_match or not aliases_match:
            raise ValueError("не найден реестр VIEWS или VIEW_ALIASES")
        views = set(re.findall(r"^\s*([a-z0-9_-]+):\s*\{", views_match.group(1), re.M))
        aliases = set(re.findall(r"^\s*([a-z0-9_-]+):\s*'", aliases_match.group(1), re.M))
        targets = {value for value in re.findall(r'\bdata-view="([^"]+)"', html) if value}
        missing = sorted(targets - views - aliases)
        if missing:
            print(f"FAIL: {label} — нет маршрута для: {', '.join(missing)}", file=sys.stderr)
            return False
        print(f"OK: {label} ({len(targets)} целей, {len(views)} экранов)")
        return True
    except Exception as exc:
        print(f"FAIL: {label} — {exc}", file=sys.stderr)
        return False


def check_frontend_assets() -> bool:
    """All working pages use one existing set of versioned local assets."""
    import re

    label = "Ресурсы HTML и оболочка service worker"
    print(f"\n==> {label}", flush=True)
    site = ROOT / "site"
    problems: list[str] = []
    versions: set[str] = set()
    main_assets: set[str] = set()

    class AssetParser(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.refs: list[str] = []

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            values = dict(attrs)
            ref = values.get("src") if tag == "script" else values.get("href") if tag == "link" and values.get("rel") == "stylesheet" else None
            if ref:
                self.refs.append(ref)

    for page in sorted(site.rglob("*.html")):
        if "prototypes" in page.parts:
            continue  # The approved D01 mockup has its own release cycle.
        parser = AssetParser()
        parser.feed(page.read_text(encoding="utf-8"))
        for ref in parser.refs:
            url = urlsplit(ref)
            if url.scheme or url.netloc or not url.path.endswith((".js", ".css")):
                continue
            target = site / url.path.lstrip("/") if url.path.startswith("/") else page.parent / url.path
            if not target.is_file():
                problems.append(f"{page.relative_to(site)}: отсутствует {ref}")
            version = parse_qs(url.query).get("v", [])
            if len(version) != 1 or not version[0]:
                problems.append(f"{page.relative_to(site)}: нет версии у {ref}")
            else:
                versions.add(version[0])
            if page == site / "index.html":
                main_assets.add("/" + target.relative_to(site).as_posix())

    if len(versions) != 1:
        problems.append(f"версии CSS/JS: {sorted(versions)}")
    sw = (site / "sw.js").read_text(encoding="utf-8")
    shell_match = re.search(r"const SHELL = \[(.*?)\];", sw, re.S)
    if not shell_match:
        problems.append("не найден SHELL в sw.js")
    else:
        shell = set(re.findall(r"'(/[^']+)'", shell_match.group(1)))
        for ref in sorted(shell):
            if ref != "/" and not (site / ref.lstrip("/")).is_file():
                problems.append(f"SHELL: отсутствует {ref}")
        for ref in sorted(main_assets - shell):
            problems.append(f"SHELL: нет ресурса главной страницы {ref}")
    if problems:
        for problem in problems[:20]:
            print(f"  {problem}", file=sys.stderr)
        print(f"FAIL: {label} ({len(problems)} ошибок)", file=sys.stderr)
        return False
    print(f"OK: {label} (версия {next(iter(versions))}, {len(main_assets)} ресурсов главной страницы)")
    return True


def check_inline_js(node_available: bool) -> bool:
    """19.0: скрипты, встроенные в HTML, тоже должны парситься.

    `node --check` в этом файле проверял только файлы `site/assets/*.js` и
    `sw.js`, а страница помощника держит всю логику во встроенном `<script>`.
    В 18.18 туда попал обрывок (`function` без имени и карточки, дописанные
    после закрывающего тега скрипта): кнопки панели молча не работали, и ни
    одна проверка этого не увидела. Здесь тот же разбор, что у Node, но
    собранный на stdlib: считаем баланс тегов и вытаскиваем встроенные блоки
    во временные файлы для `node --check`, если Node есть.
    """
    import os
    import re
    import tempfile
    label = "Встроенный JS в HTML"
    print(f"\n==> {label}", flush=True)
    pages = sorted((ROOT / "site").rglob("*.html"))
    problems: list[str] = []
    blocks: list[tuple[Path, str]] = []
    for page in pages:
        text = page.read_text(encoding="utf-8")
        opens = len(re.findall(r"<script\b", text))
        closes = len(re.findall(r"</script>", text))
        if opens != closes:
            problems.append(f"{page.relative_to(ROOT)}: <script> {opens}, </script> {closes}")
        for match in re.finditer(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", text, re.S):
            body = match.group(1)
            if body.strip():
                blocks.append((page, body))
    if problems:
        print(f"FAIL: {label} — не сходятся теги:", file=sys.stderr)
        for line in problems[:10]:
            print(f"  {line}", file=sys.stderr)
        return False
    if not node_available:
        print(f"SKIP: {label} — node не найден, проверен только баланс тегов ({len(blocks)} блоков)")
        return True
    for page, body in blocks:
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as handle:
            handle.write(body)
            temp = handle.name
        proceed = run(f"Встроенный JS: {page.relative_to(ROOT)}", ["node", "--check", temp])
        try:
            os.unlink(temp)
        except OSError:
            pass
        if not proceed:
            return False
    print(f"OK: {label} ({len(blocks)} блоков на {len(pages)} страницах)")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Проверить репозиторий PrintFlow")
    parser.add_argument("--quick", action="store_true",
                        help="не запускать полный набор unit-тестов")
    parser.add_argument("--require-tools", action="store_true",
                        help="считать отсутствие Node.js или Ruff ошибкой")
    args = parser.parse_args(argv)
    checks: list[bool] = []

    ruff = shutil.which("ruff")
    if ruff:
        checks.append(run("Ruff (ошибки Python)", [ruff, "check", ".", "--select", "F,E9"]))
    elif args.require_tools:
        print("FAIL: ruff не найден", file=sys.stderr)
        checks.append(False)
    else:
        print("SKIP: ruff не найден (установите: pip install ruff)")

    checks.append(run(
        "Компиляция Python",
        # `agent/` компилируется вместе с коннектором с 18.14: агент перестал
        # быть черновиком и несёт реестр навыков, свою базу и индекс документов.
        [sys.executable, "-m", "compileall", "-q", "connector", "agent", "pf.py",
         "launcher_window.py", "scripts"],
    ))

    node = shutil.which("node")
    js_files = sorted((ROOT / "site" / "assets").glob("*.js")) + [ROOT / "site" / "sw.js"]
    if node:
        for path in js_files:
            checks.append(run(f"JavaScript: {path.relative_to(ROOT)}",
                              [node, "--check", str(path)]))
        for path in sorted((ROOT / "scripts").glob("*.js")):
            checks.append(run(f"Скрипт JavaScript: {path.relative_to(ROOT)}",
                              [node, "--check", str(path)]))
    elif args.require_tools:
        print("FAIL: node не найден", file=sys.stderr)
        checks.append(False)
    else:
        print("SKIP: node не найден; синтаксис JS также проверяется unit-тестом при наличии Node.js")

    checks.append(check_inline_js(node is not None))

    # Б8: проверка что все data-icon существуют в PFIcons
    checks.append(check_data_icons())
    checks.append(check_view_routes())
    checks.append(check_frontend_assets())
    if node:
        checks.append(run("Service worker: старый/новый кэш и офлайн-ресурсы",
                          [node, "scripts/sw-check.js"]))

    # 14.0 (94): headless-стенд панели. node --check ловит только синтаксис,
    # а обращение к необъявленной переменной (Б1/Б2) видно лишь при загрузке
    # скриптов с заглушкой DOM.
    if node:
        checks.append(run("Headless-стенд панели (необъявленные переменные)",
                          [node, "scripts/panel-check.js"]))
    elif args.require_tools:
        print("FAIL: node не найден", file=sys.stderr)
        checks.append(False)
    else:
        print("SKIP: node не найден — стенд панели не запущен")

    # Раздел печати: каталог форм, фильтрация, серверный лист и штрихкод.
    # Раньше print-check.js существовал отдельно и общий прогон его пропускал.
    if node:
        checks.append(run("Headless-стенд печати (каталог, формы, штрихкод)",
                          [node, "scripts/print-check.js"]))
    elif args.require_tools:
        print("FAIL: node не найден — стенд печати не запущен", file=sys.stderr)
        checks.append(False)
    else:
        print("SKIP: node не найден — стенд печати не запущен")

    if node:
        checks.append(run("Headless-стенд настроек (черновик, сохранение, секреты)",
                          [node, "scripts/settings-check.js"]))
    elif args.require_tools:
        print("FAIL: node не найден — стенд настроек не запущен", file=sys.stderr)
        checks.append(False)
    else:
        print("SKIP: node не найден — стенд настроек не запущен")

    if node:
        checks.append(run("Headless-стенд клиент-бота (inbox и счётчики)",
                          [node, "scripts/clientbot-check.js"]))
    elif args.require_tools:
        print("FAIL: node не найден — стенд клиент-бота не запущен", file=sys.stderr)
        checks.append(False)
    else:
        print("SKIP: node не найден — стенд клиент-бота не запущен")

    if node:
        checks.append(run("Headless-стенд Центра смены (входящие и AMS)",
                          [node, "scripts/ops10-check.js"]))
        checks.append(run("Headless-стенд принтеров (камера, AMS и очередь)",
                          [node, "scripts/printer-check.js"]))
    elif args.require_tools:
        print("FAIL: node не найден — стенд Центра смены не запущен", file=sys.stderr)
        checks.append(False)
    else:
        print("SKIP: node не найден — стенд Центра смены не запущен")

    # Стенд кассы (17.0.13): страница кассы — не только вёрстка, но и логика
    # офлайн-очереди. Node --check видит синтаксис, а «очередь повторит продажу
    # с тем же request_id» проверяется только выполнением.
    if node:
        checks.append(run("Headless-стенд кассы (очередь и связь)",
                          [node, "scripts/kassa-check.js"]))
    elif args.require_tools:
        print("FAIL: node не найден — стенд кассы не запущен", file=sys.stderr)
        checks.append(False)
    else:
        print("SKIP: node не найден — стенд кассы не запущен")

    # Стенд пульта (18.0): страница команд — не только вёрстка. Стенд проверяет,
    # что пульт отправляет серверу ровно те тела, которые тот принимает
    # (confirmed, preflight, start_request_id), и что обрыв связи виден
    # оператору. Первый же прогон нашёл пропущенный resolve: все GET висели до
    # таймаута, а страница показывала «связи нет» при живом коннекторе.
    if node:
        checks.append(run("Headless-стенд пульта (парк, очередь, команды)",
                          [node, "scripts/pult-check.js"]))
    elif args.require_tools:
        print("FAIL: node не найден — стенд пульта не запущен", file=sys.stderr)
        checks.append(False)
    else:
        print("SKIP: node не найден — стенд пульта не запущен")

    # Стенд помощника (18.21): страница разговора держит логику во встроенном
    # скрипте. Стенд выполняет его с заглушкой DOM и сервера и проверяет путь
    # «фраза → мозг → карточка действия → Подтвердить → маршрут и журнал».
    if node:
        checks.append(run("Headless-стенд помощника (разговор, действие, память)",
                          [node, "scripts/assistant-check.js"]))
    elif args.require_tools:
        print("FAIL: node не найден — стенд помощника не запущен", file=sys.stderr)
        checks.append(False)
    else:
        print("SKIP: node не найден — стенд помощника не запущен")

    if not args.quick:
        # Unit tests use the production config by default. Keep their SQLite DB,
        # backups, uploads, and generated files in a separate disposable area.
        test_data_root = Path(tempfile.mkdtemp(prefix="printflow-unit-tests-"))
        test_env = os.environ.copy()
        if os.name == "nt":
            test_env["APPDATA"] = str(test_data_root)
        else:
            test_env["XDG_CONFIG_HOME"] = str(test_data_root)
        print(f"Unit-тесты используют изолированные данные: {test_data_root}", flush=True)
        checks.append(run(
            "Unit-тесты",
            [sys.executable, "-m", "unittest", "discover", "-s", "connector/tests", "-v"],
            env=test_env,
        ))

    if shutil.which("git") and (ROOT / ".git").exists():
        checks.append(run("Пробелы и конфликт-маркеры", ["git", "diff", "--check"]))

    failed = checks.count(False)
    print(f"\n{'FAILED' if failed else 'PASSED'}: {len(checks) - failed}/{len(checks)} проверок")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
