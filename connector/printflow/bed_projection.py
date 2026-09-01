"""Проекция карты плиты на живой кадр камеры (идея 119).

Бедняцкая AR без единой библиотеки: калибровка — четыре клика по углам
стола на эталонном кадре («Пустой стол»), дальше гомография переводит
координаты плиты из 3MF в доли кадра. Панель рисует контуры объектов
поверх видео; клик по объекту предлагает исключить его из печати.

Координаты: объекты из plate_map_3mf живут в мм плиты с началом в
левом-переднем углу (x вправо, y вглубь). Кадр описывается нормированными
долями 0..1, поэтому проекция не зависит от разрешения камеры.

Порядок калибровочных точек фиксирован: передний-левый, передний-правый,
задний-правий, задний-левый (как обход по часовой стрелке сверху).
"""
from __future__ import annotations

import json
from typing import Any, Sequence

Point = tuple[float, float]

# Углы плиты в её собственных координатах — в порядке калибровки.
PLATE_CORNERS_IN_ORDER = "front-left, front-right, back-right, back-left"


def _solve8(a: list[list[float]], b: list[float]) -> list[float] | None:
    """Гаусс для системы 8×8. None — система вырождена (плохая калибровка)."""
    n = 8
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[pivot][col]) < 1e-12:
            return None
        m[col], m[pivot] = m[pivot], m[col]
        div = m[col][col]
        for k in range(col, n + 1):
            m[col][k] /= div
        for r in range(n):
            if r == col:
                continue
            factor = m[r][col]
            if factor == 0.0:
                continue
            for k in range(col, n + 1):
                m[r][k] -= factor * m[col][k]
    return [m[i][n] for i in range(n)]


def homography(src: Sequence[Point], dst: Sequence[Point]) -> list[list[float]] | None:
    """Гомография 3×3: src (4 точки) → dst (4 точки). None — точки вырождены."""
    if len(src) != 4 or len(dst) != 4:
        return None
    a: list[list[float]] = []
    b: list[float] = []
    for (x, y), (u, v) in zip(src, dst):
        a.append([x, y, 1.0, 0.0, 0.0, 0.0, -x * u, -y * u])
        b.append(u)
        a.append([0.0, 0.0, 0.0, x, y, 1.0, -x * v, -y * v])
        b.append(v)
    sol = _solve8(a, b)
    if sol is None:
        return None
    h = sol + [1.0]
    return [h[0:3], h[3:6], h[6:9]]


def apply_h(h: Sequence[Sequence[float]], x: float, y: float) -> Point:
    """Применить гомографию к точке."""
    w = h[2][0] * x + h[2][1] * y + h[2][2]
    if abs(w) < 1e-12:
        w = 1e-12 if w >= 0 else -1e-12
    return ((h[0][0] * x + h[0][1] * y + h[0][2]) / w,
            (h[1][0] * x + h[1][1] * y + h[1][2]) / w)


def plate_to_frame(corners: Sequence[Point], plate_w: float, plate_h: float,
                   x: float, y: float) -> Point:
    """Точка плиты (мм) → доля кадра по калибровке.

    К corner-точкам на кадре приписаны углы плиты: передний ряд — нижняя
    кромка координат плиты (y = plate_h у «переднего-левого»?? нет:
    слайсер Bambu считает y от задней кромки на юг). Принято: передний-левый
    угол стола = (0, plate_h), передний-правый = (plate_w, plate_h),
    задний-правый = (plate_w, 0), задний-левый = (0, 0).
    """
    src = [(0.0, plate_h), (plate_w, plate_h), (plate_w, 0.0), (0.0, 0.0)]
    h = homography(src, [(float(cx), float(cy)) for cx, cy in corners])
    if h is None:
        return (0.0, 0.0)
    return apply_h(h, x, y)


def project_objects(corners: Sequence[Point], plate_w: float, plate_h: float,
                    objects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Объекты карты плиты → четырёхугольники в долях кадра 0..1.

    Возвращает [{id, name, pts: [[x,y]×4]}]; объекты вне кадра не
    отбрасываются — панель сама решает, что рисовать.
    """
    out: list[dict[str, Any]] = []
    for obj in objects or []:
        x0 = float(obj.get("x") or 0.0)
        y0 = float(obj.get("y") or 0.0)
        x1 = x0 + float(obj.get("w") or 0.0)
        y1 = y0 + float(obj.get("h") or 0.0)
        pts = [plate_to_frame(corners, plate_w, plate_h, px, py)
               for px, py in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
        out.append({
            "id": obj.get("id"),
            "name": obj.get("name") or f"Объект {obj.get('id')}",
            "pts": [[round(px, 4), round(py, 4)] for px, py in pts],
        })
    return out


def corners_valid(corners: Sequence[Sequence[float]]) -> bool:
    """Калибровка похожа на четыре угла стола: в диапазоне 0..1 и не вырождена."""
    if not corners or len(corners) != 4:
        return False
    for cx, cy in corners:
        try:
            fx, fy = float(cx), float(cy)
        except (TypeError, ValueError):
            return False
        if not (-0.2 <= fx <= 1.2 and -0.2 <= fy <= 1.2):
            return False
    # площадь четырёхугольника не должна схлопываться
    (x0, y0), (x1, y1), (x2, y2), (x3, y3) = [tuple(map(float, p)) for p in corners]
    area = abs((x0 * y1 - x1 * y0) + (x1 * y2 - x2 * y1)
               + (x2 * y3 - x3 * y2) + (x3 * y0 - x0 * y3)) / 2.0
    return area > 0.01

# ------------------------------------------------------------------ хранение
def calibration_key(printer_id: str) -> str:
    """Ключ настройки с калибровкой стола принтера."""
    return "cam_cal_" + str(printer_id or "").strip()


def load_calibration(db: Any, printer_id: str) -> list:
    """Четыре угла стола из настроек; [] если калибровки нет или она битая."""
    try:
        raw = db.setting(calibration_key(printer_id), None)
    except Exception:
        return []
    if isinstance(raw, str):                      # setting() обычно уже разобрал JSON
        import json as _json
        try:
            raw = _json.loads(raw)
        except (ValueError, TypeError):
            return []
    if isinstance(raw, dict):
        corners = raw.get("corners") or []
        return corners if isinstance(corners, list) else []
    return []


def save_calibration(db: Any, printer_id: str, corners: Sequence[Sequence[float]]) -> None:
    from .config import now_iso

    db.upsert("settings",
              {"key": calibration_key(printer_id),
               "value": json.dumps({"corners": [list(map(float, c)) for c in corners],
                                    "at": now_iso()}, ensure_ascii=False)},
              key="key")


def reset_calibration(db: Any, printer_id: str) -> None:
    db.execute("DELETE FROM settings WHERE key=?", (calibration_key(printer_id),))


def running_job(db: Any, printer_id: str) -> dict[str, Any] | None:
    """Активное задание принтера (последнее запущенное), если оно есть."""
    rows = db.query(
        "SELECT * FROM print_jobs WHERE printer_id=? AND state='running'"
        " ORDER BY started_at DESC LIMIT 1", (printer_id,)) or []
    return rows[0] if rows else None
