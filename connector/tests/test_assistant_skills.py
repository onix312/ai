"""Реестр навыков ассистента компьютера (18.14): данные, подтверждение, честность.

Ассистент вырос из «помощника панели» в личную программу на компьютере, и у
такой программы три способа превратиться в дыру: навык, которого нет в коде, но
есть в интерфейсе; навык, который двигает файлы без человека; и навык, который
молча не работает. Поэтому контракты здесь ровно про это:

  * реестр совпадает с исполнением: у каждого объявленного навыка есть
    обработчик, а самопроверка `skills.validate()` не находит нарушений;
  * подтверждение определяется риском из реестра, а вызывающая сторона не может
    его понизить (`confirmed` в теле запроса не читается);
  * недоступный навык не прячется: у него есть причина, и она не пустая;
  * параметры разбираются по объявленным типам, а необъявленное имя отбрасывается
    с предупреждением — модель не может протащить `delete_all`;
  * выученный навык (идея И180) собирается только из существующих шагов, всегда
    требует подтверждения и не может занять имя встроенного.

Живые окна, звук и снимки экрана здесь не проверяются: для них есть
`test_agent_os.py` и чеки-лист владельца в `docs/ПОМОЩНИК.md`.
"""
from __future__ import annotations

import pathlib
import re
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from agent import executor, fileops, skills  # noqa: E402
from agent.panel_client import Client  # noqa: E402
from agent.store import Store  # noqa: E402

IDEA_DOCS = (ROOT / "docs" / "ИДЕИ-100.md", ROOT / "docs" / "ИДЕИ-АССИСТЕНТ-ПК.md")
SKILLS_DOC = ROOT / "docs" / "НАВЫКИ.md"

# Способности «всё есть»: так проверяется, что навык работает, когда ему ничего
# не мешает. Живые способности (панель, модель) подменяются заглушкой.
FULL_CAPS = {name: True for name in skills.CAPABILITIES}
FULL_CAPS.update({f"{name}_reason": "" for name in skills.CAPABILITIES})


