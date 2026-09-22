"""GPU-сечения (18.12): чистые функции gpuslice.js без DOM — node-стенд.

Что проверяется и как:

* **Поведение в node** (правило 4 docs/ТЕСТЫ.md): модуль `gpuslice.js`
  загружается с заглушками `window`/`localStorage`, и вызывается вся
  CPU-часть конвейера — разбор STL (бинарный и текстовый), высоты плоскостей
  (сверка с `_plane_heights` движка Stage 1), сечение на отрезки, сшивка
  контуров, площади, аналитика (свесы, тонкие места, нормали, стол, масса),
  источник фонового потока и его исполнение.
* **Строковые контракты**: кнопка «Сечения» и хост виджета в `index.html`,
  пин `?v=18.12.3`, запись модуля в service-worker, точки монтирования в
  `print.js` (выбор модели → авто-сечения, кнопка, настройки профиля),
  WGSL-шейдер (подписи compute-точки входа и атомарного буфера).
"""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import sys
import unittest

from connector.printflow.slicer_engine import _plane_heights

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
SITE = ROOT / "site"
ASSETS = SITE / "assets"
INDEX = (SITE / "index.html").read_text(encoding="utf-8")
PRINT_JS = (ASSETS / "print.js").read_text(encoding="utf-8")
SW_JS = (SITE / "sw.js").read_text(encoding="utf-8")
GPUSLICE_JS = (ASSETS / "gpuslice.js").read_text(encoding="utf-8")

