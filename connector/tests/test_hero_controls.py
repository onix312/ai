"""Пульт 18.11: регулятор скорости, тракт AMS, шкала слоёв, шторка инцидентов, звуки.

Что проверяется и как:

* **Поведение в node** (правило 4 docs/ТЕСТЫ.md): модули `knob.js`,
  `amspath.js`, `incidents.js`, `sound.js`, `layers.js` подгружаются с
  заглушкой DOM, и вызываются их чистые функции — раскладка делений
  регулятора, SVG тракта AMS, важность и маршрут инцидента, группировка
  событий, счётчик непрочитанного, настройки звука и тихие часы.
* **Строковые контракты**: разметка шапки и шторки в `index.html`, пины
  `?v=18.12.3`, иконки реестра, исправление `p.temperature` в Hero-пульте,
  инкрементальный рендер (ключ структуры `heroKey`), точки монтирования
  новых модулей — DOM-логику стенд `panel-check.js` не воспроизводит.
"""
from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
SITE = ROOT / "site"
ASSETS = SITE / "assets"
INDEX = (SITE / "index.html").read_text(encoding="utf-8")
APP_JS = (ASSETS / "app.js").read_text(encoding="utf-8")
SW_JS = (SITE / "sw.js").read_text(encoding="utf-8")
PRINTER_JS = (ASSETS / "printer.js").read_text(encoding="utf-8")
CONTROLS_CSS = (ASSETS / "controls.css").read_text(encoding="utf-8")
ICONS_JS = (ASSETS / "icons.js").read_text(encoding="utf-8")
LAYERS_JS = (ASSETS / "layers.js").read_text(encoding="utf-8")
NEW_ASSETS = ("sound.js", "knob.js", "amspath.js", "layers.js", "incidents.js")