class RunnerTestCase(unittest.TestCase):
    """Исполнитель с временной базой и мёртвой панелью: всё честно недоступно."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(pathlib.Path(self.tmp.name) / "assistant.sqlite3")
        self.runner = executor.Runner(store=self.store,
                                      panel=Client("http://127.0.0.1:1"))
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.store.close)


class RegistryShapeTests(unittest.TestCase):
    def test_registry_passes_self_check(self):
        self.assertEqual([], skills.validate(),
                         "реестр навыков разошёлся с собственными правилами")

    def test_every_declared_skill_is_dispatched(self):
        """Объявленный навык без обработчика — кнопка, которая молча ничего не делает.

        Файловые навыки зовутся на временной папке: проверять диспетчер на живом
        `~/Downloads` значило бы двигать файлы владельца ради теста.
        """
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(pathlib.Path(tmp) / "a.sqlite3")
            runner = executor.Runner(store=store, panel=Client("http://127.0.0.1:1"))
            folder_params = {"folder": tmp, "folders": tmp, "path": tmp}
            try:
                for name in skills.SKILLS:
                    params = dict(folder_params) if name.startswith("files.") else {}
                    result = runner.run(name, params, confirmed=True)
                    self.assertNotIn("не исполняется", str(result.get("reason") or ""),
                                     f"навык «{name}» объявлен, но не исполняется")
            finally:
                store.close()

    def test_names_are_unique_and_shaped(self):
        names = list(skills.SKILLS)
        self.assertEqual(len(names), len(set(names)))
        for name in names:
            self.assertRegex(name, r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$",
                             f"имя «{name}» не выглядит как «группа.навык»")

    def test_confirmation_follows_risk(self):
        for name, skill in skills.SKILLS.items():
            if skill["risk"] in skills.CONFIRM_RISKS:
                self.assertTrue(skills.confirm_required({"name": name, **skill}),
                                f"«{name}» двигает чужое и не спрашивает подтверждения")
            if skill["risk"] == "read":
                self.assertFalse(skills.confirm_required({"name": name, **skill}),
                                 f"чтение «{name}» не должно требовать подтверждения")

    def test_ideas_are_described_in_docs(self):
        """Номер идеи обязан находиться в каталоге: иначе навык вырос из воздуха."""
        text = "\n".join(path.read_text(encoding="utf-8") for path in IDEA_DOCS
                         if path.exists())
        self.assertTrue(text, "каталоги идей не найдены")
        for name, skill in skills.SKILLS.items():
            self.assertTrue(skill["ideas"], f"у «{name}» не указана идея")
            for idea in skill["ideas"]:
                self.assertIn(idea, text,
                              f"идея {idea} навыка «{name}» не описана в каталоге идей")

    def test_doc_lists_every_live_skill(self):
        """Справка навыков не должна расходиться с реестром (как справка маршрутов)."""
        text = SKILLS_DOC.read_text(encoding="utf-8")
        listed = set(re.findall(r"`([a-z][a-z0-9_]*\.[a-z][a-z0-9_]*)`", text))
        missing = sorted(set(skills.SKILLS) - listed)
        self.assertEqual([], missing,
                         "этих навыков нет в docs/НАВЫКИ.md — добавьте их в таблицу живых")

    def test_panel_is_a_skill_not_the_owner(self):
        """PrintFlow — один из навыков: панельные навыки идут через loopback."""
        panel = [name for name, skill in skills.SKILLS.items() if skill["host"] == "panel"]
        self.assertTrue(panel, "панель не представлена навыками")
        own = [name for name, skill in skills.SKILLS.items() if skill["host"] == "agent"]
        self.assertGreater(len(own), len(panel),
                           "ассистент не должен состоять только из панели: он личный")


class AvailabilityTests(unittest.TestCase):
    def test_missing_capability_gives_reason(self):
        skill = {"name": "panel.ask", **skills.SKILLS["panel.ask"]}
        available, reason = skills.availability(skill, {})
        self.assertFalse(available)
        self.assertTrue(reason, "причина недоступности не может быть пустой")

    def test_full_capabilities_make_skill_available(self):
        for name, skill in skills.SKILLS.items():
            available, reason = skills.availability({"name": name, **skill}, FULL_CAPS)
            self.assertTrue(available, f"«{name}» недоступен при полном наборе: {reason}")

    def test_reason_comes_from_capabilities_not_from_skill(self):
        caps = dict(FULL_CAPS)
        caps.update(panel=False, panel_reason="Панель PrintFlow не отвечает на 127.0.0.1:8765")
        _available, reason = skills.availability(
            {"name": "day.briefing", **skills.SKILLS["day.briefing"]}, caps)
        self.assertEqual("Панель PrintFlow не отвечает на 127.0.0.1:8765", reason)

    def test_catalog_hides_nothing(self):
        rows = skills.catalog({}, {})
        self.assertEqual(len(skills.SKILLS), len(rows))
        for row in rows:
            if not row["available"]:
                self.assertTrue(row["reason"],
                                f"«{row['name']}» недоступен без объяснения")

    def test_prompt_offers_only_available_skills(self):
        text = skills.prompt({"sqlite": True, "files": True})
        self.assertIn("files.search", text)
        self.assertNotIn("panel.ask", text,
                         "недоступный навык не должен предлагаться модели")


class ParamsTests(unittest.TestCase):
    def check(self, name, raw):
        return skills.check_params({"params": skills.SKILLS[name]["params"]}, raw)

    def test_unknown_param_is_dropped_with_warning(self):
        clean, errors = self.check("files.search", {"query": "договор", "delete_all": "да"})
        self.assertEqual({"query": "договор"}, clean)
        self.assertTrue(any("delete_all" in error for error in errors))

    def test_bad_int_stops_the_skill(self):
        _clean, errors = self.check("files.search", {"query": "договор", "limit": "много"})
        self.assertTrue(any("limit" in error for error in errors))

    def test_oneof_rejects_foreign_value(self):
        _clean, errors = self.check("files.to_order",
                                    {"path": "/tmp/a.stl", "order": "145", "kind": "weapon"})
        self.assertTrue(any("kind" in error for error in errors))

    def test_oneof_accepts_declared_value(self):
        clean, errors = self.check("files.to_order",
                                   {"path": "/tmp/a.stl", "order": "145", "kind": "MODEL"})
        self.assertEqual("model", clean["kind"])
        self.assertEqual([], errors)

    def test_bool_understands_russian(self):
        clean, errors = self.check("files.facts", {"path": "/tmp/a.md", "save": "да"})
        self.assertIs(True, clean["save"])
        self.assertEqual([], errors)

    def test_object_param_accepts_structure_only(self):
        _clean, errors = self.check("panel.do", {"action": "park", "params": "не структура"})
        self.assertTrue(any("params" in error for error in errors))

    def test_path_param_rejects_nul(self):
        _clean, errors = self.check("files.facts", {"path": "/tmp/a\x00.md"})
        self.assertTrue(errors)


class LearnTests(unittest.TestCase):
    def test_valid_skill_is_learned(self):
        skill, reason = skills.learn({
            "name": "my.utro", "title": "Утро цеха",
            "steps": [{"skill": "day.briefing"},
                      {"skill": "files.recent", "params": {"limit": 5}}]})
        self.assertEqual("", reason)
        self.assertIsNotNone(skill)
        self.assertTrue(skill["confirm"], "выученный навык обязан спрашивать человека")
        # Оба шага — чтение, поэтому риск остаётся чтением, но подтверждение
        # у выученного навыка всегда требуется (цепочка чужих шагов).
        self.assertEqual("read", skill["risk"], "риск — максимальный из шагов")
        self.assertEqual(("panel", "sqlite"), tuple(skill["requires"]))
        self.assertEqual(["И180"], list(skill["ideas"]))

    def test_valid_skill_risk_is_max_of_steps(self):
        skill, reason = skills.learn({
            "name": "my.big", "title": "Раскладка",
            "steps": [{"skill": "files.tidy_plan"},
                      {"skill": "files.tidy_apply"}]})
        self.assertEqual("", reason)
        self.assertEqual("write", skill["risk"])

    def test_builtin_name_cannot_be_taken(self):
        skill, reason = skills.learn({"name": "panel.do", "title": "Обход",
                                      "steps": [{"skill": "agent.skills"}]})
        self.assertIsNone(skill)
        self.assertIn("my.", reason)

    def test_unknown_step_is_refused(self):
        skill, reason = skills.learn({"name": "my.test", "title": "Тест",
                                      "steps": [{"skill": "files.delete_everything"}]})
        self.assertIsNone(skill)
        self.assertIn("нет в реестре", reason)

    def test_learned_cannot_call_learned(self):
        learned = {"my.base": {"title": "База", "steps": [{"skill": "agent.skills"}],
                               "risk": "read", "requires": (), "params": {}}}
        skill, reason = skills.learn({"name": "my.chain", "title": "Цепочка",
                                      "steps": [{"skill": "my.base"}]}, learned)
        self.assertIsNone(skill)
        self.assertIn("выученный", reason.casefold())

    def test_long_scenario_is_refused(self):
        steps = [{"skill": "agent.skills"}] * (skills.MAX_LEARNED_STEPS + 1)
        skill, reason = skills.learn({"name": "my.long", "title": "Длинный", "steps": steps})
        self.assertIsNone(skill)
        self.assertIn("шагов", reason)

    def test_bad_step_params_are_refused(self):
        skill, reason = skills.learn({"name": "my.bad", "title": "Плохой",
                                      "steps": [{"skill": "files.search",
                                                 "params": {"limit": "много"}}]})
        self.assertIsNone(skill)
        self.assertIn("limit", reason)

    def test_learned_skill_needs_no_params_of_its_own(self):
        skill, _reason = skills.learn({"name": "my.simple", "title": "Простой",
                                       "steps": [{"skill": "agent.skills"}]})
        self.assertEqual({}, skill["params"])


class DescribeTests(unittest.TestCase):
    def test_every_skill_has_confirmation_text(self):
        for name, skill in skills.SKILLS.items():
            sample = {key: "1" for key in skill["params"]}
            text = executor.describe({"name": name, **skill}, sample)
            self.assertTrue(text.strip(), f"у «{name}» нет текста для окна подтверждения")
            self.assertLess(len(text), 400)

    def test_learned_skill_shows_its_chain(self):
        text = executor.describe(
            {"name": "my.utro", "title": "Утро цеха",
             "steps": [{"skill": "day.briefing"}, {"skill": "files.recent"}]}, {})
        self.assertIn("day.briefing", text)
        self.assertIn("files.recent", text)


class ExecutionTests(RunnerTestCase):
    def test_unknown_skill_is_refused_and_journalled(self):
        result = self.runner.run("files.delete_everything", {})
        self.assertFalse(result["ok"])
        self.assertIn("нет в реестре", result["reason"])
        entries = self.store.journal_recent(5)
        self.assertTrue(any(row["skill"] == "files.delete_everything"
                            and row["outcome"] == "refused" for row in entries))

    def test_unavailable_skill_reports_capability_reason(self):
        result = self.runner.run("panel.actions", {})
        self.assertFalse(result["ok"])
        self.assertTrue(result["unavailable"])
        self.assertIn("панель", result["reason"].casefold())

    def test_params_error_stops_execution(self):
        result = self.runner.run("files.search", {"query": "договор", "limit": "много"})
        self.assertFalse(result["ok"])
        self.assertIn("limit", result["reason"])

    def test_write_skill_waits_for_human(self):
        result = self.runner.run("files.tidy_apply", {})
        self.assertFalse(result["ok"])
        self.assertTrue(result["needs_confirmation"])
        self.assertTrue(result["text"], "окно подтверждения обязано показывать, что будет")
        entry = self.store.last_outcome("files.tidy_apply")
        self.assertEqual("needs_confirmation", entry["outcome"])

    def test_confirmed_flag_is_not_taken_from_params(self):
        """`confirmed` в параметрах навыка не существует: его ставит человек."""
        clean, errors = skills.check_params(
            {"params": skills.SKILLS["files.tidy_apply"]["params"]},
            {"folder": "/tmp", "confirmed": True})
        self.assertTrue(any("confirmed" in error for error in errors))
        self.assertNotIn("confirmed", clean)

    def test_read_skill_runs_without_confirmation(self):
        result = self.runner.run("files.search", {"query": "договор"})
        self.assertNotIn("needs_confirmation", result)
        self.assertIn("reason", result)

    def test_reads_are_not_journalled_but_refusals_are(self):
        """Чтение в журнал не пишется — иначе лента утонет (правило 18.13)."""
        before = self.store.stats()["journal"]
        self.runner.run("files.search", {"query": "что угодно"})
        self.assertEqual(before, self.store.stats()["journal"])
        self.runner.run("files.search", {"query": ""})
        self.assertEqual(before + 1, self.store.stats()["journal"])

    def test_write_skill_is_journalled(self):
        self.runner.run("files.index", {"folders": self.tmp.name})
        entry = self.store.last_outcome("files.index")
        self.assertIsNotNone(entry)
        self.assertEqual("files.index", entry["skill"])

    def test_learned_skill_runs_steps_after_confirmation(self):
        learned, reason = skills.learn({"name": "my.report", "title": "Отчёт",
                                        "steps": [{"skill": "files.search",
                                                   "params": {"query": "договор"}},
                                                  {"skill": "agent.skills"}]})
        self.assertEqual("", reason)
        self.store.save_skill("my.report", learned)
        waiting = self.runner.run("my.report", {})
        self.assertTrue(waiting["needs_confirmation"])
        result = self.runner.run("my.report", {}, confirmed=True)
        self.assertTrue(result["ok"], result.get("reason"))
        self.assertEqual(2, len(result["steps"]))
        self.assertTrue(all(step["ok"] for step in result["steps"]))

    def test_learned_skill_stops_on_first_failure(self):
        # Первый шаг доступен, но падает при исполнении (пустой запрос), поэтому
        # сценарий останавливается и второй шаг не выполняется.
        learned, _reason = skills.learn({"name": "my.broken", "title": "Сломанный",
                                         "steps": [{"skill": "files.search",
                                                    "params": {"query": ""}},
                                                   {"skill": "agent.skills"}]})
        self.assertIsNotNone(learned, _reason)
        self.store.save_skill("my.broken", learned)
        result = self.runner.run("my.broken", {}, confirmed=True)
        self.assertFalse(result["ok"])
        self.assertEqual(1, len(result["steps"]), "после отказа идти дальше нельзя")
        self.assertIn("Шаг 1", result["reason"])

    def test_skill_failure_does_not_kill_runner(self):
        """Навык обязан упасть внутрь себя: агент живёт дальше."""
        def boom(_params):
            raise RuntimeError("внутри навыка всё сломалось")

        original = self.runner._dispatch
        self.runner._dispatch = lambda skill, params: boom(params)
        try:
            result = self.runner.run("files.search", {"query": "договор"})
        finally:
            self.runner._dispatch = original
        self.assertFalse(result["ok"])
        self.assertIn("Навык упал", result["reason"])
        self.assertEqual("failed", self.store.last_outcome("files.search")["outcome"])


class MetaSkillTests(RunnerTestCase):
    def test_agent_skills_lists_registry_with_reasons(self):
        result = self.runner.run("agent.skills", {})
        self.assertTrue(result["ok"])
        self.assertEqual(len(skills.SKILLS), result["count"])
        self.assertLess(result["ready"], result["count"],
                        "без панели и модели часть навыков обязана быть недоступна")
        for row in result["unavailable"]:
            self.assertTrue(row["reason"])

    def test_agent_why_explains_unavailable_skill(self):
        result = self.runner.run("agent.why", {"skill": "day.briefing"})
        self.assertTrue(result["ok"])
        self.assertFalse(result["available"])
        self.assertTrue(result["reasons"])
        self.assertIn("недоступен", result["reason"])

    def test_agent_why_shows_last_failure(self):
        self.runner.run("files.search", {"query": ""})
        result = self.runner.run("agent.why", {"skill": "files.search"})
        self.assertTrue(result["ok"])
        self.assertIsNotNone(result["last"])
        self.assertTrue(any("Последний вызов" in line for line in result["reasons"]))

    def test_agent_why_needs_a_skill_name(self):
        result = self.runner.run("agent.why", {})
        self.assertFalse(result["ok"])
        self.assertIn("Укажите навык", result["reason"])

    def test_agent_journal_is_readable(self):
        self.runner.run("files.index", {"folders": self.tmp.name})
        result = self.runner.run("agent.journal", {"limit": 5})
        self.assertTrue(result["ok"])
        self.assertTrue(result["entries"])
        self.assertLessEqual(len(result["entries"]), 5)

    def test_learn_saves_skill_into_own_store(self):
        result = self.runner.run("agent.learn", {"skill": {
            "name": "my.morning", "title": "Утро", "description": "План и файлы",
            "steps": [{"skill": "files.recent", "params": {"limit": 3}}]}}, confirmed=True)
        self.assertTrue(result["ok"], result.get("reason"))
        self.assertIn("my.morning", self.store.learned_skills())
        self.assertTrue(result["available"], "навык из локальных шагов обязан быть доступен")

    def test_learn_refuses_bad_shape(self):
        result = self.runner.run("agent.learn", {"skill": {"name": "hack", "steps": []}},
                                 confirmed=True)
        self.assertFalse(result["ok"])
        self.assertIn("my.", result["reason"])

    def test_learned_skill_appears_in_catalog(self):
        self.store.save_skill("my.saved", {
            "title": "Сохранённый", "description": "Проверка", "host": "agent",
            "risk": "read", "confirm": True, "params": {},
            "steps": [{"skill": "agent.skills", "params": {}}],
            "requires": (), "ideas": ("И180",), "doc": "тест"})
        names = [row["name"] for row in self.runner.catalog()]
        self.assertIn("my.saved", names)
        self.assertTrue(skills.get("my.saved", self.runner.learned())["learned"])


class GuardTests(unittest.TestCase):
    def test_no_skill_executes_outside_allowed_folders(self):
        """Файловые навыки обязаны проверять границу папок до чтения."""
        allowed, reason, _path = fileops.inside_allowed("/etc/passwd")
        self.assertFalse(allowed)
        self.assertTrue(reason)

    def test_registry_has_no_destructive_skill(self):
        """Удаление файлов навыком не делается вовсе: только перемещение."""
        for name, skill in skills.SKILLS.items():
            self.assertNotIn("delete", name)
            self.assertNotIn("удал", str(skill["description"]).casefold())
            self.assertNotEqual("irreversible", skill["risk"],
                                f"«{name}» необратим: такого навыка в 18.14 быть не должно")


if __name__ == "__main__":
    unittest.main()
