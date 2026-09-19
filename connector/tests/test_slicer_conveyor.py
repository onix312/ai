"""Слайсер ↔ конвейер + пластик со склада (18.8, срез 6).

Владелец: «увеличить взаимодействие панели со слайсером… возможное
изменение g-кода для фарм-луп… интегрировать пластик со склада». Срез
держит три потока:

1. Нарезка карточки «Свой слайсер» умеет сразу собрать FarmLoop-файл
   (явный профиль или авто-постобработка из настроек — второй путь был
   сломан: пустое поле body перечитывали вместо настроек).
2. Катушка склада пластика задаётся id: нарезка берёт её материал и
   AMS-слот, а задание потом списывает расход именно с этой бобины.
3. Кнопка «В конвейер» ставит серию: N одинаковых заданий одной
   транзакцией (manager.enqueue с `cycles`) + событие в историю
   конвейера (kind=farmloop).

Маршруты запускаются по-настоящему на временной базе; UPLOAD_DIR и
DATA_DIR подменяются на temp-каталог, чтобы не трогать боевые данные.
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys_path_fix = str(ROOT)
if sys_path_fix not in sys.path:
    sys.path.insert(0, sys_path_fix)
    sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow import farmloop  # noqa: E402
from connector.printflow import routes_slicer  # noqa: E402
from connector.printflow import router as router_module  # noqa: E402
from connector.printflow.api import Api  # noqa: E402
from connector.printflow.manager import PrinterManager  # noqa: E402
from connector.printflow.repo import Repo  # noqa: E402
from connector.tests.test_phase11 import make_db  # noqa: E402
from connector.tests.test_slicer_engine import cube, write_binary_stl  # noqa: E402


TEMPLATE = (
    "; FarmLoop Stage 1 test template\n"
    "; PRINTFLOW FARMLOOP BEGIN\n"
    "G91\n"
    "G1 Z5 F3000\n"
    "G1 X0 Y0\n"
    "; PRINTFLOW FARMLOOP END\n"
)


def install_template(data_dir: pathlib.Path, profile: str = "bambu-p1s-farmloop-stage1") -> None:
    folder = data_dir / "farmloop-templates"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{profile}.gcode").write_text(TEMPLATE, encoding="utf-8")


class SlicerConveyorSliceTests(unittest.TestCase):
    """/api/slicer/slice: FarmLoop-обвязка, авто-постобработка, катушка."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = pathlib.Path(self.tmp.name)
        self.uploads = base / "uploads"
        self.data = base / "data"
        self.uploads.mkdir(parents=True)
        self.data.mkdir(parents=True)

        self.db = make_db()
        self.addCleanup(self.db.close)
        self.repo = Repo(self.db)

        self.api = Api.__new__(Api)
        self.api.db = self.db
        self.api.repo = self.repo
        self.api.manager = None  # в-очередь из нарезки не трогаем: отдельный маршрут

        router_module.register_all()

        self._uploads = routes_slicer.UPLOAD_DIR
        self._data = routes_slicer.DATA_DIR
        self._farm_data = farmloop.DATA_DIR
        # FileLibrary.put кладёт копию в library.UPLOAD_DIR / LIBRARY_DIR —
        # свои модульные константы: без патча тест пачкает боевые uploads.
        from connector.printflow import library as library_module
        self._lib_uploads = library_module.UPLOAD_DIR
        self._lib_dir = library_module.LIBRARY_DIR
        routes_slicer.UPLOAD_DIR = self.uploads
        routes_slicer.DATA_DIR = self.data
        farmloop.DATA_DIR = self.data
        library_module.UPLOAD_DIR = self.uploads
        library_module.LIBRARY_DIR = self.data / "library"
        self.addCleanup(self._restore_dirs)

        faces, verts = cube(20.0)
        self.stl = self.uploads / "cube.stl"
        write_binary_stl(self.stl, faces, verts, name="cube")

    def _restore_dirs(self):
        from connector.printflow import library as library_module
        routes_slicer.UPLOAD_DIR = self._uploads
        routes_slicer.DATA_DIR = self._data
        farmloop.DATA_DIR = self._farm_data
        library_module.UPLOAD_DIR = self._lib_uploads
        library_module.LIBRARY_DIR = self._lib_dir

    def _slice(self, body: dict):
        return router_module.router.dispatch(self.api, "POST", "/api/slicer/slice", body=body)

    def test_plain_slice_is_not_farmloop(self):
        code, body = self._slice({"file": "cube.stl"})
        self.assertEqual(code, 200, body)
        self.assertTrue(body["ok"])
        self.assertEqual(body["output"], "cube.gcode")
        self.assertIsNone(body["farmloop"])
        self.assertIsNone(body["spool"])

    def test_farmloop_slice_with_template(self):
        install_template(self.data)
        code, body = self._slice({"file": "cube.stl", "farmloop_profile": "bambu-p1s-farmloop-stage1"})
        self.assertEqual(code, 200, body)
        self.assertTrue(body["farmloop"], "нет отчёта FarmLoop-подготовки")
        self.assertEqual(body["output"], "cube.farmloop.gcode")
        self.assertEqual(body["farmloop_profile"], "bambu-p1s-farmloop-stage1")
        # Событие в историю конвейера (вкладка «Конвейер» → Журнал).
        events = self.db.query("SELECT * FROM events WHERE kind='farmloop'")
        self.assertTrue(events, "нарезка для конвейера не оставила события")
        self.assertIn("Нарезка подготовлена для конвейера", events[-1]["title"])

    def test_farmloop_without_template_is_blocked(self):
        code, body = self._slice({"file": "cube.stl", "farmloop_profile": "bambu-p1s-farmloop-stage1"})
        self.assertEqual(code, 400)
        self.assertIn("FarmLoop-шаблон", body["error"])

    def test_auto_postprocess_uses_profile_from_settings(self):
        """18.8 фикс: включённый авто-постобработчик брал профиль из
        настроек, а не перечитывал пустое поле body."""
        install_template(self.data)
        self.db.upsert("settings", {"key": "slicer_auto_postprocess_farmloop", "value": "1"}, key="key")
        code, body = self._slice({"file": "cube.stl"})
        self.assertEqual(code, 200, body)
        self.assertEqual(body["output"], "cube.farmloop.gcode",
                         "авто-постобработка не включилась из настроек")

    def test_spool_from_warehouse_travels_with_slice(self):
        """Катушка со склада: материал и AMS-слот уходят в нарезку,
        id — в отчёт и событие; расхода по ней ещё нет (печать впереди)."""
        install_template(self.data)
        self.db.upsert("spools", {
            "id": "sp_test", "material": "PETG", "color_name": "чёрный",
            "total_grams": 1000, "remaining_grams": 700, "price": 1800,
            "ams_slot": "2", "archived": 0,
        })
        code, body = self._slice({
            "file": "cube.stl", "farmloop_profile": "bambu-p1s-farmloop-stage1",
            "spool_id": "sp_test",
        })
        self.assertEqual(code, 200, body)
        self.assertEqual(body["output"], "cube.farmloop.gcode")
        self.assertEqual(body["spool"]["id"], "sp_test")
        self.assertEqual(body["spool"]["ams_slot"], "2")
        # Катушка записана в заголовок FarmLoop-файла (метаданные серии).
        out = (self.uploads / body["output"]).read_text(encoding="utf-8")
        self.assertIn("; spool: sp_test", out)

    def test_unknown_spool_is_rejected_before_slicing(self):
        code, body = self._slice({"file": "cube.stl", "spool_id": "sp_missing"})
        self.assertEqual(code, 400)
        self.assertIn("не найдена на складе", body["error"])
        self.assertFalse((self.uploads / "cube.gcode").exists(),
                         "нарезка не должна завершаться, если катушка не найдена")


