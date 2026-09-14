"""PrintFlow Slicer Stage 1: собственный движок нарезки STL → G-code для P1S.

Это не обёртка над чужим CLI, а настоящая нарезка: модель режется на слои,
контуры сечения смещаются в периметры, области «крыши» и «дна» определяются
сравнением с соседними слоями, остальное заполняется разреженным узором,
и всё это превращается в G-code с честной оценкой веса и времени.

Честные границы Stage 1 (их нельзя обойти настройкой):

* на входе только STL — бинарный и текстовый; 3MF, OBJ, STEP не поддерживаются;
* один экструдер и один материал: AMS-слот уходит в отчёт и очередь, а не в
  G-code, потому что смену филамента Stage 1 не делает;
* нет переменной высоты слоя, глажки, тонких стен, деревьев-поддержек,
  спирального режима и дуг; поддержки — «где под областью пустота», без
  интерфейсного слоя и без отделяемого зазора;
* геометрия считается по полигонам сечения: замкнутая сетка с корректными
  нормалями даёт предсказуемый результат, рваная сетка — предупреждение,
  а не тихую порчу детали.

Модуль ничего не отправляет на принтер и не ставит файл в очередь: он только
пишет отдельный G-code и отчёт. Гейты и подтверждение оператора — в
`slicer_run.py`, маршруты — в `routes_slicer.py`.
"""
from __future__ import annotations

import math
import re
import struct
from array import array
from dataclasses import dataclass, field
from pathlib import Path

from .slicer import SlicerError

BEGIN = "; PRINTFLOW SLICER BEGIN"
END = "; PRINTFLOW SLICER END"
ENGINE_ID = "printflow"
ENGINE_VERSION = 1

# Пределы Stage 1: движок честно отказывается, а не берётся за модель,
# которую не осилит за разумное время на обычном компьютере цеха.
MAX_MODEL_MB = 120.0
MAX_TRIANGLES = 1_500_000
MAX_BUCKET_ENTRIES = 60_000_000
MANIFOLD_CHECK_LIMIT = 300_000

_EPS = 1e-9


@dataclass(frozen=True)
class SliceSettings:
    """Все параметры одной нарезки. Значения уже проверены профилем."""

    layer_height: float = 0.2
    first_layer_height: float = 0.2
    walls: int = 3
    infill_percent: float = 15.0
    infill_pattern: str = "grid"        # lines | grid | triangles | concentric
    supports: bool = False
    support_spacing_mm: float = 2.0
    brim: bool = False
    brim_width_mm: float = 5.0
    nozzle_mm: float = 0.4
    filament_mm: float = 1.75
    nozzle_temp: int = 220
    bed_temp: int = 60
    material: str = "PLA"
    density_g_cm3: float = 1.24
    speed_mm_s: float = 100.0
    travel_speed_mm_s: float = 200.0
    extrusion_width_mm: float = 0.0     # 0 = авто: nozzle × 1.125
    top_solid_layers: int = 4
    bottom_solid_layers: int = 4
    seam: str = "nearest"               # nearest | aligned
    retract_mm: float = 0.8
    retract_speed_mm_s: float = 35.0
    retract_min_travel_mm: float = 1.5
    zhop_mm: float = 0.0
    fan_percent: int = 100
    ams_slot: str = ""
    center_model: bool = True
    flow: float = 1.0
    accel_mm_s2: float = 5000.0

    @property
    def ext_width(self) -> float:
        """Ширина экструзии: автоматически — 1,125 диаметра сопла."""
        if self.extrusion_width_mm and self.extrusion_width_mm > 0:
            return float(self.extrusion_width_mm)
        return round(self.nozzle_mm * 1.125, 4)


@dataclass
class Mesh:
    """Треугольники плоско: по 9 чисел на треугольник (x0,y0,z0, x1,y1,z1, …)."""

    tris: list = field(default_factory=list)
    name: str = ""

    @property
    def count(self) -> int:
        return len(self.tris)


@dataclass
class Poly:
    """Одна непрерывная линия extrusion: периметр, крыша, заполнение, поддержка."""

    kind: str                 # perimeter | skin | infill | support | brim
    layer: int
    z: float
    pts: list
    closed: bool = False
    width: float = 0.45
    height: float = 0.2
    flow: float = 1.0

    @property
    def speed_kind(self) -> str:
        return self.kind


# ============================================================ чтение модели


def _is_binary_stl(data: bytes) -> bool:
    if len(data) < 84:
        return False
    count = struct.unpack_from("<I", data, 80)[0]
    if count <= 0 or count > MAX_TRIANGLES * 2:
        return False
    need = 84 + count * 50
    return len(data) >= need and (len(data) - need) < 4096


def _parse_binary_stl(data: bytes) -> list:
    count = struct.unpack_from("<I", data, 80)[0]
    # Важно: в бинарном STL каждый треугольник занимает 50 байт — 12 чисел
    # по 4 байта и двухбайтовый атрибут. Читать буфер как сплошной массив
    # float нельзя: атрибут сдвигает всё и сетка разваливается.
    buf = data[84:84 + count * 50]
    tris: list = []
    append = tris.append
    for row in struct.iter_unpack("<12fH", buf):
        x0, y0, z0 = row[3], row[4], row[5]
        x1, y1, z1 = row[6], row[7], row[8]
        x2, y2, z2 = row[9], row[10], row[11]
        # Нормаль считаем сами: в STL она часто врёт, а вырожденные грани
        # нужны выбрасывать до нарезки, иначе контуры получатся рваными.
        ux, uy, uz = x1 - x0, y1 - y0, z1 - z0
        vx, vy, vz = x2 - x0, y2 - y0, z2 - z0
        nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
        if nx * nx + ny * ny + nz * nz < 1e-12:
            continue
        append((x0, y0, z0, x1, y1, z1, x2, y2, z2))
    return tris


_VERTEX_RE = re.compile(
    r"vertex\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)")