_HARNESS = r"""
const fs = require('fs');
const store = {};
global.localStorage = { getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); }, removeItem: (k) => { delete store[k]; } };
const noop = () => {};
const fakeEl = () => ({ setAttribute: noop, appendChild: noop, classList: { add: noop, toggle: noop, remove: noop }, style: {}, querySelector: () => null, querySelectorAll: () => [], addEventListener: noop, dataset: {} });
global.document = { addEventListener: noop, removeEventListener: noop, getElementById: () => null, querySelectorAll: () => [], querySelector: () => null, readyState: 'complete', dispatchEvent: noop, body: { classList: { add: noop, remove: noop, toggle: noop } }, createElement: fakeEl, createElementNS: fakeEl };
global.window = global;
global.CustomEvent = function (n, o) { this.type = n; this.detail = o && o.detail; };
global.PF = { state: STATE, on: noop, api: {}, ui: {}, printer: (id) => ({ name: 'P1S-цех' }) };
for (const f of ['sound.js', 'knob.js', 'amspath.js', 'layers.js', 'incidents.js']) {
  new Function(fs.readFileSync('site/assets/' + f, 'utf8'))();
}
const ams = {
  units: 1, humidity: 4, temperature: 27,
  trays: [
    { id: '00', unit: 0, slot: 0, label: 'AMS 1 · слот 1', type: 'PLA', color: '#ff0000', remain: 62, active: true, present: true },
    { id: '01', unit: 0, slot: 1, present: false },
    { id: '02', unit: 0, slot: 2, type: 'PETG', color: '#00ff00', remain: 10, present: true, generic: true },
    { id: '254', unit: 255, slot: 254, label: 'Внешний слот', type: 'TPU', color: '#0000ff', present: true },
  ],
};
const amsSwitched = JSON.parse(JSON.stringify(ams));
amsSwitched.trays[0].active = false; amsSwitched.trays[2].active = true;
const out = {
  knob: {
    angles: [1, 2, 3, 4].map(PFKnob.angleOf),
    levels: [-135, -100, -45, 10, 45, 100, 135].map(PFKnob.levelOfAngle),
    labels: PFKnob.LEVELS.map((l) => [l.level, l.label, l.pct]),
  },
  ams: {
    html: PFAmsPath.render(ams),
    empty: PFAmsPath.render({ trays: [] }),
    sigSame: PFAmsPath.signature(ams) === PFAmsPath.signature(JSON.parse(JSON.stringify(ams))),
    sigChanged: PFAmsPath.signature(ams) !== PFAmsPath.signature(amsSwitched),
    humidity: [1, 2, 3, 5, 35, 70].map(PFAmsPath.humidityText),
    active: PFAmsPath.activeTray(ams).id,
  },
  incidents: {
    live: PFIncidents.liveItems().map((i) => [i.severity, i.title, !!i.paused]),
    events: PFIncidents.eventItems().map((i) => [i.severity, i.title, i.count]),
    routes: {
      filament: PFIncidents.routeOf({ kind: 'filament_low' }),
      order: PFIncidents.routeOf({ kind: 'order' }),
      printer: PFIncidents.routeOf({ kind: 'error', printer_id: 'p1' }),
      update: PFIncidents.routeOf({ kind: 'update' }),
      unknown: PFIncidents.routeOf({ kind: 'zzz' }),
    },
    severity: {
      complete: PFIncidents.severityOf({ kind: 'complete', title: 'Печать завершена без ошибок' }),
      failText: PFIncidents.severityOf({ kind: 'system', title: 'Не удалось сохранить' }),
      warnText: PFIncidents.severityOf({ kind: 'spool', title: 'Катушка заканчивается' }),
      plain: PFIncidents.severityOf({ kind: 'order', title: 'Новый заказ' }),
    },
    unread: PFIncidents.unread(),
  },
  sound: {
    prefs: PFSound.prefs(),
    playWithoutAudio: PFSound.play('click'),
    events: PFSound.EVENT_SOUND,
    names: Object.keys(PFSound.SOUNDS),
  },
  layers: { order: PFLayers.ORDER, colors: Object.keys(PFLayers.COLORS), help: PFLayers.HELP },
};
PFSound.set({ quiet: true, quiet_from: 0, quiet_to: 24 });
out.sound.quietAllDay = PFSound.quietNow();
PFSound.set({ quiet: true, quiet_from: 25, quiet_to: 25 });
out.sound.quietNever = PFSound.quietNow();
out.sound.persisted = JSON.parse(store.pf_sound).quiet_from;
console.log(JSON.stringify(out));
"""

_STATE = {
    "live": {"printers": [{
        "id": "p1", "name": "P1S-цех", "enabled": True,
        "connection": {"configured": True, "connected": False, "last_error": "timeout"},
        "printer": {"state": "PAUSE", "task": "box.3mf",
                    "problems": [{"code": "0300-0D00", "title": "Обрыв филамента", "severity": "error", "advice": "Заправьте"},
                                 {"code": "0500-0100", "title": "Совет", "severity": "info"}]},
    }]},
    "events": [
        {"id": 5, "kind": "error", "title": "Ошибка печати", "detail": "HMS", "printer_id": "p1", "at": "2026-09-20T05:00:00"},
        {"id": 4, "kind": "error", "title": "Ошибка печати", "detail": "HMS", "printer_id": "p1", "at": ""},
        {"id": 3, "kind": "filament_low", "title": "Мало филамента"},
        {"id": 2, "kind": "order", "title": "Новый заказ"},
        {"id": 1, "kind": "complete", "title": "Печать завершена без ошибок"},
    ],
}


def _run_harness() -> dict:
    script = "const STATE = " + json.dumps(_STATE, ensure_ascii=False) + ";\n" + _HARNESS
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=60, cwd=ROOT)
    assert proc.returncode == 0, f"node упал: {proc.stderr[:800]}"
    return json.loads(proc.stdout)


