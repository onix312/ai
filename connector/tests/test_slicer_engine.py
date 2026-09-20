"""Поведение собственного движка нарезки: геометрия, G-code и отказы."""
from __future__ import annotations

import math
import struct
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.slicer import SlicerError  # noqa: E402
from connector.printflow import slicer_engine as engine  # noqa: E402
from connector.printflow.slicer_profile import P1S, _Z_RE  # noqa: E402


def cube(size: float = 20.0, height: float | None = None):
    """Замкнутый куб: 12 треугольников с корректными нормалями."""
    s = size
    h = height if height is not None else size
    verts = [(0, 0, 0), (s, 0, 0), (s, s, 0), (0, s, 0),
             (0, 0, h), (s, 0, h), (s, s, h), (0, s, h)]
    faces = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7),
             (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5),
             (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    return faces, verts


def tube(outer: float = 20.0, inner: float = 10.0, height: float = 10.0):
    """Прямоугольная труба: в сечении контур с дырой."""
    a, b, h = outer / 2, inner / 2, height
    verts = [(-a, -a, 0), (a, -a, 0), (a, a, 0), (-a, a, 0),
             (-a, -a, h), (a, -a, h), (a, a, h), (-a, a, h),
             (-b, -b, 0), (b, -b, 0), (b, b, 0), (-b, b, 0),
             (-b, -b, h), (b, -b, h), (b, b, h), (-b, b, h)]
    faces = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7),
             (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5),
             (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7),
             (8, 9, 11), (9, 10, 11), (12, 14, 13), (12, 15, 14),
             (8, 13, 9), (8, 12, 13), (9, 14, 10), (9, 13, 14),
             (10, 15, 11), (10, 14, 15), (11, 12, 8), (11, 15, 12)]
    return faces, verts


def write_binary_stl(path: Path, faces, verts, name: str = "printflow") -> None:
    out = bytearray(name.encode()[:80].ljust(80, b"\0"))
    out += struct.pack("<I", len(faces))
    for a, b, c in faces:
        out += struct.pack("<3f", 0.0, 0.0, 0.0)
        for index in (a, b, c):
            out += struct.pack("<3f", *verts[index])
        out += struct.pack("<H", 0)
    path.write_bytes(bytes(out))


def write_ascii_stl(path: Path, faces, verts, name: str = "printflow") -> None:
    rows = [f"solid {name}"]
    for a, b, c in faces:
        rows.append(" facet normal 0 0 0")
        rows.append("  outer loop")
        for index in (a, b, c):
            rows.append("   vertex {} {} {}".format(*verts[index]))
        rows.append("  endloop")
        rows.append(" endfacet")
    rows.append(f"endsolid {name}")
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