def _parse_ascii_stl(data: bytes) -> list:
    text = data.decode("utf-8", errors="replace")
    nums = _VERTEX_RE.findall(text)
    tris: list = []
    for i in range(0, len(nums) - 2, 3):
        try:
            a = (float(nums[i][0]), float(nums[i][1]), float(nums[i][2]))
            b = (float(nums[i + 1][0]), float(nums[i + 1][1]), float(nums[i + 1][2]))
            c = (float(nums[i + 2][0]), float(nums[i + 2][1]), float(nums[i + 2][2]))
        except ValueError:
            continue
        ux, uy, uz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
        vx, vy, vz = c[0] - a[0], c[1] - a[1], c[2] - a[2]
        nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
        if nx * nx + ny * ny + nz * nz < 1e-12:
            continue
        tris.append((a[0], a[1], a[2], b[0], b[1], b[2], c[0], c[1], c[2]))
    return tris


def load_mesh(path: str | Path, *, max_mb: float = MAX_MODEL_MB,
              max_triangles: int = MAX_TRIANGLES) -> Mesh:
    """Прочитать STL. Всё, что не STL, отклоняется честно."""
    src = Path(str(path)).expanduser()
    if not src.is_file():
        raise SlicerError(f"Файл не найден: {src.name}")
    suffix = src.suffix.lower()
    if suffix != ".stl":
        raise SlicerError(
            f"Stage 1 нарезает только STL, а получен {suffix or 'файл без расширения'}. "
            "Экспортируйте модель как STL в CAD или в OrcaSlicer.")
    size_mb = src.stat().st_size / (1024 * 1024)
    if size_mb > max_mb:
        raise SlicerError(
            f"Модель {size_mb:.0f} МБ больше предела Stage 1 ({max_mb:.0f} МБ)")
    data = src.read_bytes()
    if _is_binary_stl(data):
        tris = _parse_binary_stl(data)
    elif data[:80].lstrip().lower().startswith(b"solid"):
        tris = _parse_ascii_stl(data)
    else:
        raise SlicerError("STL не читается: файл не бинарный и не текстовый")
    if not tris:
        raise SlicerError("В STL нет ни одного невырожденного треугольника")
    if len(tris) > max_triangles:
        raise SlicerError(
            f"В модели {len(tris)} треугольников — больше предела Stage 1 "
            f"({max_triangles}). Упростите сетку.")
    return Mesh(tris=tris, name=src.name)


def mesh_bbox(mesh: Mesh) -> dict:
    xs: list = []
    ys: list = []
    zs: list = []
    for tri in mesh.tris:
        xs.append(tri[0]); xs.append(tri[3]); xs.append(tri[6])
        ys.append(tri[1]); ys.append(tri[4]); ys.append(tri[7])
        zs.append(tri[2]); zs.append(tri[5]); zs.append(tri[8])
    if not xs:
        return {"x": 0.0, "y": 0.0, "z": 0.0, "min": (0.0, 0.0, 0.0), "max": (0.0, 0.0, 0.0)}
    lo = (min(xs), min(ys), min(zs))
    hi = (max(xs), max(ys), max(zs))
    return {
        "x": hi[0] - lo[0], "y": hi[1] - lo[1], "z": hi[2] - lo[2],
        "min": lo, "max": hi,
    }


def mesh_manifold(mesh: Mesh) -> bool | None:
    """Замкнута ли сетка: у замкнутой每个 ребро ровно у двух треугольников.

    `None` — проверка пропущена (слишком большая модель), а не «всё хорошо».
    """
    if mesh.count > MANIFOLD_CHECK_LIMIT:
        return None
    edges: dict = {}
    for tri in mesh.tris:
        v0 = (tri[0], tri[1], tri[2])
        v1 = (tri[3], tri[4], tri[5])
        v2 = (tri[6], tri[7], tri[8])
        for a, b in ((v0, v1), (v1, v2), (v2, v0)):
            key = (a, b) if a <= b else (b, a)
            edges[key] = edges.get(key, 0) + 1
    return all(count == 2 for count in edges.values())


def mesh_stats(mesh: Mesh) -> dict:
    return {
        "name": mesh.name,
        "triangles": mesh.count,
        "bbox": mesh_bbox(mesh),
        "closed": mesh_manifold(mesh),
    }


def place_mesh(mesh: Mesh, profile, center: bool = True) -> Mesh:
    """Посадить модель на стол и, если просят, отцентрировать по плите."""
    box = mesh_bbox(mesh)
    dx = -box["min"][0]
    dy = -box["min"][1]
    dz = -box["min"][2]
    if center:
        bed_x = float(getattr(profile, "bed_width_mm", 256.0))
        bed_y = float(getattr(profile, "bed_height_mm", 256.0))
        dx += (bed_x - box["x"]) / 2.0
        dy += (bed_y - box["y"]) / 2.0
    tris = []
    for tri in mesh.tris:
        tris.append((
            tri[0] + dx, tri[1] + dy, tri[2] + dz,
            tri[3] + dx, tri[4] + dy, tri[5] + dz,
            tri[6] + dx, tri[7] + dy, tri[8] + dz,
        ))
    return Mesh(tris=tris, name=mesh.name)


# ============================================================ геометрия 2D


def _signed_area(pts: list) -> float:
    total = 0.0
    n = len(pts)
    for i in range(n):
        x0, y0 = pts[i]
        x1, y1 = pts[(i + 1) % n]
        total += x0 * y1 - x1 * y0
    return total / 2.0


def _point_in_poly(point, poly: list) -> bool:
    x, y = point
    inside = False
    n = len(poly)
    for i in range(n):
        x0, y0 = poly[i]
        x1, y1 = poly[(i + 1) % n]
        if (y0 > y) != (y1 > y):
            t = (y - y0) / (y1 - y0) if (y1 - y0) else 0.0
            if x < x0 + t * (x1 - x0):
                inside = not inside
    return inside