@unittest.skipUnless(shutil.which("node"), "нужен node")
class ModulesBehaviorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = _run_harness()

    # ---- регулятор
    def test_knob_detents_are_evenly_spread_and_snap(self):
        knob = self.out["knob"]
        self.assertEqual([-135, -45, 45, 135], knob["angles"])
        self.assertEqual([1, 1, 2, 3, 3, 4, 4], knob["levels"])
        self.assertEqual([[1, "Тихо", 50], [2, "Стандарт", 100], [3, "Спорт", 124], [4, "Ludicrous", 166]],
                         knob["labels"], "четыре режима Bambu, в порядке уровней 1..4")

    # ---- тракт AMS
    def test_ams_path_draws_slots_external_spool_and_flow(self):
        ams = self.out["ams"]
        html = ams["html"]
        self.assertEqual(5, html.count('class="ams-spool '), "4 слота + внешняя катушка")
        flowing = re.findall(r'class="ams-tube ([a-z]+) flow', html)
        self.assertEqual(["trunk", "slot"], flowing,
                         "«течёт» ровно трубка активного слота и ствол к соплу")
        self.assertIn("ams-spool empty", html)
        self.assertIn("ams-spool present generic", html)
        self.assertIn("Заканчивается: 1", html, "PETG с остатком 10 % попадает в предупреждение")
        self.assertIn("влажно", html)
        self.assertIn("Сейчас: <b>AMS 1 · слот 1</b>", html)
        self.assertIn("внешняя", html)
        self.assertIn("сопло", html)
        self.assertEqual("00", ams["active"])

    def test_ams_signature_is_stable_and_reacts_to_switch(self):
        ams = self.out["ams"]
        self.assertTrue(ams["sigSame"], "одинаковый снимок — одна подпись: анимация не сбрасывается")
        self.assertTrue(ams["sigChanged"], "смена активного слота меняет подпись — перерисовка нужна")

    def test_ams_empty_and_humidity_text(self):
        ams = self.out["ams"]
        self.assertIn("AMS не подключён", ams["empty"])
        self.assertEqual(["сухо", "нормально", "влажновато", "очень влажно", "35 % RH", "70 % RH"], ams["humidity"])

    # ---- инциденты
    def test_live_incidents_from_telemetry(self):
        live = self.out["incidents"]["live"]
        self.assertEqual([["warn", "Нет связи: P1S-цех", False],
                          ["bad", "P1S-цех: Обрыв филамента", False],
                          ["info", "P1S-цех: Совет", False],
                          ["warn", "P1S-цех на паузе", True]], live)

    def test_events_group_and_classify(self):
        events = self.out["incidents"]["events"]
        self.assertEqual([["bad", "Ошибка печати", 2], ["warn", "Мало филамента", 1],
                          ["info", "Новый заказ", 1], ["info", "Печать завершена без ошибок", 1]], events)

    def test_severity_heuristics(self):
        sev = self.out["incidents"]["severity"]
        self.assertEqual("info", sev["complete"], "«без ошибок» не делает завершение тревогой")
        self.assertEqual("bad", sev["failText"])
        self.assertEqual("warn", sev["warnText"])
        self.assertEqual("info", sev["plain"])

    def test_routes_lead_to_the_right_section(self):
        routes = self.out["incidents"]["routes"]
        self.assertEqual(["inventory", "Склад пластика"], routes["filament"])
        self.assertEqual(["orders", "Заказы"], routes["order"])
        self.assertEqual(["printers", "Станок"], routes["printer"])
        self.assertEqual(["settings", "Настройки"], routes["update"])
        self.assertEqual(["dashboard", "Обзор"], routes["unknown"])

    def test_unread_counts_alarms_only(self):
        unread = self.out["incidents"]["unread"]
        self.assertEqual({"events": 2, "live": 3, "worst": "bad"}, unread,
                         "инфо-события в счётчик не входят; живые проблемы — входят")

    # ---- звуки
    def test_sound_defaults_and_safety_without_audio(self):
        snd = self.out["sound"]
        self.assertEqual({"enabled": True, "ui": True, "events": True, "volume": 60, "quiet": False,
                          "quiet_from": 22, "quiet_to": 7}, snd["prefs"])
        self.assertFalse(snd["playWithoutAudio"], "нет AudioContext — тихо, без исключений")
        self.assertEqual(["click", "toggle", "tick", "success", "finish", "warn", "alert"], snd["names"])
        self.assertEqual("finish", snd["events"]["complete"])
        self.assertEqual("alert", snd["events"]["error"])
        self.assertEqual("warn", snd["events"]["filament_low"])
        self.assertTrue(snd["quietAllDay"])
        self.assertFalse(snd["quietNever"])
        self.assertEqual(25, snd["persisted"], "настройки живут в localStorage устройства")

    # ---- слои
    def test_layers_buckets_match_backend(self):
        from connector.printflow.gcode_layers import BUCKETS
        lay = self.out["layers"]
        self.assertEqual(set(BUCKETS), set(lay["order"]))
        self.assertEqual(set(BUCKETS), set(lay["colors"]))
        self.assertIn("Следить", lay["help"])


