"""Реестр 3D-моделей PrintFlow 6.0: хранение, версии, привязка к изделиям.

Модель ≠ изделие. Одна модель (STL/3MF) может использоваться в нескольких
изделиях (разные материалы, цвета, профили), а одно изделие может состоять
из нескольких моделей (сборка).

Реестр решает:
    • где лежит STL/3MF файл и какая у него версия;
    • сколько штук влезает на плиту (раскладка);
    • сколько времени занимает подготовка (для калькулятора);
    • кто и когда последний раз печатал эту модель.

Подготовка модели — это отдельный этап работы:
    1. найти/скачать модель;
    2. открыть в слайсере, ориентировать;
    3. размножить на плите;
    4. настроить профиль печати;
    5. добавить поддержки;
    6. нарезать и проверить;
    7. загрузить на принтер.
Каждый этап занимает время, которое учитывается в себестоимости.
"""
from __future__ import annotations

import math
from typing import Any

from .accounting import num, uid
from .config import now_iso
from .db import Database

# Площадь плиты Bambu Lab P1S/X1C: 256 × 256 мм.
# С учётом полей (брим, зазоры) рабочая площадь ~240 × 240 мм.
PLATE_SIZE_MM = (256, 256)
PLATE_USABLE_MM = (240, 240)
# Зазор между деталями на плите (мм) — минимум для снятия.
GAP_MM = 3.0

# Типовое время подготовки модели (минуты) по этапам.
# Эти значения используются для автооценки, когда реальных данных нет.
DEFAULT_PREP_TIMES: dict[str, dict[str, float]] = {
    "simple": {
        "find": 5,       # найти/скачать модель
        "orient": 2,     # ориентировать на столе
        "duplicate": 2,  # размножить
        "profile": 2,    # выбрать профиль
        "supports": 2,   # добавить поддержки
        "slice": 3,      # нарезать и проверить
    },
    "medium": {
        "find": 10,
        "orient": 5,
        "duplicate": 3,
        "profile": 5,
        "supports": 10,
        "slice": 5,
    },
    "complex": {
        "find": 20,
        "orient": 10,
        "duplicate": 5,
        "profile": 10,
        "supports": 20,
        "slice": 10,
    },
}

# Названия этапов для UI.
PREP_STAGE_NAMES: dict[str, str] = {
    "find": "Поиск / скачивание модели",
    "orient": "Ориентация на столе",
    "duplicate": "Размножение на плите",
    "profile": "Выбор профиля печати",
    "supports": "Настройка поддержек",
    "slice": "Нарезка и проверка",
}