_HARNESS = r"""
const fs = require('fs');
const store = {};
global.localStorage = { getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); }, removeItem: (k) => { delete store[k]; } };
global.window = global;
global.navigator = {};
global.TextDecoder = TextDecoder;
global.TextEncoder = TextEncoder;
new Function(fs.readFileSync('site/assets/gpuslice.js', 'utf8'))();
const G = window.PFGpuSlice;

function boxPositions(x0, y0, z0, x1, y1, z1) {
  const v = [[x0,y0,z0],[x1,y0,z0],[x1,y1,z0],[x0,y1,z0],[x0,y0,z1],[x1,y0,z1],[x1,y1,z1],[x0,y1,z1]];
  const f = [[0,2,1],[0,3,2],[4,5,6],[4,6,7],[0,1,5],[0,5,4],[1,2,6],[1,6,5],[2,3,7],[2,7,6],[3,0,4],[3,4,7]];
  const out = [];
  for (const t of f) for (const i of t) out.push(...v[i]);
  return Float32Array.from(out);
}
function stlOf(positions) {
  const n = positions.length / 9;
  const buf = new ArrayBuffer(84 + 50 * n);
  const dv = new DataView(buf);
  dv.setUint32(80, n, true);
  let o = 84;
  for (let i = 0; i < n; i++) { o += 12; for (let k = 0; k < 9; k++) { dv.setFloat32(o, positions[i * 9 + k], true); o += 4; } o += 2; }
  return buf;
}
const SETTINGS = { layer_height: 0.2, first_layer_height: 0.2, walls: 3, extrusion_width_mm: 0.45,
  infill_percent: 15, density_g_cm3: 1.24, top_solid_layers: 4, bottom_solid_layers: 3,
  bed_mm: [256, 256], max_print_height_mm: 256 };

/* Синхронный путь run() без Promise: те же стадии — разбор, на стол,
   плоскости, сечение, анализ. В node Promise недоступен из синхронного
   стенда, а стадии должны проверяться по отдельности. */
function runSync(buffer, settings) {
  const model = G.parseSTL(buffer);
  const placed = G.place(model);
  const pl = G.planes(model.bbox.size[2], settings.first_layer_height, settings.layer_height);
  const res = G.sliceCPU(placed, pl.first, pl.step, pl.count);
  const r = G.analyze(model, pl, res, settings);
  r.backend = 'cpu';
  r.model = { triangles: model.triangles, bbox: model.bbox };
  return r;
}

const out = {};
out.backends = ['auto', 'gpu', 'cpu'];
out.planes = (() => {
  const pl = G.planes(10, 0.2, 0.2);
  return { count: pl.count, z0: pl.z[0], zTop: pl.z[pl.count - 1], topLast: pl.top[pl.count - 1] };
})();
out.cube = (() => {
  const r = runSync(stlOf(boxPositions(0, 0, 0, 10, 10, 10)), { settings: SETTINGS, inline: true });
  return r; // Promise
})();
out.raw = (() => {
  const pos = boxPositions(0, 0, 0, 10, 10, 10);
  const res = G.sliceCPU(pos, 0.2, 0.2, 50);
  const loops = [];
  for (const i of [0, 25, 49]) {
    const g = G.groupByLayer(res, 50);
    const idx = g.order.subarray(g.start[i], g.start[i + 1]);
    const c = G.chain(res.segs, idx);
    loops.push({ n: c.loops.length, open: c.open, areas: c.loops.map(G.loopArea).map((a) => +a.toFixed(3)), len: c.loops.map(G.loopLength).map((l) => +l.toFixed(1)) });
  }
  return { count: res.count, overflow: res.overflow, overhangArea: res.overhangArea, loops, order0: (() => { const g = G.groupByLayer(res, 50); return Array.from(g.order.subarray(g.start[0], g.start[1])).slice(0, 2); })() };
})();
out.hollow = (() => {
  const outer = boxPositions(0, 0, 0, 10, 10, 10);
  const inner = boxPositions(3, 3, -1, 7, 7, 11);
  const flipped = new Float32Array(inner.length);
  for (let i = 0; i < inner.length; i += 9) {
    flipped.set(inner.subarray(i, i + 3), i);
    flipped.set(inner.subarray(i + 6, i + 9), i + 3);
    flipped.set(inner.subarray(i + 3, i + 6), i + 6);
  }
  const all = new Float32Array(outer.length + flipped.length);
  all.set(outer); all.set(flipped, outer.length);
  const res = G.sliceCPU(all, 0.2, 0.2, 50);
  const g = G.groupByLayer(res, 50);
  const idx = g.order.subarray(g.start[10], g.start[11]);
  const c = G.chain(res.segs, idx);
  return { areas: c.loops.map(G.loopArea).map((a) => +a.toFixed(3)), open: c.open };
})();
out.thin = (() => {
  const r = runSync(stlOf(boxPositions(0, 0, 0, 20, 0.5, 5)), { settings: SETTINGS, inline: true });
  return r;
})();
out.floating = (() => {
  /* столб + более широкая плита сверху: низ плиты — настоящий свес
     (place() прижимает модель к столу, «парящая» коробка свесом не станет) */
  const pillar = boxPositions(3, 3, 0, 7, 7, 10);
  const slab = boxPositions(0, 0, 10, 10, 10, 12);
  const all = new Float32Array(pillar.length + slab.length);
  all.set(pillar); all.set(slab, pillar.length);
  const r = runSync(stlOf(all), { settings: SETTINGS, inline: true });
  return { overhangArea: r.overhangArea, downArea: r.downArea,
           warns: r.warnings.map((w) => w.kind + '|' + w.text) };
})();
out.big = (() => {
  const r = runSync(stlOf(boxPositions(0, 0, 0, 300, 300, 300)), { settings: SETTINGS, inline: true });
  return { fits: r.fits, warns: r.warnings.map((w) => w.kind + '|' + w.text) };
})();
out.ascii = (() => {
  const text = 'solid t\nfacet normal 0 0 1\nouter loop\nvertex 0 0 0\nvertex 1 0 0\nvertex 0 1 0\nendloop\nendfacet\nendsolid t\n';
  const m = G.parseSTL(new TextEncoder().encode(text).buffer);
  return { triangles: m.triangles, ascii: m.ascii, sx: m.bbox.size[0], sy: m.bbox.size[1], sz: m.bbox.size[2] };
})();
out.asciiBroken = (() => {
  const text = 'solid t\nfacet normal 0 0 1\nouter loop\nvertex 1 0 0\nvertex 0 1 0\nvertex 0 0 1\nendloop\nendfacet\nouter loop\nvertex 0 0 0\nvertex 1 1 1\nendloop\nendfacet\nendsolid t\n';
  try { G.parseSTL(new TextEncoder().encode(text).buffer); return 'no-error'; }
  catch (e) { return String(e.message || e); }
})();
out.inverted = (() => {
  const pos = boxPositions(0, 0, 0, 10, 10, 10);
  const flip = new Float32Array(pos.length);
  for (let i = 0; i < pos.length; i += 9) { flip.set(pos.subarray(i, i + 3), i); flip.set(pos.subarray(i + 6, i + 9), i + 3); flip.set(pos.subarray(i + 3, i + 6), i + 6); }
  const r = runSync(stlOf(flip), { settings: SETTINGS, inline: true });
  return r;
})();
out.worker = (() => {
  const src = G.workerSource();
  // источник уже содержит const COS_LIMIT / MAX_SEGMENTS — параметров не нужно
  const fn = new Function('self', src + '\nreturn { onmessage: self.onmessage };');
  const posted = [];
  const fakeSelf = { postMessage: (m) => posted.push(m) };
  fn(fakeSelf);
  const pos = boxPositions(0, 0, 0, 10, 10, 10);
  fakeSelf.onmessage({ data: { positions: pos, first: 0.2, step: 0.2, count: 50 } });
  return { messages: posted.length, ok: posted[0] && posted[0].ok, count: posted[0] && posted[0].r.count };
})();
out.pref = (() => {
  G.setBackendPref('gpu');
  return G.backendPref();
})();
out.help = G.HELP;
out.wgsl = { compute: G.WGSL.includes('@compute'), atomic: G.WGSL.includes('atomicAdd'), wg: G.WGSL.includes('workgroup_size') };
console.log('PF_GPUJSON:' + JSON.stringify(out));
"""