def _interior_point(pts: list):
    """Точка внутри контура: середина первого ребра, сдвинутая к центру."""
    cx = sum(p[0] for p in pts) / len(pts)
    cy = sum(p[1] for p in pts) / len(pts)
    x0, y0 = pts[0]
    x1, y1 = pts[1]
    mx, my = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    dx, dy = cx - mx, cy - my
    length = math.hypot(dx, dy)
    if length > 1e-9:
        mx += dx / length * 1e-3
        my += dy / length * 1e-3
    return (mx, my)


def offset_contour(pts: list, delta: float, is_hole: bool,
                   miter_limit: float = 4.0) -> list:
    """Сместить контур в материал на `delta` (для дыр — расширить наружу).

    Отрицательный `delta` — смещение из материала (так делается brim).
    Контур нормализуется против часовой стрелки, поэтому знак площади
    однозначно говорит, выродился контур или нет.
    """
    if len(pts) < 3 or abs(delta) < 1e-9:
        return list(pts) if len(pts) >= 3 else []
    area = _signed_area(pts)
    if abs(area) < 1e-9:
        return []
    work = list(pts) if area > 0 else list(reversed(pts))
    n = len(work)
    d = -delta if is_hole else delta

    dirs: list = []
    norms: list = []
    for i in range(n):
        x0, y0 = work[i]
        x1, y1 = work[(i + 1) % n]
        dx, dy = x1 - x0, y1 - y0
        length = math.hypot(dx, dy)
        if length < 1e-12:
            dirs.append(None)
            norms.append(None)
            continue
        ux, uy = dx / length, dy / length
        dirs.append((ux, uy))
        norms.append((-uy, ux))          # левая нормаль: внутрь для обхода CCW

    out: list = []
    limit = abs(d) * miter_limit + 1e-9
    for i in range(n):
        prev = (i - 1) % n
        nrm_prev = norms[prev]
        nrm_cur = norms[i]
        if nrm_prev is None or nrm_cur is None:
            continue
        vx, vy = work[i]
        px, py = work[prev][0] + nrm_prev[0] * d, work[prev][1] + nrm_prev[1] * d
        qx, qy = vx + nrm_cur[0] * d, vy + nrm_cur[1] * d
        ux, uy = dirs[prev]
        wx, wy = dirs[i]
        denom = ux * wy - uy * wx
        if abs(denom) < 1e-12:
            out.append((vx + nrm_prev[0] * d, vy + nrm_prev[1] * d))
            continue
        t = ((qx - px) * wy - (qy - py) * wx) / denom
        x = px + ux * t
        y = py + uy * t
        if math.hypot(x - vx, y - vy) > limit:
            # Острый угол: митр уехал слишком далеко — срезаем двумя точками.
            out.append((vx + nrm_prev[0] * d, vy + nrm_prev[1] * d))
            out.append((vx + nrm_cur[0] * d, vy + nrm_cur[1] * d))
        else:
            out.append((x, y))

    cleaned: list = []
    for point in out:
        if cleaned and abs(point[0] - cleaned[-1][0]) < 1e-9 \
                and abs(point[1] - cleaned[-1][1]) < 1e-9:
            continue
        cleaned.append(point)
    if len(cleaned) > 2 and abs(cleaned[0][0] - cleaned[-1][0]) < 1e-9 \
            and abs(cleaned[0][1] - cleaned[-1][1]) < 1e-9:
        cleaned.pop()
    if len(cleaned) < 3:
        return []
    if _signed_area(cleaned) <= 0:
        return []
    return cleaned


def classify_contours(contours: list) -> list:
    """Разложить контуры на внешние и дыры: ориентируясь на вложенность.

    Ориентацию из сетки не используем: у экспортёров она расходится. Глубина
    вложенности считается по чётности — внешний контур, дыра, остров внутри
    дыры и так далее.
    """
    items = []
    for pts in contours:
        if len(pts) < 3:
            continue
        area = _signed_area(pts)
        if abs(area) < 1e-7:
            continue
        items.append((list(pts), _interior_point(pts)))
    result: list = []
    for i, (pts, probe) in enumerate(items):
        depth = 0
        for j, (other, _) in enumerate(items):
            if i == j:
                continue
            if _point_in_poly(probe, other):
                depth += 1
        is_hole = (depth % 2) == 1
        area = _signed_area(pts)
        want_positive = not is_hole
        if (area > 0) != want_positive:
            pts = list(reversed(pts))
        result.append((pts, is_hole))
    return result


class Spanner:
    """Интервалы сечения полигонов горизонтальной линией (чёт-нечет).

    Один объект на слой: запросы идут по возрастанию y, активные рёбра
    переиспользуются. Ориентация контуров не важна — важно только чётное
    или нечётное число пересечений, поэтому дыры работают сами.
    """

    def __init__(self, contours: list):
        edges: list = []
        for pts in contours:
            n = len(pts)
            if n < 3:
                continue
            for i in range(n):
                x0, y0 = pts[i]
                x1, y1 = pts[(i + 1) % n]
                if y0 == y1:
                    continue
                if y0 < y1:
                    edges.append((y0, y1, x0, x1))
                else:
                    edges.append((y1, y0, x1, x0))
        edges.sort(key=lambda edge: edge[0])
        self._edges = edges
        self._index = 0
        self._active: list = []
        self._last_y = None

    @property
    def empty(self) -> bool:
        return not self._edges

    def spans(self, y: float) -> list:
        """Список [x0, x1] внутри области на этой линии."""
        if self._last_y is None or y < self._last_y - 1e-12:
            self._index = 0
            self._active = []
        self._last_y = y
        edges = self._edges
        while self._index < len(edges) and edges[self._index][0] <= y:
            edge = edges[self._index]
            if edge[1] > y:
                self._active.append(edge)
            self._index += 1
        if self._active:
            self._active = [edge for edge in self._active if edge[1] > y]
        if not self._active:
            return []
        crossings = []
        for y0, y1, xa, xb in self._active:
            crossings.append(xa + (xb - xa) * (y - y0) / (y1 - y0))
        crossings.sort()
        out: list = []
        for k in range(0, len(crossings) - 1, 2):
            if crossings[k + 1] - crossings[k] > 1e-6:
                out.append([crossings[k], crossings[k + 1]])
        return out


