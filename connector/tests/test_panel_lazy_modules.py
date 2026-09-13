"""Ленивые разделы панели: загрузка и ошибка должны быть видны (17.0.16).

Тяжёлые разделы (`marketing`, `clientbot`, `ops10`) грузятся при первом входе.
До этой правки загрузка была молчаливой: при медленном Wi-Fi оператор смотрел
на пустую вкладку и не понимал, идёт загрузка или всё сломалось, а при ошибке
`PF.loadModule` уходил в `console.error` — вкладка оставалась пустой навсегда,
без способа повторить.

Это контракт на исходник, а не на поведение: заглушка DOM стенда панели
(`scripts/panel-check.js`) отвечает цепочкой на любой `querySelector`, поэтому
DOM-логику плашки она показать не может. Стенд при этом проверяет своё — что в
`core.js` нет обращений к необъявленным переменным.
"""
from __future__ import annotations

import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
CORE = (ROOT / "site" / "assets" / "core.js").read_text(encoding="utf-8")
CSS = (ROOT / "site" / "assets" / "app.css").read_text(encoding="utf-8")


def block(marker: str, end: str) -> str:
    start = CORE.index(marker)
    return CORE[start:CORE.index(end, start)]


class LazyModuleFeedbackTests(unittest.TestCase):
    def test_entering_a_lazy_section_shows_progress(self):
        section = block("if (LAZY_MODULES[name] && !lazyLoaded.has(name))", "  }\n}")
        self.assertLess(section.index("showLazyLoading(name)"),
                        section.index("PF.loadModule(name)"),
                        "плашка «загружаем» должна появляться до запроса файла")

    def test_success_clears_the_note(self):
        loader = block("PF.loadModule = (name) => {", "PF.isLazyLoaded")
        self.assertIn("clearLazyNote(name)", loader)

    def test_failure_is_visible_and_repeatable(self):
        loader = block("PF.loadModule = (name) => {", "PF.isLazyLoaded")
        catch = loader.split(".catch(", 1)[1]
        self.assertIn("showLazyError(name", catch,
                      "ошибка загрузки уходит в console и не видна оператору")
        error = block("function showLazyError(", "PF.lazyState")
        self.assertIn("Повторить", error)
        self.assertIn("toast(", error, "об ошибке должен быть и тост")
        # Кнопка действия в тосте — объект { label, run } (см. toast() в core.js).
        tail = error.split("toast(", 1)[1]
        self.assertIn("label:", tail, "в тосте нет подписи кнопки")
        self.assertIn("run:", tail, "в тосте нет действия кнопки")

    def test_retry_forgets_the_failed_script(self):
        """Повтор обязан снять недогруженный <script>: иначе `data-lazy`
        останется, и загрузчик решит, что файл уже есть."""
        error = block("function showLazyError(", "PF.lazyState")
        self.assertIn("lazyPending.delete(name)", error)
        loader = block("PF.loadModule = (name) => {", "PF.isLazyLoaded")
        self.assertIn("tag.dataset.failed = name", loader)

    def test_lazy_note_is_styled_for_both_states(self):
        self.assertIn(".lazy-note", CSS)
        self.assertIn('.lazy-note[data-state="loading"]', CSS)
        self.assertIn('.lazy-note[data-state="error"]', CSS)

    def test_lazy_modules_registry_is_intact(self):
        """Список ленивых модулей — часть кэш-контракта: имя раздела в
        `VIEWS`/`LAZY_MODULES` и имя файла в `site/assets/`."""
        registry = block("const LAZY_MODULES = {", "};")
        for name, file in (("marketing", "marketing.js"),
                           ("clientbot", "clientbot.js"),
                           ("ops10", "ops10.js")):
            self.assertIn(f"{name}: ['{file}']", registry)
            self.assertTrue((ROOT / "site" / "assets" / file).exists(), f"нет {file}")


if __name__ == "__main__":
    unittest.main()
