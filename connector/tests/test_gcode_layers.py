"""Шкала слоёв (18.11): послойный индекс G-code и маршруты Hero-пульта.

Проверяется поведение настоящего кода — `connector/printflow/gcode_layers.py`
и маршруты `GET /api/gcode/layers` / `GET /api/gcode/layer`:

* маркеры слоёв трёх слайсеров: Bambu Studio («; CHANGE_LAYER», «; Z_HEIGHT»,
  «; FEATURE»), Orca/Prusa («;LAYER_CHANGE», «;Z:», «;TYPE:»), Cura («;LAYER:»);
  без маркеров — смена Z при экструзии;
* корзины по «;TYPE:» и «; FEATURE:», расход пластика на слой, M73;
* относительный и абсолютный экструдер, G92, дуги G2/G3;
* прореживание: коллинеарные точки схлопываются, слой не длиннее
  LAYER_SEGMENT_CAP отрезков;
* 3MF: G-code читается из Metadata/plate_N.gcode нужной плиты;
* маршруты: причина, когда файла нет; индекс по имени файла; отрезки слоя;
  печатающееся задание принтера через print_jobs.
"""
from __future__ import annotations

import io
import math
import pathlib
import sys
import tempfile
import time
import unittest
import zipfile
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow import gcode_layers as gl  # noqa: E402
from connector.printflow.router import router  # noqa: E402
from connector.tests.test_phase11 import make_api, make_db  # noqa: E402

BAMBU = """; HEADER_BLOCK_START
M83
G90
G92 E0
; CHANGE_LAYER
; Z_HEIGHT: 0.2
; LAYER_HEIGHT: 0.2
; layer num/total_layer_count: 1/3
M73 P0 R30
G1 X10 Y10 Z0.2 F3000
; FEATURE: Outer wall
G1 X50 Y10 E1.5
G1 X50 Y50 E1.5
G1 X10 Y50 E1.5
G1 X10 Y10 E1.5
; FEATURE: Sparse infill
G1 X20 Y20 E0
G1 X40 Y40 E0.9
G2 X60 Y40 I10 J0 E0.5
; CHANGE_LAYER
; Z_HEIGHT: 0.4
; LAYER_HEIGHT: 0.2
; layer num/total_layer_count: 2/3
M73 P40 R18
G1 Z0.4
; FEATURE: Inner wall
G1 X12 Y12 E0
G1 X48 Y12 E1.2
G1 X48 Y48 E1.2
; FEATURE: Bridge
G1 X12 Y48 E1.2
; CHANGE_LAYER
; Z_HEIGHT: 0.6
; LAYER_HEIGHT: 0.2
; layer num/total_layer_count: 3/3
M73 P80 R6
G1 Z0.6
; FEATURE: Support
G1 X30 Y30 E0
G1 X31 Y31 E0.1
"""

ORCA = """M83
G90
;LAYER_CHANGE
;Z:0.2
;HEIGHT:0.2
G1 X0 Y0 Z0.2
;TYPE:External perimeter
G1 X10 Y0 E1
;TYPE:Perimeter
G1 X10 Y10 E1
;TYPE:Internal infill
G1 X0 Y10 E1
;TYPE:Top solid infill
G1 X0 Y0 E1
;TYPE:Support material interface
G1 X5 Y5 E0.2
;TYPE:Skirt/Brim
G1 X6 Y6 E0.2
;TYPE:Overhang perimeter
G1 X7 Y7 E0.2
;TYPE:Custom
G1 X8 Y8 E0.2
"""

CURA = """M82
G90
G92 E0
;LAYER_COUNT:2
;LAYER:0
G0 X0 Y0 Z0.3
;TYPE:WALL-OUTER
G1 X10 Y0 E1
G1 X10 Y10 E2
;TYPE:FILL
G1 X0 Y10 E3
;LAYER:1
G0 Z0.6
;TYPE:SKIN
G1 X0 Y0 E4
"""