def _interval_subtract(a: list, b: list) -> list:
    """Из интервалов `a` вычесть интервалы `b`."""
    if not b:
        return [list(item) for item in a]
    out: list = []
    for x0, x1 in a:
        cur = x0
        for bx0, bx1 in b:
            if bx1 <= cur:
                continue
            if bx0 >= x1:
                break
            if bx0 > cur:
                out.append([cur, min(bx0, x1)])
            cur = max(cur, bx1)
            if cur >= x1:
                break
        if cur < x1:
            out.append([cur, x1])
    return [item for item in out if item[1] - item[0] > 1e-6]


def _interval_intersect(a: list, b: list) -> list:
    if not a or not b:
        return []
    out: list = []
    i = j = 0
    while i < len(a) and j < len(b):
        lo = max(a[i][0], b[j][0])
        hi = min(a[i][1], b[j][1])
        if hi - lo > 1e-6:
            out.append([lo, hi])
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return out


def _rotate(points: list, angle_deg: float) -> list:
    if not angle_deg:
        return list(points)
    a = math.radians(angle_deg)
    ca, sa = math.cos(a), math.sin(a)
    out = []
    for x, y in points:
        out.append((x * ca + y * sa, -x * sa + y * ca))
    return out


def _rotate_contours(contours: list, angle_deg: float) -> list:
    if not angle_deg:
        return [list(pts) for pts in contours]
    return [_rotate(pts, angle_deg) for pts in contours]


# ============================================================ нарезка на слои


@dataclass
class Layer:
    index: int
    z_print: float     # высота сопла: верх слоя
    z_plane: float     # плоскость сечения: середина слоя
    height: float      # толщина слоя
    contours: list     # [(points, is_hole)]


def _plane_heights(z_max: float, first_height: float, layer_height: float) -> list:
    """Высоты слоёв: (верх слоя, плоскость сечения, толщина)."""
    rows: list = []
    index = 0
    while True:
        top = first_height if index == 0 else first_height + index * layer_height
        bottom = 0.0 if index == 0 else first_height + (index - 1) * layer_height
        rows.append((top, (bottom + top) / 2.0, top - bottom))
        if top >= z_max - 1e-9:
            break
        index += 1
        if index > 50000:
            break
    return rows


def slice_mesh(mesh: Mesh, layer_height: float, first_layer_height: float) -> tuple:
    """Разрезать сетку на слои и сшить сечения в замкнутые контуры.

    Возвращает `(layers, open_loops)`: слой и число контуров, которые не
    сомкнулись (ненулевое значение — признак рваной сетки).
    """
    tris = mesh.tris
    if not tris:
        return []
    z_max = max(max(tri[2], tri[5], tri[8]) for tri in tris)
    z_min = min(min(tri[2], tri[5], tri[8]) for tri in tris)
    if z_max - z_min < 1e-6:
        raise SlicerError("Модель без объёма по высоте: нарезать нечего")
    rows = _plane_heights(z_max, first_layer_height, layer_height)
    count = len(rows)

    # Раскладка треугольников по слоям: иначе на каждой высоте пришлось бы
    # перебирать всю сетку, и большая модель не нарезалась бы вообще.
    buckets: list = [[] for _ in range(count)]
    entries = 0
    for index, tri in enumerate(tris):
        tz0, tz1, tz2 = tri[2], tri[5], tri[8]
        lo = min(tz0, tz1, tz2)
        hi = max(tz0, tz1, tz2)
        if hi - lo < 1e-12:
            continue
        if lo < rows[0][1] < hi:
            buckets[0].append(index)
            entries += 1
        first = max(1, int(math.ceil((lo - first_layer_height) / layer_height + 0.5)))
        last = min(count - 1, int(math.floor((hi - first_layer_height) / layer_height + 0.5)))
        for i in range(first, last + 1):
            plane = rows[i][1]
            if lo < plane < hi:
                buckets[i].append(index)
                entries += 1
        if entries > MAX_BUCKET_ENTRIES:
            raise SlicerError(
                "Модель слишком сложная для Stage 1: слишком много треугольников "
                "пересекают слои. Упростите сетку или уменьшите высоту модели.")
    if entries == 0:
        raise SlicerError("Сечение модели пустое: проверьте файл STL")

    layers: list = []
    open_loops = 0
    for i in range(count):
        top, plane, height = rows[i]
        segments: list = []
        for tri_index in buckets[i]:
            tri = tris[tri_index]
            point = _triangle_cross(tri, plane)
            if point is not None:
                segments.append(point)
        contours: list = []
        if segments:
            loops = _chain_loops(segments)
            raw = []
            for pts, closed in loops:
                if not closed:
                    open_loops += 1
                dedup = []
                for point in pts:
                    if dedup and abs(point[0] - dedup[-1][0]) < 1e-9 \
                            and abs(point[1] - dedup[-1][1]) < 1e-9:
                        continue
                    dedup.append(point)
                if len(dedup) >= 3:
                    raw.append(_simplify(dedup))
            contours = classify_contours(raw)
        layers.append(Layer(index=i, z_print=top, z_plane=plane,
                            height=height, contours=contours))
    # Разомкнутые контуры — признак рваной сетки. Деталь всё равно
    # получится, но молчать об этом нельзя.
    return layers, open_loops