class SlicerConveyorProfileTests(unittest.TestCase):
    """/api/slicer/profile отдаёт конвейерные гейты карточке слайсера."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = pathlib.Path(self.tmp.name)
        self.data = base / "data"
        self.data.mkdir(parents=True)

        self.db = make_db()
        self.addCleanup(self.db.close)
        self.api = Api.__new__(Api)
        self.api.db = self.db
        router_module.register_all()

        self._data = routes_slicer.DATA_DIR
        self._farm_data = farmloop.DATA_DIR
        routes_slicer.DATA_DIR = self.data
        farmloop.DATA_DIR = self.data
        self.addCleanup(self._restore)

    def _restore(self):
        routes_slicer.DATA_DIR = self._data
        farmloop.DATA_DIR = self._farm_data

    def _profile(self):
        code, body = router_module.router.dispatch(self.api, "GET", "/api/slicer/profile", query={})
        self.assertEqual(code, 200)
        return body["farmloop"]

    def test_gate_reports_missing_template(self):
        gate = self._profile()
        self.assertFalse(gate["can_prepare"])
        self.assertFalse(gate["can_series"])
        self.assertIn("шаблон", gate["blocked_reason"].lower())

    def test_gate_opens_with_template_and_clearances(self):
        install_template(self.data)
        self.db.upsert("settings", {"key": "farmloop_mechanics_verified", "value": "1"}, key="key")
        self.db.upsert("settings", {"key": "farmloop_template_verified", "value": "1"}, key="key")
        self.db.upsert("settings", {"key": "farmloop_pusher_enabled", "value": "1"}, key="key")
        self.db.upsert("settings", {"key": "farmloop_bender_enabled", "value": "1"}, key="key")
        self.db.upsert("settings", {"key": "farmloop_sensor_mode", "value": "camera"}, key="key")
        gate = self._profile()
        self.assertTrue(gate["can_prepare"])
        self.assertTrue(gate["can_series"])
        self.assertEqual(gate["blocked_reason"], "")
        self.assertEqual(gate["profile_id"], "bambu-p1s-farmloop-stage1")


class ConveyorSeriesEnqueueTests(unittest.TestCase):
    """manager.enqueue с `cycles`: серия конвейера — N заданий одной
    транзакцией + событие в историю конвейера."""

    def setUp(self):
        self.db = make_db()
        self.addCleanup(self.db.close)
        self.repo = Repo(self.db)
        self.manager = PrinterManager(self.db, self.repo)
        # Серии в тестах без автостарта и без реальных файлов: оценки не
        # считаются, задания честно стоят в очереди.
        self.db.upsert("settings", {"key": "auto_queue", "value": "0"}, key="key")

    def _jobs(self):
        return self.db.query(
            "SELECT * FROM print_jobs ORDER BY datetime(created_at), id")

    def test_series_creates_n_jobs_and_event(self):
        job = self.manager.enqueue({
            "file": "cube.farmloop.gcode", "name": "cube",
            "source": "printflow-conveyor", "cycles": 3,
            "spool_id": "sp_test", "material": "PETG",
            "no_auto": 1,
        })
        self.assertEqual(job["series"]["cycles"], 3)
        self.assertEqual(len(job["series"]["job_ids"]), 3)
        jobs = self._jobs()
        self.assertEqual(len(jobs), 3)
        for one in jobs:
            self.assertEqual(one["state"], "queued")
            self.assertEqual(one["source"], "printflow-conveyor")
            self.assertEqual(one["spool_id"], "sp_test")
        events = self.db.query("SELECT * FROM events WHERE kind='farmloop'")
        self.assertEqual(len(events), 1)
        self.assertIn("Серия конвейера", events[0]["title"])
        import json as _json
        self.assertEqual(_json.loads(events[0]["data"])["cycles"], 3)

    def test_single_enqueue_is_unchanged(self):
        """`cycles` не задан — старое поведение: одно задание, без события
        конвейера и без ключа series."""
        job = self.manager.enqueue({
            "file": "cube.gcode", "name": "cube",
            "source": "printflow-slicer", "no_auto": 1,
        })
        self.assertNotIn("series", job)
        self.assertEqual(len(self._jobs()), 1)
        self.assertFalse(self.db.query("SELECT * FROM events WHERE kind='farmloop'"))

    def test_cycles_is_clamped(self):
        job = self.manager.enqueue({
            "file": "cube.farmloop.gcode", "name": "cube",
            "source": "printflow-conveyor", "cycles": 500, "no_auto": 1,
        })
        self.assertEqual(job["series"]["cycles"], 100)
        self.assertEqual(len(self._jobs()), 100)
        job = self.manager.enqueue({
            "file": "cube.farmloop.gcode", "name": "cube2",
            "source": "printflow-conveyor", "cycles": 0, "no_auto": 1,
        })
        self.assertNotIn("series", job)
        self.assertEqual(len(self._jobs()), 101)


if __name__ == "__main__":
    unittest.main()