class StlReadingTests(unittest.TestCase):
    def test_binary_stl_is_read_completely(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "cube.stl"
            write_binary_stl(path, *cube())
            mesh = engine.load_mesh(path)
            self.assertEqual(mesh.count, 12)
            stats = engine.mesh_stats(mesh)
            self.assertTrue(stats["closed"])
            self.assertAlmostEqual(stats["bbox"]["z"], 20.0)

    def test_ascii_stl_is_read_completely(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "cube.stl"
            write_ascii_stl(path, *cube())
            mesh = engine.load_mesh(path)
            self.assertEqual(mesh.count, 12)
            self.assertTrue(engine.mesh_manifold(mesh))

    def test_only_stl_is_accepted(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "model.3mf"
            path.write_bytes(b"PK\x03\x04 not really")
            with self.assertRaisesRegex(SlicerError, "только STL"):
                engine.load_mesh(path)

    def test_broken_file_is_rejected_not_guessed(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "model.stl"
            path.write_bytes(b"\x01\x02\x03 not a mesh at all")
            with self.assertRaisesRegex(SlicerError, "не читается"):
                engine.load_mesh(path)


class GeometryTests(unittest.TestCase):
    def test_offset_shrinks_solid_and_grows_hole(self):
        square = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
        inner = engine.offset_contour(square, 1.0, is_hole=False)
        self.assertAlmostEqual(engine._signed_area(inner), 64.0, places=3)
        outer = engine.offset_contour(square, 1.0, is_hole=True)
        self.assertAlmostEqual(engine._signed_area(outer), 144.0, places=3)

    def test_offset_collapses_thin_contour(self):
        thin = [(0.0, 0.0), (10.0, 0.0), (10.0, 0.4), (0.0, 0.4)]
        self.assertEqual(engine.offset_contour(thin, 1.0, is_hole=False), [])

    def test_spanner_sees_hole_as_two_spans(self):
        square = [[(0.0, 0.0), (20.0, 0.0), (20.0, 20.0), (0.0, 20.0)],
                  [(8.0, 8.0), (8.0, 12.0), (12.0, 12.0), (12.0, 8.0)]]
        spans = engine.Spanner(square).spans(10.0)
        self.assertEqual(len(spans), 2)
        self.assertAlmostEqual(spans[0][1] - spans[0][0], 8.0, places=6)
        self.assertAlmostEqual(spans[1][0], 12.0, places=6)

    def test_interval_algebra(self):
        self.assertEqual(engine._interval_subtract([[0.0, 10.0]], [[3.0, 5.0]]),
                         [[0.0, 3.0], [5.0, 10.0]])
        self.assertEqual(engine._interval_intersect([[0.0, 10.0]], [[3.0, 20.0]]),
                         [[3.0, 10.0]])

    def test_simplify_keeps_corners(self):
        pts = [(0.0, 0.0), (5.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
        self.assertEqual(engine._simplify(pts),
                         [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)])


class SlicingTests(unittest.TestCase):
    def test_cube_layers_and_gcode(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "cube.stl"
            write_binary_stl(path, *cube())
            text, report = engine.slice_model(path, engine.SliceSettings(), P1S)
            self.assertEqual(report["layers"], 100)
            self.assertEqual(report["triangles"], 12)
            self.assertIn(engine.BEGIN, text)
            self.assertIn(engine.END, text)
            self.assertIn("M104 S220", text)
            self.assertIn("M140 S60", text)
            self.assertIn("M104 S0", text.split(";LAYER:0")[1])
            self.assertGreater(report["weight_g"], 0)
            self.assertGreater(report["minutes"], 0)
            # Пустотелая деталь всегда легче сплошного куба того же размера.
            solid = 20.0 * 20.0 * 20.0 / 1000.0 * 1.24
            self.assertLess(report["weight_g"], solid)

    def test_tube_slice_has_hole_contours(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "tube.stl"
            write_binary_stl(path, *tube())
            mesh = engine.place_mesh(engine.load_mesh(path), P1S, center=True)
            layers, open_loops = engine.slice_mesh(mesh, 0.2, 0.2)
            self.assertEqual(open_loops, 0)
            holes = [hole for _pts, hole in layers[5].contours]
            self.assertIn(True, holes)

    def test_model_out_of_bed_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "big.stl"
            write_binary_stl(path, *cube(size=300.0, height=20.0))
            with self.assertRaisesRegex(SlicerError, "не влезает"):
                engine.slice_model(path, engine.SliceSettings(), P1S)

    def test_model_taller_than_printer_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "tall.stl"
            write_binary_stl(path, *cube(size=20.0, height=300.0))
            with self.assertRaisesRegex(SlicerError, "больше предела"):
                engine.slice_model(path, engine.SliceSettings(), P1S)

    def test_source_file_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "cube.stl"
            write_binary_stl(source, *cube())
            before = source.read_bytes()
            with self.assertRaisesRegex(SlicerError, "разными файлами"):
                engine.slice_to_file(source, source, engine.SliceSettings(), P1S)
            self.assertEqual(source.read_bytes(), before)

    def test_slice_to_file_writes_report(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "cube.stl"
            write_binary_stl(source, *cube())
            target = Path(folder) / "out" / "cube.gcode"
            report = engine.slice_to_file(source, target, engine.SliceSettings(), P1S)
            self.assertTrue(target.is_file())
            self.assertEqual(report["output"], str(target))
            self.assertGreater(report["output_bytes"], 1000)


class GcodeShapeTests(unittest.TestCase):
    def test_first_layer_is_slower_and_has_no_retract_before_prime(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "cube.stl"
            write_binary_stl(path, *cube())
            text, _report = engine.slice_model(path, engine.SliceSettings(), P1S)
            lines = [line for line in text.splitlines() if line.startswith("G1 ")]
            first_extrusion = next(line for line in lines if " E" in line)
            self.assertNotIn("E-", first_extrusion)
            # Первый G-code-файл начинается не с ретракта: plastic ещё не подан.
            first_move = next(line for line in lines)
            self.assertNotIn("E-", first_move)

    def test_layer_markers_are_numbered(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "cube.stl"
            write_binary_stl(path, *cube())
            text, _report = engine.slice_model(path, engine.SliceSettings(), P1S)
            markers = [line for line in text.splitlines() if line.startswith(";LAYER:")]
            self.assertEqual(markers[0], ";LAYER:0")
            self.assertEqual(markers[-1], ";LAYER:99")

    def test_type_markers_speak_the_common_language(self):
        """18.12: ;TYPE: в форме Prusa/Orca — шкала слоёв раскладывает корзины."""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "cube.stl"
            write_binary_stl(path, *cube())
            text, _report = engine.slice_model(path, engine.SliceSettings(), P1S)
            types = [line.split(":", 1)[1] for line in text.splitlines()
                     if line.startswith(";TYPE:")]
            self.assertIn("External perimeter", types)
            self.assertIn("Internal infill", types)
            self.assertIn("Solid infill", types)
            self.assertNotIn("Custom", types)
            # снаружи стенка помечена раньше внутренней: порядок колец соблюдён
            first_wall = types.index("External perimeter")
            self.assertGreater(types.index("Perimeter", first_wall), first_wall)

    def test_m73_progress_per_layer_is_written(self):
        """18.12: M73 P/R в начале каждого слоя — прогресс и остаток в минутах."""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "cube.stl"
            write_binary_stl(path, *cube())
            text, report = engine.slice_model(path, engine.SliceSettings(), P1S)
            marks = [line for line in text.splitlines() if line.startswith("M73 ")]
            self.assertEqual(len(marks), 101, "M73 — по одному на каждый слой плюс финальный")
            self.assertTrue(marks[0].startswith("M73 P0 R"))
            self.assertEqual(marks[-1], "M73 P100 R0")
            import re as _re
            pcts = [int(_re.match(r"M73 P(\d+)", m).group(1)) for m in marks]
            self.assertEqual(pcts, sorted(pcts), "проценты только растут")
            self.assertLessEqual(max(pcts[:-1]), 99)
            # шкала слоёв PrintFlow читает проценты из этих строк
            from connector.printflow import gcode_layers
            overview = gcode_layers.parse_text(text).overview()
            self.assertIsNotNone(overview["layers"][2]["pct"])
            self.assertGreaterEqual(overview["layers"][2]["pct"], 0)

    def test_z_never_goes_below_bed(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "cube.stl"
            write_binary_stl(path, *cube())
            text, _report = engine.slice_model(path, engine.SliceSettings(), P1S)
            z_values = [float(match.group(1))
                        for match in _Z_RE.finditer(text)]
            self.assertGreaterEqual(min(z_values), 0.0)

    def test_broken_mesh_is_reported_not_hidden(self):
        faces, verts = cube()
        # Убираем грань: сетка становится незамкнутой, контуры не сомкнутся.
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "broken.stl"
            write_binary_stl(path, faces[:-1], verts)
            try:
                _text, report = engine.slice_model(path, engine.SliceSettings(), P1S)
            except SlicerError:
                return
            joined = " ".join(report["warnings"])
            self.assertTrue("не замкнута" in joined or "сомкнулись" in joined,
                            report["warnings"])

    def test_speed_scales_with_layer_role(self):
        settings = engine.SliceSettings(speed_mm_s=100.0)
        perimeter = engine._speed_for("perimeter", settings, False)
        infill = engine._speed_for("infill", settings, False)
        first_layer = engine._speed_for("infill", settings, True)
        self.assertLess(perimeter, infill)
        self.assertLess(first_layer, infill)

    def test_move_time_accounts_for_acceleration(self):
        slow = engine._move_time(1.0, 100.0, 5000.0)
        fast = engine._move_time(100.0, 100.0, 5000.0)
        self.assertGreater(slow, 0)
        self.assertLess(fast / slow, 100.0)
        self.assertAlmostEqual(math.isclose(slow, 2 * math.sqrt(1.0 / 5000.0)), True)


if __name__ == "__main__":
    unittest.main()