def _triangle_cross(tri, z: float):
    """Отрезок пересечения треугольника с плоскостью z."""
    points: list = []
    verts = ((tri[0], tri[1], tri[2]), (tri[3], tri[4], tri[5]), (tri[6], tri[7], tri[8]))
    for i in range(3):
        ax, ay, az = verts[i]
        bx, by, bz = verts[(i + 1) % 3]
        if az == bz:
            continue
        if (az <= z < bz) or (bz <= z < az):
            t = (z - az) / (bz - az)
            points.append((ax + (bx - ax) * t, ay + (by - ay) * t))
    if len(points) < 2:
        return None
    a = points[0]
    for candidate in points[1:]:
        if abs(candidate[0] - a[0]) > 1e-7 or abs(candidate[1] - a[1]) > 1e-7:
            return (a, candidate)
    return None


def _simplify(pts: list, tolerance: float = 0.005) -> list:
    """Убрать точки, лежащие на прямой: форма не меняется, G-code короче.

    Точка выбрасывается, только если её отклонение от отрезка соседей меньше
    допуска, — поэтому скругления и фаски сохраняются.
    """
    if len(pts) < 4:
        return list(pts)
    out: list = [pts[0]]
    for i in range(1, len(pts) - 1):
        ax, ay = out[-1]
        bx, by = pts[i]
        cx, cy = pts[i + 1]
        ux, uy = cx - ax, cy - ay
        length = math.hypot(ux, uy)
        if length < 1e-9:
            continue
        distance = abs(ux * (by - ay) - uy * (bx - ax)) / length
        if distance > tolerance:
            out.append((bx, by))
    out.append(pts[-1])
    if len(out) >= 3 and abs(out[0][0] - out[-1][0]) < 1e-9 \
            and abs(out[0][1] - out[-1][1]) < 1e-9:
        out.pop()
    return out if len(out) >= 3 else list(pts)


def _chain_loops(segments: list, eps: float = 1e-4) -> list:
    """Сшить отрезки сечения в замкнутые контуры."""
    def key(point):
        return (int(round(point[0] / eps)), int(round(point[1] / eps)))

    grid: dict = {}
    for index, (a, b) in enumerate(segments):
        grid.setdefault(key(a), []).append(index)
        grid.setdefault(key(b), []).append(index)
    used = [False] * len(segments)
    loops: list = []
    for start in range(len(segments)):
        if used[start]:
            continue
        used[start] = True
        a, b = segments[start]
        pts = [a, b]
        cur = b
        closed = False
        while len(pts) < 200000:
            nxt = None
            for candidate in grid.get(key(cur), ()):
                if not used[candidate]:
                    nxt = candidate
                    break
            if nxt is None:
                break
            used[nxt] = True
            sa, sb = segments[nxt]
            other = sb if key(sa) == key(cur) else sa
            if key(other) == key(pts[0]):
                closed = True
                break
            pts.append(other)
            cur = other
        if len(pts) >= 3:
            loops.append((pts, closed))
    return loops


# ============================================================ построение путей


def _fill_angle(pattern: str, index: int) -> float:
    if pattern == "lines":
        return 0.0
    if pattern == "triangles":
        return (0.0, 60.0, 120.0)[index % 3]
    if pattern == "concentric":
        return 0.0
    return 0.0 if index % 2 == 0 else 90.0      # grid


def _zigzag(scanlines: list, tolerance: float) -> list:
    """Связать интервалы соседних линий в непрерывный зигзаг.

    Соединение соседних линий — экструдируемая диагональ, поэтому шов
    получается одним проходом, а не серией коротких переездов.
    """
    polylines: list = []
    active: list = []
    for row, (y, intervals) in enumerate(scanlines):
        if not intervals:
            continue
        reverse = (row % 2 == 1)
        ordered = sorted(intervals)
        if reverse:
            ordered = list(reversed(ordered))
        matched: list = []
        for x0, x1 in ordered:
            chain = None
            if len(active) <= 64:
                for item in active:
                    if x0 - tolerance <= item["x"] <= x1 + tolerance:
                        chain = item
                        break
            if chain is None:
                chain = {"pts": [], "x": None}
                polylines.append(chain)
            else:
                active.remove(chain)
            a, b = (x1, x0) if reverse else (x0, x1)
            chain["pts"].extend([(a, y), (b, y)])
            chain["x"] = b
            matched.append(chain)
        active = matched
    return [item["pts"] for item in polylines if len(item["pts"]) >= 2]


def _contour_bounds(contours: list):
    if not contours:
        return None
    xs: list = []
    ys: list = []
    for pts in contours:
        for x, y in pts:
            xs.append(x)
            ys.append(y)
    return (min(xs), min(ys), max(xs), max(ys))