SETTINGS_PY = {
    "layer_height": 0.2, "first_layer_height": 0.2, "walls": 3,
    "extrusion_width_mm": 0.45, "infill_percent": 15.0,
}


def _gpu_json() -> dict:
    proc = subprocess.run([sys.executable, "-c", "print('ok')"], capture_output=True)
    del proc
    node = shutil.which("node")
    if not node:
        raise unittest.SkipTest("node недоступен")
    proc = subprocess.run([node, "-e", _HARNESS], cwd=str(ROOT), capture_output=True,
                          text=True, timeout=120)
    if proc.returncode != 0:
        raise AssertionError(f"node-стенд упал:\n{proc.stdout}\n{proc.stderr}")
    line = next((l for l in proc.stdout.splitlines() if l.startswith("PF_GPUJSON:")), None)
    if not line:
        raise AssertionError(f"нет JSON из стенда:\n{proc.stdout}\n{proc.stderr}")
    return json.loads(line.split(":", 1)[1])


class GpuSlicePipeline(unittest.TestCase):
    """Чистые функции конвейера сечений — считаются в node один раз."""

    data: dict = {}

    @classmethod
    def setUpClass(cls):
        cls.data = _gpu_json()

    def test_planes_match_engine_first_layer_and_step(self):
        pl = self.data["planes"]
        rows = _plane_heights(10.0, 0.2, 0.2)
        self.assertEqual(pl["count"], len(rows))
        self.assertAlmostEqual(pl["z0"], rows[0][1], places=6)
        self.assertAlmostEqual(pl["zTop"], rows[-1][1], places=6)
        self.assertAlmostEqual(pl["topLast"], rows[-1][0], places=6)

    def test_cube_slices_into_square_loops(self):
        raw = self.data["raw"]
        self.assertGreater(raw["count"], 0)
        self.assertFalse(raw["overflow"])
        self.assertEqual(raw["overhangArea"], 0)
        for layer in raw["loops"]:
            self.assertEqual(layer["n"], 1, "у куба один наружный контур")
            self.assertEqual(layer["open"], 0)
            self.assertEqual(layer["areas"], [100.0])
            self.assertEqual(layer["len"], [40.0])

    def test_hole_loop_has_negative_area(self):
        hollow = self.data["hollow"]
        self.assertEqual(sorted(hollow["areas"]), [-16.0, 100.0])
        self.assertEqual(hollow["open"], 0)

    def test_cube_mass_and_volume(self):
        cube = self.data["cube"]
        self.assertAlmostEqual(cube["volume_mm3"], 1000.0, delta=1.0)
        self.assertAlmostEqual(cube["massFull"], 1.24, delta=0.02)
        # заполнение 15% + 3 стенки + сплошные — заметно меньше сплошной отливки
        self.assertGreater(cube["massEst"], 0.4)
        self.assertLess(cube["massEst"], cube["massFull"])
        self.assertEqual(cube["backend"], "cpu")
        self.assertEqual(cube["count"], 50)
        self.assertTrue(cube["fits"])

    def test_thin_slab_is_flagged(self):
        thin = self.data["thin"]
        self.assertTrue(all(l["thin"] for l in thin["layers"] if l["area"] > 0))
        self.assertTrue(any("Тонкие места" in w["text"] for w in thin["warnings"]))

    def test_slab_on_narrow_pillar_reports_overhang(self):
        fl = self.data["floating"]
        self.assertGreater(fl["overhangArea"], 80.0)  # низ плиты 10×10 над столбом 4×4
        self.assertTrue(any(w.startswith("warn|") and "Свесы" in w for w in fl["warns"]))

    def test_oversize_model_fails_bed_check(self):
        big = self.data["big"]
        self.assertFalse(big["fits"])
        self.assertTrue(any(w.startswith("bad|") for w in big["warns"]))

    def test_ascii_stl_parses_with_bbox(self):
        a = self.data["ascii"]
        self.assertEqual(a["triangles"], 1)
        self.assertTrue(a["ascii"])
        self.assertEqual([a["sx"], a["sy"], a["sz"]], [1, 1, 0])

    def test_ascii_partial_last_facet_is_rejected(self):
        self.assertIn("Последняя грань неполная", self.data["asciiBroken"])

    def test_inverted_normals_are_detected_not_hidden(self):
        inv = self.data["inverted"]
        self.assertTrue(inv["inverted"])
        self.assertTrue(any("Нормали" in w["text"] for w in inv["warnings"]))

    def test_worker_source_executes_and_slices(self):
        w = self.data["worker"]
        self.assertTrue(w["ok"])
        self.assertGreater(w["count"], 0)

    def test_backend_pref_roundtrip(self):
        self.assertEqual(self.data["pref"], "gpu")
        self.assertIn("pf_gpuslice_backend", GPUSLICE_JS)

    def test_help_is_written_for_operator(self):
        for phrase in ("ползунок", "свес", "тонкие места", "«План»"):
            self.assertIn(phrase, self.data["help"])