class ModelRegistry:
    """Реестр 3D-моделей: хранение, раскладка, трекинг подготовки."""

    def __init__(self, db: Database):
        self.db = db

    # --------------------------------------------------------------- схема
    def ensure_schema(self) -> None:
        """Создать таблицы реестра, если их нет."""
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS models (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                file TEXT,
                file_3mf TEXT,
                version TEXT DEFAULT '1.0',
                source TEXT,
                license TEXT,
                nom_id TEXT,
                complexity TEXT DEFAULT 'simple',
                dim_x REAL, dim_y REAL, dim_z REAL,
                weight_grams REAL,
                fit_per_plate INTEGER DEFAULT 1,
                prep_minutes REAL,
                prep_stages TEXT,
                notes TEXT,
                created_at TEXT,
                updated_at TEXT,
                archived INTEGER DEFAULT 0
            )
        """)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS model_versions (
                id TEXT PRIMARY KEY,
                model_id TEXT NOT NULL,
                version TEXT NOT NULL,
                file TEXT,
                file_3mf TEXT,
                note TEXT,
                at TEXT
            )
        """)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS model_prep_sessions (
                id TEXT PRIMARY KEY,
                model_id TEXT,
                nom_id TEXT,
                order_id TEXT,
                started_at TEXT,
                finished_at TEXT,
                duration_minutes REAL,
                stages TEXT,
                result TEXT,
                note TEXT
            )
        """)

    # ---------------------------------------------------------- раскладка
    def plate_layout(self, dim_x: float, dim_y: float,
                     gap: float = GAP_MM,
                     plate_w: float = 0.0, plate_h: float = 0.0) -> dict:
        """Сколько штук влезет на плиту (простая прямоугольная раскладка).

        dim_x, dim_y — габариты модели в мм (берём максимум по X и Y,
        потому что модель может быть повёрнута).
        gap — зазор между деталями.
        """
        pw = plate_w or PLATE_USABLE_MM[0]
        ph = plate_h or PLATE_USABLE_MM[1]
        dx, dy = max(1.0, num(dim_x)), max(1.0, num(dim_y))
        # Прямая раскладка
        cols_a = max(1, int((pw + gap) / (dx + gap)))
        rows_a = max(1, int((ph + gap) / (dy + gap)))
        fit_a = cols_a * rows_a
        # Повёрнутая на 90° (для прямоугольных моделей)
        cols_b = max(1, int((pw + gap) / (dy + gap)))
        rows_b = max(1, int((ph + gap) / (dx + gap)))
        fit_b = cols_b * rows_b
        # Берём лучший вариант
        if fit_b > fit_a:
            cols, rows, fit = cols_b, rows_b, fit_b
            rotated = True
        else:
            cols, rows, fit = cols_a, rows_a, fit_a
            rotated = False
        used_w = cols * dx + (cols - 1) * gap
        used_h = rows * dy + (rows - 1) * gap
        return {
            "fit_per_plate": fit,
            "cols": cols,
            "rows": rows,
            "rotated": rotated,
            "used_mm": (round(used_w, 1), round(used_h, 1)),
            "plate_mm": (pw, ph),
            "gap_mm": gap,
            "utilization_pct": round(used_w * used_h / (pw * ph) * 100, 1),
            "svg": self._layout_svg(cols, rows, dx, dy, gap, pw, ph, rotated),
        }

    def _layout_svg(self, cols: int, rows: int, dx: float, dy: float,
                    gap: float, pw: float, ph: float, rotated: bool) -> str:
        """SVG-превью раскладки на плите."""
        scale = 0.8
        sw = pw * scale + 20
        sh = ph * scale + 20
        rects = []
        for r in range(rows):
            for c in range(cols):
                x = 10 + c * (dx + gap) * scale
                y = 10 + r * (dy + gap) * scale
                w = dx * scale
                h = dy * scale
                rects.append(
                    f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}"'
                    f' rx="2" fill="#4f46e5" opacity="0.7"/>')
        count = cols * rows
        label = f"{count} шт" + (" (повёрнуты)" if rotated else "")
        return (
            f'<svg viewBox="0 0 {sw:.0f} {sh:.0f}" xmlns="http://www.w3.org/2000/svg">'
            f'<rect x="10" y="10" width="{pw * scale:.0f}" height="{ph * scale:.0f}"'
            f' rx="4" fill="none" stroke="#64748b" stroke-width="1.5" stroke-dasharray="4"/>'
            f'{"".join(rects)}'
            f'<text x="{sw / 2}" y="{sh - 2}" text-anchor="middle"'
            f' font-size="10" fill="#64748b">{label}</text>'
            f'</svg>'
        )

    # ---------------------------------------------------------- подготовка
    def estimate_prep_time(self, complexity: str = "simple",
                           stages: dict | None = None) -> dict[str, float]:
        """Оценка времени подготовки модели по этапам.

        Если передан stages — используются его значения, иначе дефолты.
        """
        base = DEFAULT_PREP_TIMES.get(complexity, DEFAULT_PREP_TIMES["simple"])
        result = {}
        for stage, default in base.items():
            if stages and stage in stages:
                result[stage] = max(0.0, num(stages[stage]))
            else:
                result[stage] = default
        result["_total"] = round(sum(result.values()), 1)
        return result

    def start_prep_session(self, model_id: str = "", nom_id: str = "",
                           order_id: str = "") -> dict:
        """Начать сессию подготовки модели — засекаем время."""
        session = {
            "id": uid("prep"),
            "model_id": model_id or None,
            "nom_id": nom_id or None,
            "order_id": order_id or None,
            "started_at": now_iso(),
            "stages": "{}",
            "result": "in_progress",
        }
        self.db.upsert("model_prep_sessions", session)
        return session

    def finish_prep_session(self, session_id: str, stages: dict | None = None,
                            note: str = "") -> dict:
        """Завершить сессию: записать время каждого этапа и общее время."""
        session = self.db.one(
            "SELECT * FROM model_prep_sessions WHERE id=?", (session_id,))
        if not session:
            raise ValueError("Сессия не найдена")
        from datetime import datetime
        started = datetime.fromisoformat(session["started_at"])
        finished = datetime.now().astimezone()
        duration = round((finished - started).total_seconds() / 60, 1)
        import json
        stages_data = stages or {}
        total = sum(num(v) for v in stages_data.values()) or duration
        self.db.execute(
            "UPDATE model_prep_sessions SET finished_at=?, duration_minutes=?,"
            " stages=?, result='done', note=? WHERE id=?",
            (now_iso(), total, json.dumps(stages_data, ensure_ascii=False),
             note, session_id))
        # Обновить модель: записать фактическое время подготовки
        if session.get("model_id"):
            self.db.execute(
                "UPDATE models SET prep_minutes=?, prep_stages=?, updated_at=?"
                " WHERE id=?",
                (total, json.dumps(stages_data, ensure_ascii=False),
                 now_iso(), session["model_id"]))
        return self.db.one(
            "SELECT * FROM model_prep_sessions WHERE id=?", (session_id,)) or {}

    # ------------------------------------------------------------ CRUD
    def list(self, search: str = "", nom_id: str = "",
             include_archived: bool = False) -> list[dict]:
        sql = "SELECT * FROM models WHERE 1=1"
        params: list[Any] = []
        if not include_archived:
            sql += " AND archived=0"
        if nom_id:
            sql += " AND nom_id=?"
            params.append(nom_id)
        if search:
            like = f"%{search.lower()}%"
            sql += " AND pylower(name) LIKE ?"
            params.append(like)
        sql += " ORDER BY updated_at DESC"
        rows = self.db.query(sql, params)
        for row in rows:
            row["prep"] = self._parse_prep(row)
            row["layout"] = self._calc_layout(row)
            row["history"] = self._prep_history(row["id"])
        return rows

    def get(self, model_id: str) -> dict | None:
        row = self.db.one("SELECT * FROM models WHERE id=?", (model_id,))
        if not row:
            return None
        row["prep"] = self._parse_prep(row)
        row["layout"] = self._calc_layout(row)
        row["versions"] = self.db.query(
            "SELECT * FROM model_versions WHERE model_id=? ORDER BY at DESC",
            (model_id,))
        row["history"] = self._prep_history(model_id)
        row["prints"] = self._print_history(model_id)
        return row

    def save(self, data: dict) -> dict:
        data = dict(data)
        new = not data.get("id")
        if new:
            data["id"] = uid("mdl")
            data["created_at"] = now_iso()
        data["updated_at"] = now_iso()
        if not (data.get("name") or "").strip():
            raise ValueError("Укажите название модели")
        # Авто-раскладка если есть габариты
        if data.get("dim_x") and data.get("dim_y"):
            layout = self.plate_layout(num(data["dim_x"]), num(data["dim_y"]))
            if not data.get("fit_per_plate"):
                data["fit_per_plate"] = layout["fit_per_plate"]
        # Авто-оценка подготовки если не указана
        if not data.get("prep_minutes"):
            complexity = data.get("complexity", "simple")
            prep = self.estimate_prep_time(complexity)
            data["prep_minutes"] = prep["_total"]
            import json
            data["prep_stages"] = json.dumps(
                {k: v for k, v in prep.items() if k != "_total"},
                ensure_ascii=False)
        row = self.db.upsert("models", data)
        # Записать версию если изменился файл
        if data.get("file"):
            self._add_version(row)
        self.db.add_event(
            "model", "Модель создана" if new else "Модель обновлена",
            row.get("name", ""), data={"model_id": row["id"]})
        return self.get(row["id"]) or row

    def delete(self, model_id: str) -> None:
        self.db.execute("UPDATE models SET archived=1, updated_at=? WHERE id=?",
                        (now_iso(), model_id))

    def _add_version(self, model: dict) -> None:
        """Записать версию модели (если файл изменился)."""
        last = self.db.one(
            "SELECT file FROM model_versions WHERE model_id=? ORDER BY at DESC LIMIT 1",
            (model["id"],))
        if last and last.get("file") == model.get("file"):
            return
        self.db.upsert("model_versions", {
            "id": uid("mv"),
            "model_id": model["id"],
            "version": model.get("version", "1.0"),
            "file": model.get("file"),
            "file_3mf": model.get("file_3mf"),
            "at": now_iso(),
        })

    def _parse_prep(self, row: dict) -> dict:
        import json
        try:
            stages = json.loads(row.get("prep_stages") or "{}")
        except Exception:
            stages = {}
        return {
            "total": num(row.get("prep_minutes")),
            "stages": stages,
            "stage_names": PREP_STAGE_NAMES,
        }

    def _calc_layout(self, row: dict) -> dict | None:
        dx = num(row.get("dim_x"))
        dy = num(row.get("dim_y"))
        if not dx or not dy:
            return None
        return self.plate_layout(dx, dy)

    def _prep_history(self, model_id: str) -> list[dict]:
        return self.db.query(
            "SELECT * FROM model_prep_sessions WHERE model_id=?"
            " ORDER BY datetime(started_at) DESC LIMIT 20", (model_id,))

    def _print_history(self, model_id: str) -> list[dict]:
        """История печатей этой модели (из журнала заданий)."""
        model = self.db.one("SELECT file FROM models WHERE id=?", (model_id,))
        if not model or not model.get("file"):
            return []
        return self.db.query(
            "SELECT * FROM print_jobs WHERE file=? AND state IN ('done','failed')"
            " ORDER BY datetime(finished_at) DESC LIMIT 20", (model["file"],))

    # ----------------------------------------------------------- статистика
    def stats(self) -> dict[str, Any]:
        """Общая статистика по реестру моделей."""
        total = self.db.one("SELECT COUNT(*) n FROM models WHERE archived=0") or {}
        with_nom = self.db.one(
            "SELECT COUNT(*) n FROM models WHERE archived=0 AND nom_id IS NOT NULL"
            " AND nom_id<>''") or {}
        avg_prep = self.db.one(
            "SELECT COALESCE(AVG(prep_minutes),0) v FROM models"
            " WHERE archived=0 AND prep_minutes>0") or {}
        sessions = self.db.one(
            "SELECT COUNT(*) n, COALESCE(AVG(duration_minutes),0) avg"
            " FROM model_prep_sessions WHERE result='done'") or {}
        return {
            "total_models": int(num(total.get("n"))),
            "linked_to_products": int(num(with_nom.get("n"))),
            "avg_prep_minutes": round(num(avg_prep.get("v")), 1),
            "prep_sessions": int(num(sessions.get("n"))),
            "avg_session_minutes": round(num(sessions.get("avg")), 1),
        }

    # ------------------------------------------- повторная печать из 3MF
    def repeat_from_model(self, model_id: str, qty: int = 1,
                          printer_id: str = "") -> dict:
        """Повторная печать: взять 3MF модели и запустить партию."""
        model = self.get(model_id)
        if not model:
            raise ValueError("Модель не найдена")
        file_3mf = model.get("file_3mf") or model.get("file")
        if not file_3mf:
            raise ValueError("У модели нет файла для печати")
        fit = max(1, int(num(model.get("fit_per_plate"), 1)))
        plates = int(math.ceil(qty / fit))
        return {
            "model_id": model_id,
            "model_name": model.get("name"),
            "file": file_3mf,
            "qty": qty,
            "fit_per_plate": fit,
            "plates": plates,
            "printer_id": printer_id,
            "prep_minutes": num(model.get("prep_minutes")),
        }

    def clone_batch(self, batch_id: str, new_qty: int = 0) -> dict:
        """Клонировать партию: взять параметры прошлой партии для новой."""
        batch = self.db.one("SELECT * FROM batches WHERE id=?", (batch_id,))
        if not batch:
            raise ValueError("Партия не найдена")
        qty = new_qty or int(num(batch.get("qty_planned"), 1))
        return {
            "source_batch_id": batch_id,
            "nom_id": batch.get("nom_id"),
            "qty": qty,
            "fit_per_plate": int(num(batch.get("fit_per_plate"), 1)),
            "file": batch.get("file") or "",
            "material": batch.get("material") or "",
            "mode": batch.get("mode") or "full",
            "printer_id": batch.get("printer_id") or "",
        }