def build_paths(layers: list, settings: SliceSettings, profile) -> tuple:
    """Превратить слои в список линий extrusion."""
    ext_w = settings.ext_width
    walls = max(1, int(settings.walls))
    fill_inset = max(0.0, (walls - 0.5) * ext_w)
    top_n = max(0, int(settings.top_solid_layers))
    bot_n = max(0, int(settings.bottom_solid_layers))
    total = len(layers)
    skin_step = max(0.15, ext_w * 0.95)
    density = max(0.0, min(100.0, float(settings.infill_percent)))
    notes: list = []
    unsupported: list = []

    cache: dict = {}

    def region_spanner(index: int, angle: float) -> Spanner:
        key = ("region", index, angle)
        found = cache.get(key)
        if found is None:
            found = Spanner(_rotate_contours(
                [pts for pts, _hole in layers[index].contours], -angle))
            cache[key] = found
        return found

    def fill_contours(index: int, angle: float) -> list:
        key = ("fill", index, angle)
        found = cache.get(key)
        if found is None:
            raw = []
            for pts, is_hole in layers[index].contours:
                if fill_inset > 0:
                    moved = offset_contour(pts, fill_inset, is_hole)
                    if len(moved) >= 3:
                        raw.append(moved)
                else:
                    raw.append(list(pts))
            found = (raw, Spanner(_rotate_contours(raw, -angle)))
            cache[key] = found
        return found

    polys: list = []
    for index, layer in enumerate(layers):
        if not layer.contours:
            continue
        z = layer.z_print
        height = layer.height
        first = (index == 0)
        flow = 1.05 if first else 1.0

        # --- brim: петли вокруг первого слоя, из материала наружу
        if first and settings.brim and settings.brim_width_mm > 0:
            loops = max(1, int(round(float(settings.brim_width_mm) / ext_w)))
            for step in range(loops):
                delta = -(0.5 + step) * ext_w
                for pts, is_hole in layer.contours:
                    ring = offset_contour(pts, delta, is_hole)
                    if len(ring) >= 3:
                        polys.append(Poly("brim", index, z, ring, closed=True,
                                          width=ext_w, height=height, flow=flow))

        # --- поддержки: там, где под областью предыдущего слоя пустота
        if settings.supports and index > 0:
            below = region_spanner(index - 1, 0.0)
            here = region_spanner(index, 0.0)
            if not here.empty:
                bounds = _contour_bounds([pts for pts, _h in layer.contours])
                step = max(0.5, float(settings.support_spacing_mm))
                lines: list = []
                y = bounds[1] + step * 0.5
                while y < bounds[3]:
                    gap = _interval_subtract(here.spans(y), below.spans(y))
                    gap = [iv for iv in gap if iv[1] - iv[0] >= ext_w * 0.8]
                    if gap:
                        lines.append((y, gap))
                    y += step
                for pts in _zigzag(lines, step):
                    polys.append(Poly("support", index, z, pts, closed=False,
                                      width=ext_w, height=height, flow=flow))
                if lines:
                    notes.append(f"поддержки: слой {index + 1}")

        # --- периметры: изнутри наружу, чтобы внешняя стенка легла последней
        for wall in range(walls - 1, -1, -1):
            delta = (0.5 + wall) * ext_w
            for pts, is_hole in layer.contours:
                ring = offset_contour(pts, delta, is_hole)
                if len(ring) >= 3:
                    polys.append(Poly("perimeter", index, z, ring, closed=True,
                                      width=ext_w, height=height, flow=flow))

        raw_fill, fill_spanner = fill_contours(index, _fill_angle(
            settings.infill_pattern, index))
        if not raw_fill or fill_spanner.empty:
            continue
        angle = _fill_angle(settings.infill_pattern, index)

        if settings.infill_pattern == "concentric":
            # Концентрика: вложенные кольца до вырождения контура.
            for pts, is_hole in layers[index].contours:
                step = 0
                while step < 400:
                    delta = fill_inset + step * ext_w
                    ring = offset_contour(pts, delta, is_hole)
                    if len(ring) < 3:
                        break
                    polys.append(Poly("infill", index, z, ring, closed=True,
                                      width=ext_w, height=height, flow=flow))
                    step += 1
            continue

        bounds = _contour_bounds(raw_fill)
        if bounds is None:
            continue
        y0, y1 = bounds[1], bounds[3]
        infill_step = ext_w
        if density > 0.5:
            infill_step = min(50.0, max(ext_w * 0.5, ext_w / (density / 100.0)))
        skin_ys: set = set()
        value = y0 + skin_step * 0.5
        while value < y1:
            skin_ys.add(round(value, 4))
            value += skin_step
        infill_ys: set = set()
        if density > 0.5:
            value = y0 + infill_step * 0.5
            while value < y1:
                infill_ys.add(round(value, 4))
                value += infill_step
        ordered_ys = sorted(skin_ys | infill_ys)

        above = [region_spanner(i, angle) for i in range(index + 1, min(total, index + 1 + top_n))]
        below_layers = [region_spanner(i, angle) for i in range(max(0, index - bot_n), index)]

        skin_lines: list = []
        sparse_lines: list = []
        for y in ordered_ys:
            fill = fill_spanner.spans(y)
            if not fill:
                continue
            top_skin = fill
            if above:
                cover = None
                for spanner in above:
                    spans = spanner.spans(y)
                    cover = spans if cover is None else _interval_intersect(cover, spans)
                    if not cover:
                        break
                if cover:
                    top_skin = _interval_subtract(fill, cover)
            rest = _interval_subtract(fill, top_skin)
            bottom_skin = rest
            if below_layers:
                cover = None
                for spanner in below_layers:
                    spans = spanner.spans(y)
                    cover = spans if cover is None else _interval_intersect(cover, spans)
                    if not cover:
                        break
                if cover:
                    bottom_skin = _interval_subtract(rest, cover)
            sparse = _interval_subtract(rest, bottom_skin)
            key_y = round(y, 4)
            if key_y in skin_ys:
                combined = top_skin + bottom_skin
                if combined:
                    skin_lines.append((y, sorted(combined)))
            if key_y in infill_ys and sparse:
                sparse_lines.append((y, sparse))

        for pts in _zigzag(skin_lines, skin_step):
            polys.append(Poly("skin", index, z,
                              _rotate(pts, angle) if angle else pts,
                              closed=False, width=ext_w, height=height, flow=flow))
        for pts in _zigzag(sparse_lines, infill_step):
            polys.append(Poly("infill", index, z,
                              _rotate(pts, angle) if angle else pts,
                              closed=False, width=ext_w, height=height, flow=flow))

    if settings.infill_pattern == "gyroid":
        unsupported.append(
            "гироид в Stage 1 не поддерживается: нарезано сеткой (grid)")
    if settings.supports:
        unsupported.append(
            "поддержки Stage 1 — без интерфейсного слоя: снимаются хуже, "
            "чем в OrcaSlicer")
    return polys, {"notes": notes, "unsupported": unsupported}


# ============================================================ запись G-code


def _speed_for(kind: str, settings: SliceSettings, first_layer: bool) -> float:
    base = float(settings.speed_mm_s)
    if first_layer:
        base = max(12.0, base * 0.35)
    if kind == "perimeter":
        return max(8.0, base * 0.6)
    if kind in ("skin", "support"):
        return max(8.0, base * 0.8)
    return max(8.0, base)