class BucketTests(unittest.TestCase):
    def test_bambu_and_prusa_and_cura_names(self):
        cases = {
            "Outer wall": "wall_outer", "External perimeter": "wall_outer", "WALL-OUTER": "wall_outer",
            "Inner wall": "wall_inner", "Perimeter": "wall_inner", "WALL-INNER": "wall_inner",
            "Sparse infill": "infill", "Internal infill": "infill", "FILL": "infill",
            "Internal solid infill": "solid", "Top surface": "solid", "Bottom surface": "solid",
            "Top solid infill": "solid", "SKIN": "solid", "Ironing": "solid", "Gap infill": "solid",
            "Support": "support", "Support interface": "support", "Support material": "support",
            "Skirt": "skirt", "Brim": "skirt", "Prime tower": "skirt", "Wipe tower": "skirt", "Skirt/Brim": "skirt",
            "Bridge": "bridge", "Overhang wall": "bridge", "Bridge infill": "bridge", "Internal Bridge": "bridge",
            "Custom": "other", "": "other",
        }
        for text, want in cases.items():
            self.assertEqual(want, gl.bucket_of(text), text)


class ParserTests(unittest.TestCase):
    def test_bambu_markers_layers_and_features(self):
        idx = gl.parse_text(BAMBU)
        self.assertFalse(idx.building)
        self.assertEqual("", idx.error)
        self.assertEqual(3, len(idx.layers))
        self.assertEqual(3, idx.total_declared)
        one, two, three = (layer.summary() for layer in idx.layers)
        self.assertEqual((1, 0.2, 0.2), (one["i"], one["z"], one["h"]))
        self.assertEqual((2, 0.4), (two["i"], two["z"]))
        self.assertEqual((3, 0.6), (three["i"], three["z"]))
        # Пластик: квадрат 4×1.5 + заполнение 0.9 + дуга 0.5.
        self.assertAlmostEqual(7.4, one["ext"], places=3)
        self.assertEqual({"wall_outer", "infill"}, set(one["types"]))
        self.assertEqual(4, one["types"]["wall_outer"])
        self.assertEqual({"wall_inner", "bridge"}, set(two["types"]))
        self.assertEqual({"support"}, set(three["types"]))
        # M73 перед первым ходом слоя — прогресс слоя.
        self.assertEqual((0.0, 30.0), (one["pct"], one["rem"]))
        self.assertEqual((40.0, 18.0), (two["pct"], two["rem"]))
        self.assertEqual((80.0, 6.0), (three["pct"], three["rem"]))

    def test_arc_is_split_into_chords_and_bbox_covers_it(self):
        idx = gl.parse_text(BAMBU)
        infill = idx.layers[0].paths["infill"]
        # Прямая 20,20→40,40 и дуга радиуса 10 до 60,40: хорд больше одной.
        self.assertGreater(len(infill) // 4, 3)
        self.assertEqual([10.0, 10.0, 60.0, 50.0], idx.bbox())
        # Дуга проходит через верхнюю точку (50, 50) — центр (50, 40), R=10.
        ys = [infill[i + 3] for i in range(0, len(infill), 4)]
        self.assertAlmostEqual(50.0, max(ys), places=1)

    def test_orca_and_prusa_types(self):
        idx = gl.parse_text(ORCA)
        self.assertEqual(1, len(idx.layers))
        types = idx.layers[0].summary()["types"]
        self.assertEqual({"wall_outer", "wall_inner", "infill", "solid", "support",
                          "skirt", "bridge", "other"}, set(types))

    def test_cura_layers_with_absolute_extruder(self):
        idx = gl.parse_text(CURA)
        self.assertEqual(2, len(idx.layers))
        self.assertEqual(2, idx.total_declared)
        first, second = idx.layers
        self.assertEqual(0.3, round(first.z, 3))
        self.assertAlmostEqual(3.0, first.ext, places=3)
        self.assertEqual({"wall_outer", "infill"}, set(first.paths))
        self.assertEqual(0.6, round(second.z, 3))
        self.assertAlmostEqual(1.0, second.ext, places=3)
        self.assertEqual({"solid"}, set(second.paths))

    def test_g90_does_not_reset_relative_extruder(self):
        # Bambu пишет M83 до G90: G90 не должен возвращать экструдер в абсолют.
        text = "M83\nG90\n;LAYER_CHANGE\n;Z:0.2\nG1 X0 Y0 Z0.2\nG1 X10 Y0 E1\nG1 X10 Y10 E1\nG1 X0 Y10 E1\n"
        idx = gl.parse_text(text)
        self.assertEqual(1, len(idx.layers))
        self.assertAlmostEqual(3.0, idx.layers[0].ext, places=3)
        self.assertEqual(3, idx.layers[0].segments)

    def test_fallback_layers_by_z_without_markers(self):
        text = "\n".join([
            "G90", "M82", "G92 E0",
            "G1 X0 Y0 Z0.2", "G1 X10 Y0 E1", "G1 X10 Y10 E2",
            "G1 Z0.6", "G1 Z0.4",              # z-hop туда-обратно — не слой
            "G1 X0 Y10 E3", "G92 E0", "G1 X0 Y0 E1",
            "G1 Z0.8", "G1 Z0.6", "G1 X10 Y0 E2",
        ])
        idx = gl.parse_text(text)
        got = [(layer.index, round(layer.z, 2), round(layer.height, 2), round(layer.ext, 2))
               for layer in idx.layers]
        self.assertEqual([(1, 0.2, 0.2, 2.0), (2, 0.4, 0.2, 2.0), (3, 0.6, 0.2, 1.0)], got)

    def test_collinear_points_are_merged(self):
        lines = ["M83", "G90", ";LAYER_CHANGE", ";Z:0.2", ";HEIGHT:0.2", "G1 X0 Y0 Z0.2",
                 ";TYPE:Outer wall"]
        lines += [f"G1 X{i} Y0 E0.1" for i in range(1, 101)]
        lines += [f"G1 X100 Y{i} E0.1" for i in range(1, 101)]
        idx = gl.parse_text("\n".join(lines))
        layer = idx.layers[0]
        self.assertEqual(2, layer.segments, "две прямые — два отрезка, а не 200")
        self.assertEqual([0.0, 0.0, 100.0, 0.0, 100.0, 0.0, 100.0, 100.0],
                         list(layer.paths["wall_outer"]))
        self.assertAlmostEqual(20.0, layer.ext, places=3, msg="пластик считается по всем ходам")

    def test_segment_cap_thins_but_keeps_every_bucket(self):
        lines = ["M83", "G90", ";LAYER_CHANGE", ";Z:0.2", ";HEIGHT:0.2", "G1 X128 Y128 Z0.2"]
        for name, radius in (("Outer wall", 60), ("Sparse infill", 30)):
            lines.append(f";TYPE:{name}")
            for k in range(3000):
                a = k / 3000 * 2 * math.pi
                lines.append(f"G1 X{128 + radius * math.cos(a):.3f} Y{128 + radius * math.sin(a):.3f} E0.05")
        idx = gl.parse_text("\n".join(lines))
        layer = idx.layers[0]
        self.assertLessEqual(layer.segments, gl.LAYER_SEGMENT_CAP)
        self.assertEqual({"wall_outer", "infill"}, set(layer.paths))
        self.assertGreater(len(layer.paths["infill"]) // 4, 10)

    def test_detail_rounds_coordinates(self):
        idx = gl.parse_text(BAMBU)
        detail = idx.layer(1).detail()
        self.assertEqual(1, detail["i"])
        self.assertIn("wall_outer", detail["paths"])
        for value in detail["paths"]["wall_outer"]:
            self.assertEqual(value, round(value, 2))
        self.assertIsNone(idx.layer(0))
        self.assertIsNone(idx.layer(4))

    def test_truncated_after_max_lines(self):
        with mock.patch.object(gl, "MAX_LINES", 20):
            idx = gl.parse_text(BAMBU)
        self.assertTrue(idx.truncated)
        self.assertGreaterEqual(len(idx.layers), 1)

    def test_overview_hides_layers_while_building(self):
        idx = gl.LayerIndex("x")
        self.assertTrue(idx.building)
        self.assertEqual([], idx.overview()["layers"])
        self.assertIsNone(idx.layer(1))
        idx.build(io.StringIO(BAMBU))
        self.assertFalse(idx.building)
        self.assertEqual(3, len(idx.overview()["layers"]))


class FilesAndCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = pathlib.Path(self.tmp.name)
        gl.clear_cache()
        self.addCleanup(gl.clear_cache)

    def make_3mf(self, name="job.3mf", plates=(1, 2)) -> pathlib.Path:
        path = self.dir / name
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("3D/3dmodel.model", "<model/>")
            for plate in plates:
                body = BAMBU if plate == 1 else CURA
                zf.writestr(f"Metadata/plate_{plate}.gcode", body)
        return path

    def test_gcode_member_prefers_requested_plate(self):
        path = self.make_3mf()
        self.assertEqual("Metadata/plate_2.gcode", gl.gcode_member(path, 2))
        self.assertEqual("Metadata/plate_1.gcode", gl.gcode_member(path, None))
        self.assertEqual("Metadata/plate_1.gcode", gl.gcode_member(path, 7),
                         "нет такой плиты — берём первую, а не падаем")
        empty = self.dir / "empty.3mf"
        with zipfile.ZipFile(empty, "w") as zf:
            zf.writestr("3D/3dmodel.model", "<model/>")
        self.assertIsNone(gl.gcode_member(empty))
        (self.dir / "bad.3mf").write_bytes(b"not a zip")
        self.assertIsNone(gl.gcode_member(self.dir / "bad.3mf"))

    def test_index_for_reads_plate_from_3mf_and_caches(self):
        path = self.make_3mf()
        idx = gl.index_for(path, 2, wait=5)
        self.assertIsNotNone(idx)
        idx.ready.wait(5)
        self.assertEqual(2, len(idx.layers), "плита 2 — Cura-файл из двух слоёв")
        self.assertEqual("job.3mf:Metadata/plate_2.gcode", idx.source)
        self.assertIs(idx, gl.index_for(path, 2, wait=1), "повторный вызов — тот же индекс")
        other = gl.index_for(path, 1, wait=5)
        self.assertIsNot(idx, other, "другая плита — другой индекс")
        self.assertEqual(3, len(other.layers))

    def test_index_for_plain_gcode_and_missing_file(self):
        path = self.dir / "demo.gcode"
        path.write_text(ORCA, encoding="utf-8")
        idx = gl.index_for(path, wait=5)
        self.assertEqual(1, len(idx.layers))
        self.assertIsNone(gl.index_for(self.dir / "nope.gcode"))

    def test_cache_invalidates_when_file_changes(self):
        path = self.dir / "demo.gcode"
        path.write_text(ORCA, encoding="utf-8")
        first = gl.index_for(path, wait=5)
        time.sleep(0.01)
        path.write_text(CURA, encoding="utf-8")
        second = gl.index_for(path, wait=5)
        self.assertIsNot(first, second)
        self.assertEqual(2, len(second.layers))

    def test_cache_keeps_only_last_entries(self):
        for i in range(gl.CACHE_SIZE + 2):
            p = self.dir / f"f{i}.gcode"
            p.write_text(ORCA, encoding="utf-8")
            gl.index_for(p, wait=5)
        self.assertLessEqual(len(gl._CACHE), gl.CACHE_SIZE)

    def test_resolve_job_file_stays_inside_roots(self):
        (self.dir / "a.gcode").write_text("G1", encoding="utf-8")
        self.assertEqual(self.dir / "a.gcode", gl.resolve_job_file("a.gcode", [self.dir]))
        self.assertEqual(self.dir / "a.gcode",
                         gl.resolve_job_file("C:\\Users\\x\\a.gcode", [self.dir]),
                         "берём только имя файла — путь Studio/Windows отбрасывается")
        self.assertIsNone(gl.resolve_job_file("../a.gcode", [self.dir / "sub"]))
        self.assertIsNone(gl.resolve_job_file("", [self.dir]))
        self.assertIsNone(gl.resolve_job_file("missing.gcode", [self.dir]))


class RouteTests(unittest.TestCase):
    def setUp(self):
        import connector.printflow.routes_printers  # noqa: F401
        self.db = make_db()
        self.addCleanup(self.db.close)
        self.api = make_api(self.db)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.up = pathlib.Path(self.tmp.name) / "uploads"
        self.lib = pathlib.Path(self.tmp.name) / "library"
        self.up.mkdir()
        self.lib.mkdir()
        for target in ("connector.printflow.config.UPLOAD_DIR", "connector.printflow.library.UPLOAD_DIR"):
            patch = mock.patch(target, self.up)
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch("connector.printflow.library.LIBRARY_DIR", self.lib)
        patch.start()
        self.addCleanup(patch.stop)
        gl.clear_cache()
        self.addCleanup(gl.clear_cache)

    def get(self, path, **query):
        return router.dispatch(self.api, "GET", path, query={k: [str(v)] for k, v in query.items()})

    def test_no_printer_and_no_job_have_readable_reasons(self):
        code, data = self.get("/api/gcode/layers")
        self.assertEqual(200, code)
        self.assertFalse(data["has"])
        self.assertEqual("Не указан принтер", data["reason"])
        code, data = self.get("/api/gcode/layers", printer_id="p1")
        self.assertFalse(data["has"])
        self.assertIn("ничего не печатается", data["reason"])
        code, data = self.get("/api/gcode/layers", file="missing.gcode")
        self.assertFalse(data["has"])
        self.assertIn("не найден", data["reason"])

    def test_index_and_layer_by_file_name(self):
        (self.up / "demo.gcode").write_text(BAMBU, encoding="utf-8")
        code, data = self.get("/api/gcode/layers", file="demo.gcode")
        self.assertEqual(200, code)
        self.assertTrue(data["has"])
        self.assertFalse(data["building"])
        self.assertEqual(3, data["total"])
        self.assertEqual("demo.gcode", data["file"])
        self.assertEqual([10.0, 10.0, 60.0, 50.0], data["bbox"])
        self.assertEqual(8, len(data["buckets"]))
        self.assertEqual(["i", "z", "h", "seg", "ext", "pct", "rem", "types"],
                         list(data["layers"][0]))
        code, layer = self.get("/api/gcode/layer", file="demo.gcode", layer=2)
        self.assertEqual(200, code)
        self.assertEqual(2, layer["i"])
        self.assertEqual(3, layer["total"])
        self.assertEqual({"wall_inner", "bridge"}, set(layer["paths"]))
        code, err = self.get("/api/gcode/layer", file="demo.gcode", layer=9)
        self.assertEqual(404, code)
        self.assertIn("3 слоёв", err["error"])

    def test_library_file_is_found_too(self):
        (self.lib / "lib.gcode").write_text(ORCA, encoding="utf-8")
        code, data = self.get("/api/gcode/layers", file="lib.gcode")
        self.assertTrue(data["has"])
        self.assertEqual(1, data["total"])

    def test_running_job_of_printer_uses_its_3mf_plate(self):
        path = self.up / "box.gcode.3mf"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("Metadata/plate_1.gcode", BAMBU)
            zf.writestr("Metadata/plate_2.gcode", CURA)
        from connector.printflow.config import now_iso
        self.db.upsert("print_jobs", {"id": "j1", "printer_id": "p1", "name": "Коробка",
                                      "file": "box.gcode.3mf", "plate": 2, "state": "running",
                                      "started_at": now_iso()})
        code, data = self.get("/api/gcode/layers", printer_id="p1")
        self.assertEqual(200, code)
        self.assertTrue(data["has"], data)
        self.assertEqual(2, data["total"], "плита 2 задания — Cura-файл из двух слоёв")
        self.assertEqual("j1", data["job"]["id"])
        self.assertEqual("Коробка", data["job"]["name"])
        code, layer = self.get("/api/gcode/layer", printer_id="p1", layer=1)
        self.assertEqual(200, code)
        self.assertEqual({"wall_outer", "infill"}, set(layer["paths"]))

    def test_job_without_local_copy(self):
        from connector.printflow.config import now_iso
        self.db.upsert("print_jobs", {"id": "j2", "printer_id": "p1", "name": "С карты",
                                      "file": "sd_only.3mf", "state": "running",
                                      "started_at": now_iso()})
        code, data = self.get("/api/gcode/layers", printer_id="p1")
        self.assertFalse(data["has"])
        self.assertIn("не найден локально", data["reason"])
        self.assertEqual("j2", data["job"]["id"])
        code, err = self.get("/api/gcode/layer", printer_id="p1", layer=1)
        self.assertEqual(404, code)

    def test_3mf_without_gcode_explains_itself(self):
        path = self.up / "raw.3mf"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("3D/3dmodel.model", "<model/>")
        code, data = self.get("/api/gcode/layers", file="raw.3mf")
        self.assertFalse(data["has"])
        self.assertIn("нарезать", data["reason"])

    def test_building_state_while_parsing(self):
        (self.up / "slow.gcode").write_text(ORCA, encoding="utf-8")
        pending = gl.LayerIndex("slow.gcode")   # разбор «ещё идёт»
        with mock.patch("connector.printflow.gcode_layers.index_for",
                        return_value=pending):
            code, data = self.get("/api/gcode/layers", file="slow.gcode")
            self.assertEqual(200, code)
            self.assertTrue(data["has"])
            self.assertTrue(data["building"])
            self.assertEqual([], data["layers"])
            code, layer = self.get("/api/gcode/layer", file="slow.gcode", layer=1)
            self.assertEqual(202, code)
            self.assertTrue(layer["building"])
        # Настоящий разбор маленького файла успевает за время ожидания маршрута.
        code, data = self.get("/api/gcode/layers", file="slow.gcode")
        self.assertFalse(data["building"])
        self.assertEqual(1, data["total"])


if __name__ == "__main__":
    unittest.main()