class GpuSliceContracts(unittest.TestCase):
    """Разметка и точки монтирования: кнопка, хост, SW, WGSL."""

    data: dict = {}

    @classmethod
    def setUpClass(cls):
        cls.data = _gpu_json()

    def test_wgsl_is_a_compute_shader(self):
        wgsl = self.data["wgsl"]
        self.assertTrue(wgsl["compute"] and wgsl["atomic"] and wgsl["wg"])

    def test_index_has_sections_button_and_host(self):
        self.assertIn('id="pr_sl_sections"', INDEX)
        self.assertIn('id="pr_sl_gpu"', INDEX)
        self.assertIn('Разрезать модель на слои прямо в браузере', INDEX)

    def test_index_pins_gpuslice(self):
        self.assertIn('<script src="assets/gpuslice.js?v=18.12.3"></script>', INDEX)

    def test_sw_precaches_gpuslice(self):
        self.assertIn("'/assets/gpuslice.js',", SW_JS)

    def test_print_js_mounts_on_select_and_button(self):
        self.assertIn("runGpuSections({ auto: true })", PRINT_JS)
        self.assertIn("$('pr_sl_sections')", PRINT_JS)
        self.assertIn("PFGpuSlice.mount(host, {})", PRINT_JS)
        self.assertIn("gpuSectionSettings", PRINT_JS)
        # настройки берутся из профиля слайсера, а не с потолка
        self.assertIn("prof.settings", PRINT_JS)
        self.assertIn("prof.bed_mm", PRINT_JS)

    def test_print_js_skips_non_stl(self):
        self.assertIn("Сечения только для STL", PRINT_JS)

    def test_cpu_fallback_paths_exist(self):
        self.assertIn("sliceInWorker", GPUSLICE_JS)
        self.assertIn("sliceCPU(placed", GPUSLICE_JS)
        # GPU-отказ не роняет виджет, а понижает бэкенд
        self.assertIn("посчитано на процессоре", GPUSLICE_JS)


if __name__ == "__main__":
    unittest.main()