class MarkupContractTests(unittest.TestCase):
    """Разметка и пины: то, что стенд не прогоняет, а браузер требует."""

    def test_new_assets_are_wired_with_current_version(self):
        for name in NEW_ASSETS:
            self.assertIn(f'<script src="assets/{name}?v=18.12.3"></script>', INDEX, name)
            self.assertIn(f"'/assets/{name}'", SW_JS, f"{name} должен быть в SHELL sw.js")
        self.assertIn('<link rel="stylesheet" href="assets/controls.css?v=18.12.3">', INDEX)
        self.assertIn("'/assets/controls.css'", SW_JS)
        self.assertIn('<script src="assets/app.js?v=18.12.3"></script>', INDEX)
        self.assertIn('<script src="assets/core.js?v=18.12.3"></script>', INDEX)
        self.assertIn("printflow-shell-v83", SW_JS)
        # Порядок: модули пульта грузятся после icons.js и до app.js.
        order = [INDEX.index(f"assets/{n}?v=") for n in ("icons.js", *NEW_ASSETS, "app.js")]
        self.assertEqual(order, sorted(order))

    def test_topbar_has_incidents_and_sound_buttons(self):
        self.assertIn('id="incidents_btn"', INDEX)
        self.assertIn('data-icon="siren"', INDEX)
        self.assertIn('id="sound_btn"', INDEX)
        self.assertIn('data-icon="volume"', INDEX)
        for name in ("volume", "volumeoff", "siren"):
            self.assertRegex(ICONS_JS, rf"\n  {name}: '<", f"иконка {name} в реестре PFIcons")

    def test_incidents_drawer_markup(self):
        for needle in ('id="incidents_drawer"', 'id="incidents_list"', 'data-inc-close', 'data-inc-readall',
                       'data-inc-filter="all"', 'data-inc-filter="bad"', 'data-inc-filter="warn"',
                       'data-inc-filter="info"', 'id="incidents_printers_only"', 'Alt+I'):
            self.assertIn(needle, INDEX, needle)

    def test_gpuslice_asset_wired(self):
        self.assertIn('<script src="assets/gpuslice.js?v=18.12.3"></script>', INDEX)
        self.assertIn("'/assets/gpuslice.js'", SW_JS)

    def test_sound_settings_card(self):
        self.assertIn('<h2>Звуки панели</h2>', INDEX)
        self.assertIn('id="set_sound"', INDEX)

    def test_controls_css_covers_every_module(self):
        for cls in (".pf-knob", ".pf-knob-detent", ".ams-tube.flow", ".ams-spool", ".lay-canvas", ".lay-strip",
                    ".inc-drawer", ".inc-badge", ".inc-row.bad", ".snd-row", ".spin"):
            self.assertIn(cls, CONTROLS_CSS, cls)
        self.assertIn("prefers-reduced-motion", CONTROLS_CSS, "анимации отключаемы")