def _move_time(distance: float, speed_mm_s: float, accel: float) -> float:
    if distance <= 0 or speed_mm_s <= 0:
        return 0.0
    if accel <= 0:
        return distance / speed_mm_s
    ramp = speed_mm_s * speed_mm_s / accel
    if distance < ramp:
        return 2.0 * math.sqrt(distance / accel)
    return distance / speed_mm_s + speed_mm_s / accel


class _Writer:
    def __init__(self, settings: SliceSettings, profile):
        self.settings = settings
        self.profile = profile
        self.lines: list = []
        self.x = 0.0
        self.y = 0.0
        self.z = 0.0
        self.e = 0.0
        self.seconds = 0.0
        self.extruded = 0.0
        self.moves = 0
        self.retracted = 0.0
        self.primed = False

    def add(self, text: str) -> None:
        self.lines.append(text)

    def travel(self, x: float, y: float, z: float | None = None,
               comment: str = "") -> None:
        settings = self.settings
        distance = math.hypot(x - self.x, y - self.y)
        if distance < 1e-6 and (z is None or abs(z - self.z) < 1e-9):
            return
        hop = 0.0
        if (self.primed and settings.retract_mm > 0
                and distance >= float(settings.retract_min_travel_mm)):
            self.add(f"G1 E-{settings.retract_mm:.5f} "
                     f"F{settings.retract_speed_mm_s * 60:.0f}")
            self.seconds += settings.retract_mm / max(1.0, settings.retract_speed_mm_s)
            self.retracted = settings.retract_mm
            hop = float(settings.zhop_mm or 0.0)
            if hop > 0:
                self.add(f"G1 Z{self.z + hop:.3f} F600")
                self.seconds += _move_time(hop, 10.0, settings.accel_mm_s2)
        if z is not None and abs(z - self.z) > 1e-9:
            self.add(f"G1 Z{z:.3f} F600")
            self.seconds += _move_time(abs(z - self.z), 10.0, settings.accel_mm_s2)
            self.z = z
        feed = max(10.0, float(settings.travel_speed_mm_s)) * 60.0
        self.add(f"G1 X{x:.3f} Y{y:.3f} F{feed:.0f}{(' ; ' + comment) if comment else ''}")
        self.seconds += _move_time(distance, max(10.0, settings.travel_speed_mm_s),
                                   settings.accel_mm_s2)
        self.x, self.y = x, y
        self.moves += 1
        if self.retracted:
            self.add(f"G1 E{self.retracted:.5f} "
                     f"F{settings.retract_speed_mm_s * 60:.0f}")
            self.seconds += self.retracted / max(1.0, settings.retract_speed_mm_s)
            self.retracted = 0.0
            if hop > 0:
                self.add(f"G1 Z{z if z is not None else self.z:.3f} F600")
                self.seconds += _move_time(hop, 10.0, settings.accel_mm_s2)


def _e_per_mm(width: float, height: float, filament_mm: float, flow: float) -> float:
    area = width * height
    fil_area = math.pi * (filament_mm / 2.0) ** 2
    if fil_area <= 0:
        return 0.0
    return area / fil_area * flow


def write_gcode(polys: list, settings: SliceSettings, profile,
                meta: dict | None = None) -> tuple:
    """Собрать G-code из линий extrusion и посчитать честную оценку."""
    meta = meta or {}
    writer = _Writer(settings, profile)
    filament_area = math.pi * (settings.filament_mm / 2.0) ** 2
    layer_index = None
    last_kind = ""
    for poly in polys:
        if not poly.pts:
            continue
        if poly.layer != layer_index:
            layer_index = poly.layer
            if layer_index == 1:
                pwm = int(round(max(0, min(100, settings.fan_percent)) * 255 / 100))
                writer.add(f"M106 S{pwm}")
            writer.add(f";LAYER:{layer_index}")
        first_layer = (poly.layer == 0)
        pts = poly.pts
        if poly.closed:
            start = 0
            if settings.seam == "nearest" and len(pts) > 2:
                best = None
                for i, point in enumerate(pts):
                    distance = math.hypot(point[0] - writer.x, point[1] - writer.y)
                    if best is None or distance < best[0]:
                        best = (distance, i)
                if best:
                    start = best[1]
            pts = pts[start:] + pts[:start]
            pts = pts + [pts[0]]
        speed = _speed_for(poly.kind, settings, first_layer)
        feed = speed * 60.0
        e_mm = _e_per_mm(poly.width, poly.height, settings.filament_mm,
                         poly.flow * float(settings.flow))
        writer.travel(pts[0][0], pts[0][1], poly.z,
                      comment=("" if poly.kind == last_kind else poly.kind))
        last_kind = poly.kind
        for point in pts[1:]:
            distance = math.hypot(point[0] - writer.x, point[1] - writer.y)
            if distance < 1e-9:
                continue
            writer.e += distance * e_mm
            writer.extruded += distance * e_mm
            writer.primed = True
            writer.add(f"G1 X{point[0]:.3f} Y{point[1]:.3f} "
                       f"E{distance * e_mm:.5f} F{feed:.0f}")
            writer.seconds += _move_time(distance, speed, settings.accel_mm_s2)
            writer.x, writer.y = point
            writer.moves += 1

    body = list(writer.lines)
    volume = writer.extruded * filament_area
    weight = volume / 1000.0 * float(settings.density_g_cm3)
    material = str(meta.get("material") or settings.material)

    header = [
        BEGIN,
        f"; engine: {ENGINE_ID}",
        f"; engine_version: {ENGINE_VERSION}",
        f"; profile: {meta.get('profile', '')}",
        f"; printer: {meta.get('printer', '')}",
        f"; model: {meta.get('model', '')}",
        f"; material: {material}",
        f"; layer_height: {settings.layer_height}",
        f"; first_layer_height: {settings.first_layer_height}",
        f"; walls: {settings.walls}",
        f"; infill_percent: {settings.infill_percent}",
        f"; infill_pattern: {settings.infill_pattern}",
        f"; supports: {int(bool(settings.supports))}",
        f"; nozzle_mm: {settings.nozzle_mm}",
        f"; extrusion_width_mm: {settings.ext_width}",
        f"; nozzle_temp: {settings.nozzle_temp}",
        f"; bed_temp: {settings.bed_temp}",
        f"; estimated_filament_mm: {writer.extruded:.1f}",
        f"; estimated_volume_cm3: {volume / 1000.0:.2f}",
        f"; estimated_weight_g: {weight:.2f}",
        f"; estimated_time_s: {int(round(writer.seconds))}",
        END,
    ]
    if settings.ams_slot:
        header.insert(-1, f"; ams_slot: {settings.ams_slot}")
    start_block = [
        f"M104 S{int(settings.nozzle_temp)}",
        f"M140 S{int(settings.bed_temp)}",
        "G90",
        "G21",
        "M83",
        "G92 E0",
        "G28",
        f"M109 S{int(settings.nozzle_temp)}",
        f"M190 S{int(settings.bed_temp)}",
        "M106 S0",
    ]
    park_x = float(getattr(profile, "park_x_mm", 10.0))
    park_y = float(getattr(profile, "park_y_mm", 246.0))
    end_block = [
        "M106 S0",
        "M104 S0",
        "M140 S0",
        f"G1 X{park_x:.1f} Y{park_y:.1f} "
        f"F{max(10.0, settings.travel_speed_mm_s) * 60:.0f}",
    ]
    text = "\n".join(header + start_block + body + end_block) + "\n"
    report = {
        "engine": ENGINE_ID,
        "engine_version": ENGINE_VERSION,
        "filament_mm": round(writer.extruded, 1),
        "volume_cm3": round(volume / 1000.0, 2),
        "weight_g": round(weight, 2),
        "seconds": int(round(writer.seconds)),
        "minutes": round(writer.seconds / 60.0, 1),
        "moves": writer.moves,
    }
    return text, report