class HeroPultContractTests(unittest.TestCase):
    """Hero-пульт после 18.11: температура, инкрементальный рендер, модули."""

    def test_temperature_field_bug_is_fixed(self):
        self.assertIn("p.temperature || p.temperatures", APP_JS,
                      "снимок отдаёт temperature — иначе Hero-пульт показывал «—» вместо градусов")
        self.assertNotRegex(APP_JS, r"const t = p\.temperatures \|\| \{\};")

    def test_incremental_render_keeps_camera_stream(self):
        self.assertIn("host.dataset.heroKey", APP_JS)
        for field in ("pct", "rem", "bar", "facts", "telemetry", "ams", "knob", "layers"):
            self.assertIn(f'data-hf="{field}"', APP_JS, field)
        # Живые поля обновляются без пересборки DOM (и без нового <img> MJPEG).
        self.assertIn("host.querySelector('[data-hf=\"pct\"]').innerHTML", APP_JS)
        self.assertIn("knobEl._knob.set(speedLvl)", APP_JS)
        self.assertIn("layEl._layers.update({ layer: info.layer", APP_JS)

    def test_modules_are_mounted_and_speed_uses_knob(self):
        self.assertIn("PFKnob.create(knobEl", APP_JS)
        self.assertIn("PFLayers.mount(layEl, { printerId: p.id", APP_JS)
        self.assertIn("PFAmsPath.mount(", APP_JS)
        self.assertNotIn('data-hero-speed="1"', APP_JS, "четыре кнопки скорости заменены регулятором")
        self.assertIn("execHeroSpeed(p.id, level, null)", APP_JS)
        self.assertIn("knobEl._knob.fail()", APP_JS, "отказ станка возвращает стрелку")
        # Контракт test_design11: класс сегмента остаётся в CSS.
        more_css = (ASSETS / "more.css").read_text(encoding="utf-8")
        self.assertIn(".hero-speed-seg", more_css)

    def test_eta_is_epoch_seconds(self):
        self.assertIn("new Date(num(info.eta) > 1e12 ? num(info.eta) : num(info.eta) * 1000)", APP_JS,
                      "eta приходит числом секунд, а не ISO-строкой")

    def test_layers_widget_calls_backend_routes(self):
        self.assertIn("'/api/gcode/layers'", LAYERS_JS)
        self.assertIn("'/api/gcode/layer'", LAYERS_JS)
        self.assertIn("building", LAYERS_JS, "виджет умеет ждать фоновый разбор")
        self.assertIn("getContext('2d')", LAYERS_JS)
        self.assertIn("if (!ctx) return;", LAYERS_JS, "стенд без канвы не должен падать")

    def test_printers_tab_mounts_the_same_widgets_as_hero(self):
        """18.12: «Принтеры» — те же виджеты, что и Hero-пульт на «Обзоре»."""
        for needle in ('id="pr_speed_knob_row"', 'id="pr_speed_knob"',
                       'id="pr_ams_path"', 'id="pr_ams_path_host"',
                       'id="pr_layers_card"', 'id="pr_layers_host"',
                       'Регулятор скорости', 'Тихо 50 %'):
            self.assertIn(needle, INDEX, needle)
        for needle in ("function renderPultWidgets", "renderPultWidgets(p, kind)",
                       "PFKnob.create(knobEl", "PFAmsPath.mount(pathHost, p.ams",
                       "PFLayers.mount(layHost", "knobEl._knob.fail()",
                       "api.destroy", "layCard.hidden = true"):
            self.assertIn(needle, PRINTER_JS, needle)
        for cls in (".pr-knob-row", ".pr-knob-note", ".pr-layers-card", ".pr-ams-path"):
            self.assertIn(cls, CONTROLS_CSS, cls)

    def test_old_speed_selector_stays_as_fallback(self):
        # старый путь не выброшен: если knob.js не загрузился, селектор возвращается
        self.assertIn('id="pr_speed_row"', INDEX)
        self.assertIn('id="pr_speed_apply"', INDEX)
        self.assertIn("oldRow.hidden = true", PRINTER_JS)

    def test_studio_card_shows_relay_tls_and_announce(self):
        for needle in ("studio_relay_enabled", "studio_relay_targets", "tls_handshake_timeouts",
                       "tls_resumed", "relay_printers", 'id="studio_announce_btn"', "/api/studio/announce"):
            self.assertIn(needle, APP_JS, needle)


if __name__ == "__main__":
    unittest.main()