def slice_model(source: str | Path, settings: SliceSettings, profile,
                *, max_mb: float = MAX_MODEL_MB) -> tuple:
    """Нарезать модель целиком: STL → G-code + отчёт.

    Ничего не отправляет на принтер и не трогает исходный файл.
    """
    mesh = load_mesh(source, max_mb=max_mb)
    stats = mesh_stats(mesh)
    box = stats["bbox"]
    bed_x = float(getattr(profile, "bed_width_mm", 256.0))
    bed_y = float(getattr(profile, "bed_height_mm", 256.0))
    max_z = float(getattr(profile, "max_print_height_mm", 256.0))
    if box["x"] > bed_x + 1e-6 or box["y"] > bed_y + 1e-6:
        raise SlicerError(
            f"Модель {box['x']:.1f} × {box['y']:.1f} мм не влезает в стол "
            f"{bed_x:.0f} × {bed_y:.0f} мм")
    if box["z"] > max_z + 1e-6:
        raise SlicerError(
            f"Высота модели {box['z']:.1f} мм больше предела {max_z:.0f} мм")
    placed = place_mesh(mesh, profile, center=bool(settings.center_model))
    layers, open_loops = slice_mesh(placed, settings.layer_height,
                                    settings.first_layer_height)
    if not layers:
        raise SlicerError("Сечение модели пустое: проверьте файл STL")
    polys, notes = build_paths(layers, settings, profile)
    if not polys:
        raise SlicerError("Не получено ни одной линии extrusion: модель пустая "
                          "или слишком тонкая для сопла")
    warnings: list = list(notes.get("unsupported") or [])
    if stats["closed"] is False:
        warnings.append("Сетка не замкнута: возможны пропуски и артефакты")
    if stats["closed"] is None:
        warnings.append("Замкнутость сетки не проверена: модель слишком большая")
    if open_loops:
        warnings.append(f"Контуры сечения не сомкнулись в {open_loops} местах: "
                        "сетка рваная")
    text, report = write_gcode(
        polys, settings, profile,
        meta={
            "profile": getattr(profile, "id", ""),
            "printer": getattr(profile, "printer", ""),
            "model": Path(str(source)).name,
            "material": settings.material,
        })
    report.update({
        "model": Path(str(source)).name,
        "triangles": stats["triangles"],
        "closed": stats["closed"],
        "bbox_mm": [round(box["x"], 2), round(box["y"], 2), round(box["z"], 2)],
        "layers": len(layers),
        "warnings": warnings,
        "notes": notes.get("notes") or [],
        "settings": {
            "layer_height": settings.layer_height,
            "first_layer_height": settings.first_layer_height,
            "walls": settings.walls,
            "infill_percent": settings.infill_percent,
            "infill_pattern": settings.infill_pattern,
            "supports": bool(settings.supports),
            "brim": bool(settings.brim),
            "nozzle_mm": settings.nozzle_mm,
            "extrusion_width_mm": settings.ext_width,
            "nozzle_temp": settings.nozzle_temp,
            "bed_temp": settings.bed_temp,
            "material": settings.material,
            "ams_slot": settings.ams_slot,
        },
    })
    return text, report


def slice_to_file(source: str | Path, destination: str | Path,
                  settings: SliceSettings, profile) -> dict:
    """Файловая обёртка: исходник не перезаписывается никогда."""
    src = Path(str(source)).expanduser()
    dst = Path(str(destination)).expanduser()
    if src.resolve() == dst.resolve():
        raise SlicerError("Исходная модель и результат должны быть разными файлами")
    text, report = slice_model(src, settings, profile)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(text, encoding="utf-8", newline="\n")
    report.update({"source": str(src), "output": str(dst),
                   "output_bytes": len(text.encode("utf-8"))})
    return report
