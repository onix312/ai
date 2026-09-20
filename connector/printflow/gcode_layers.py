"""Шкала слоёв (18.11): послойный индекс G-code для Hero-пульта.

Зачем
-----
Оператор смотрит на живое видео и видит «что-то печатается». Шкала слоёв
рядом отвечает на вопросы, на которые камера не отвечает: какой это слой из
скольких, что именно кладёт сопло сейчас (стенка, заполнение, поддержка,
мост), сколько пластика ушло на слой и сколько минут осталось по расчёту
слайсера. Ползунок листает любой слой файла — можно заглянуть вперёд и
понять, где будут мосты и поддержки.

Что разбираем
-------------
* ``.gcode`` рядом с заданием или ``Metadata/plate_N.gcode`` внутри 3MF —
  ровно тот файл, который ушёл на станок;
* маркеры слоёв Bambu Studio / OrcaSlicer / PrusaSlicer (``;LAYER_CHANGE``,
  ``;Z:``, ``;HEIGHT:``, ``; layer num/total_layer_count: N/T``) и Cura
  (``;LAYER:n``); без маркеров — смена Z при экструзии;
* ``;TYPE:`` → корзина (наружная стенка, внутренняя стенка, заполнение,
  сплошные слои, поддержка, юбка/башня, мосты и свесы, прочее);
* ``M73 P<процент> R<минут>`` — расчётный прогресс слайсера на начало слоя;
* дуги G2/G3 (I/J) раскладываются на хорды.

Ограничения по ресурсам: разбор идёт потоково (файл в память не грузится),
слой прореживается до :data:`LAYER_SEGMENT_CAP` отрезков (коллинеарные
точки схлопываются с допуском :data:`MERGE_TOLERANCE_MM`), координаты
хранятся в ``array('f')``. Индекс строится в фоне и кэшируется на три
последних файла: пока идёт разбор, маршрут отвечает ``building: true``.
"""
from __future__ import annotations

import array
import io
import math
import re
import threading
import time
import zipfile
from collections import OrderedDict
from pathlib import Path
from typing import Any, Iterable, Iterator

# Сколько отрезков оставляем на слой после прореживания: браузер рисует
# слой на канве за один кадр, а JSON слоя не разрастается за ~40 КБ.
LAYER_SEGMENT_CAP = 1200
# Допуск схлопывания коллинеарных точек (мм): визуально не отличимо.
MERGE_TOLERANCE_MM = 0.06
# Предохранители от «файла на гигабайт»: дальше индекс помечается truncated.
MAX_LINES = 6_000_000
MAX_LAYERS = 5000
# Сколько индексов держим в памяти (по одному на печатающий станок обычно).
CACHE_SIZE = 3

BUCKETS = ("wall_outer", "wall_inner", "infill", "solid", "support", "skirt",
           "bridge", "other")
BUCKET_LABELS = {
    "wall_outer": "Наружная стенка",
    "wall_inner": "Внутренняя стенка",
    "infill": "Заполнение",
    "solid": "Сплошные слои",
    "support": "Поддержка",
    "skirt": "Юбка / башня",
    "bridge": "Мосты и свесы",
    "other": "Прочее",
}

_PLATE_MEMBER_RE = re.compile(r"^Metadata/plate_(\d+)\.gcode$", re.I)
_LAYER_NUM_RE = re.compile(r"layer num/total_layer_count:\s*(\d+)\s*/\s*(\d+)", re.I)
_CURA_LAYER_RE = re.compile(r"^;LAYER:\s*(-?\d+)", re.I)
_M73_RE = re.compile(r"\bP(\d+(?:\.\d+)?)|\bR(\d+(?:\.\d+)?)")


def bucket_of(type_text: str) -> str:
    """Корзина по строке ``;TYPE:`` любого из трёх слайсеров."""
    t = str(type_text or "").strip().lower()
    if not t:
        return "other"
    if "overhang" in t or "bridge" in t:
        return "bridge"
    if "support" in t:
        return "support"
    if "skirt" in t or "brim" in t or "prime" in t or "wipe tower" in t:
        return "skirt"
    if "outer wall" in t or "external perimeter" in t or t == "wall-outer":
        return "wall_outer"
    if "wall" in t or "perimeter" in t:
        return "wall_inner"
    if "sparse infill" in t or "internal infill" in t or t in ("fill", "infill"):
        return "infill"
    if ("solid" in t or "top" in t or "bottom" in t or "skin" in t
            or "ironing" in t or "gap" in t):
        return "solid"
    return "other"


# --------------------------------------------------------------- геометрия
class _Polyline:
    """Накопитель одного непрерывного прохода: схлопывает коллинеарные точки."""

    __slots__ = ("out", "start", "points")

    def __init__(self, out: array.array):
        self.out = out
        self.start: tuple[float, float] | None = None
        self.points: list[tuple[float, float]] = []

    def begin(self, x: float, y: float) -> None:
        self.flush()
        self.start = (x, y)
        self.points = []

    def extend(self, x: float, y: float) -> None:
        if self.start is None:
            self.start = (x, y)
            return
        pts = self.points
        if pts:
            # Отклонение промежуточных точек от хорды start→(x, y).
            sx, sy = self.start
            dx, dy = x - sx, y - sy
            length = math.hypot(dx, dy)
            if length > 1e-9:
                tol = MERGE_TOLERANCE_MM
                for px, py in pts:
                    dev = abs((px - sx) * dy - (py - sy) * dx) / length
                    if dev > tol:
                        lx, ly = pts[-1]
                        self.out.extend((sx, sy, lx, ly))
                        self.start = (lx, ly)
                        self.points = [(x, y)]
                        return
        pts.append((x, y))

    def flush(self) -> None:
        if self.start is not None and self.points:
            lx, ly = self.points[-1]
            sx, sy = self.start
            self.out.extend((sx, sy, lx, ly))
        self.start = None
        self.points = []


class _Layer:
    __slots__ = ("index", "z", "height", "ext", "pct", "rem", "paths", "poly",
                 "bucket", "minx", "miny", "maxx", "maxy", "moves")

    def __init__(self, index: int, z: float, height: float, pct: float | None,
                 rem: float | None):
        self.index = index
        self.z = z
        self.height = height
        self.ext = 0.0
        self.pct = pct
        self.rem = rem
        self.paths: dict[str, array.array] = {}
        self.poly: _Polyline | None = None
        self.bucket = ""
        self.minx = self.miny = math.inf
        self.maxx = self.maxy = -math.inf
        self.moves = 0

    def add(self, bucket: str, x0: float, y0: float, x1: float, y1: float,
            de: float) -> None:
        if bucket != self.bucket or self.poly is None:
            if self.poly is not None:
                self.poly.flush()
            out = self.paths.get(bucket)
            if out is None:
                out = self.paths[bucket] = array.array("f")
            self.poly = _Polyline(out)
            self.poly.begin(x0, y0)
            self.bucket = bucket
        elif self.poly.start is None:
            self.poly.begin(x0, y0)
        else:
            last = self.poly.points[-1] if self.poly.points else self.poly.start
            if abs(last[0] - x0) > 1e-6 or abs(last[1] - y0) > 1e-6:
                # Между проходами был холостой ход — начинаем новую ломаную.
                self.poly.begin(x0, y0)
        self.poly.extend(x1, y1)
        self.ext += de
        self.moves += 1
        if x1 < self.minx:
            self.minx = x1
        if x1 > self.maxx:
            self.maxx = x1
        if y1 < self.miny:
            self.miny = y1
        if y1 > self.maxy:
            self.maxy = y1

    def finish(self) -> None:
        if self.poly is not None:
            self.poly.flush()
            self.poly = None
        total = sum(len(a) // 4 for a in self.paths.values())
        if total > LAYER_SEGMENT_CAP:
            # Равномерное прореживание внутри каждой корзины — форма слоя
            # остаётся узнаваемой, стенки не пропадают целиком.
            keep = LAYER_SEGMENT_CAP / float(total)
            for key, arr in list(self.paths.items()):
                n = len(arr) // 4
                want = max(1, int(n * keep))
                if want >= n:
                    continue
                step = n / float(want)
                thinned = array.array("f")
                pos = 0.0
                while pos < n:
                    i = int(pos) * 4
                    thinned.extend(arr[i:i + 4])
                    pos += step
                self.paths[key] = thinned

    @property
    def segments(self) -> int:
        return sum(len(a) // 4 for a in self.paths.values())

    def summary(self) -> dict[str, Any]:
        return {
            "i": self.index,
            "z": round(self.z, 3),
            "h": round(self.height, 3),
            "seg": self.segments,
            "ext": round(self.ext, 2),
            "pct": None if self.pct is None else round(self.pct, 1),
            "rem": None if self.rem is None else round(self.rem, 1),
            "types": {k: len(v) // 4 for k, v in self.paths.items()},
        }

    def detail(self) -> dict[str, Any]:
        return {
            "i": self.index,
            "z": round(self.z, 3),
            "h": round(self.height, 3),
            "ext": round(self.ext, 2),
            "pct": None if self.pct is None else round(self.pct, 1),
            "rem": None if self.rem is None else round(self.rem, 1),
            "paths": {k: [round(v, 2) for v in arr] for k, arr in self.paths.items()},
        }


# ----------------------------------------------------------------- парсер
def _arc_points(x0: float, y0: float, x1: float, y1: float, i: float, j: float,
                clockwise: bool) -> list[tuple[float, float]]:
    """Хорды дуги G2/G3 с центром (x0+i, y0+j): ≤ 12 отрезков."""
    cx, cy = x0 + i, y0 + j
    r = math.hypot(i, j)
    if r < 1e-6:
        return [(x1, y1)]
    a0 = math.atan2(y0 - cy, x0 - cx)
    a1 = math.atan2(y1 - cy, x1 - cx)
    sweep = a1 - a0
    if clockwise:
        if sweep >= 0:
            sweep -= 2 * math.pi
    elif sweep <= 0:
        sweep += 2 * math.pi
    if abs(x1 - x0) < 1e-6 and abs(y1 - y0) < 1e-6:
        sweep = -2 * math.pi if clockwise else 2 * math.pi
    n = max(1, min(12, int(abs(sweep) * r / 1.5) + 1))
    pts = []
    for k in range(1, n + 1):
        a = a0 + sweep * k / n
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    pts[-1] = (x1, y1)
    return pts


class LayerIndex:
    """Результат разбора одного файла G-code."""

    def __init__(self, source: str):
        self.source = source
        self.layers: list[_Layer] = []
        self.total_declared = 0
        self.lines = 0
        self.truncated = False
        self.error = ""
        self.ready = threading.Event()
        self.started_at = time.time()
        self.finished_at = 0.0
        self.minx = self.miny = math.inf
        self.maxx = self.maxy = -math.inf

    # ---- сводки -----------------------------------------------------
    @property
    def building(self) -> bool:
        return not self.ready.is_set()

    def bbox(self) -> list[float] | None:
        if self.minx is math.inf:
            return None
        return [round(self.minx, 2), round(self.miny, 2),
                round(self.maxx, 2), round(self.maxy, 2)]

    def overview(self) -> dict[str, Any]:
        building = self.building
        layers = list(self.layers)
        # Пока фоновый разбор идёт, список слоёв не отдаём: последний слой
        # ещё наполняется из другого потока. Достаточно счётчика прогресса.
        return {
            "source": self.source,
            "building": building,
            "error": self.error,
            "truncated": self.truncated,
            "lines": self.lines,
            "total": len(layers),
            "total_declared": self.total_declared,
            "bbox": None if building else self.bbox(),
            "buckets": [{"key": k, "label": BUCKET_LABELS[k]} for k in BUCKETS],
            "layers": [] if building else [layer.summary() for layer in layers],
            "seconds": round((self.finished_at or time.time()) - self.started_at, 2),
        }

    def layer(self, number: int) -> _Layer | None:
        if self.building:
            return None
        if 1 <= number <= len(self.layers):
            return self.layers[number - 1]
        return None

    # ---- разбор -----------------------------------------------------
    def build(self, lines: Iterable[str]) -> None:
        try:
            self._build(lines)
        except Exception as exc:  # pragma: no cover - защитный барьер
            self.error = f"{type(exc).__name__}: {exc}"
        finally:
            for layer in self.layers:
                layer.finish()
            self.finished_at = time.time()
            self.ready.set()

    def _new_layer(self, z: float, height: float, pct, rem) -> _Layer:
        if self.layers:
            self.layers[-1].finish()
        layer = _Layer(len(self.layers) + 1, z, height, pct, rem)
        self.layers.append(layer)
        return layer

    def _build(self, lines: Iterable[str]) -> None:
        x = y = z = 0.0
        e = 0.0
        absolute_xyz = True
        absolute_e = True
        markers = False           # видели ли маркеры слоёв
        pending_z: float | None = None
        pending_h: float | None = None
        pct: float | None = None
        rem: float | None = None
        bucket = "other"
        layer: _Layer | None = None
        pending_layer = False
        count = 0
        for raw in lines:
            count += 1
            if count > MAX_LINES:
                self.truncated = True
                break
            line = raw.strip()
            if not line:
                continue
            c0 = line[0]
            if c0 == ";":
                # --- маркеры слайсера --------------------------------
                # Bambu Studio пишет «; CHANGE_LAYER», «; Z_HEIGHT: », «; FEATURE: »,
                # Orca/Prusa — «;LAYER_CHANGE», «;Z:», «;TYPE:», Cura — «;LAYER:n».
                tag = line[1:].lstrip()
                if tag.startswith(("LAYER_CHANGE", "CHANGE_LAYER")):
                    markers = True
                    pending_layer = True
                    pending_z = pending_h = None
                elif tag.startswith("Z:") or tag.startswith("Z_HEIGHT:"):
                    try:
                        pending_z = float(tag.split(":", 1)[1].strip())
                    except ValueError:
                        pass
                elif tag.startswith("HEIGHT:") or tag.startswith("LAYER_HEIGHT:"):
                    try:
                        pending_h = float(tag.split(":", 1)[1].strip())
                    except ValueError:
                        pass
                elif tag.startswith("TYPE:") or tag.startswith("FEATURE:"):
                    bucket = bucket_of(tag.split(":", 1)[1])
                elif "layer num/total_layer_count" in tag:
                    m = _LAYER_NUM_RE.search(tag)
                    if m:
                        markers = True
                        self.total_declared = max(self.total_declared, int(m.group(2)))
                        if not pending_layer and (layer is None
                                                  or layer.index != int(m.group(1))):
                            pending_layer = True
                            pending_z = pending_h = None
                elif tag.startswith("LAYER:"):
                    m = _CURA_LAYER_RE.match(";" + tag)
                    if m:
                        markers = True
                        pending_layer = True
                        pending_z = pending_h = None
                elif tag.startswith("LAYER_COUNT:"):
                    try:
                        self.total_declared = max(self.total_declared,
                                                  int(tag[12:].strip()))
                    except ValueError:
                        pass
                continue
            if c0 not in "GMgm":
                continue
            code_end = line.find(" ")
            code = (line if code_end < 0 else line[:code_end]).upper()
            if code in ("G1", "G0", "G2", "G3"):
                nx, ny, nz, ne = x, y, z, None
                ai = aj = None
                for tok in line[code_end + 1:].split(" ") if code_end > 0 else ():
                    if not tok:
                        continue
                    if tok[0] == ";":
                        break
                    k = tok[0]
                    try:
                        val = float(tok[1:])
                    except ValueError:
                        continue
                    if k in "Xx":
                        nx = val if absolute_xyz else x + val
                    elif k in "Yy":
                        ny = val if absolute_xyz else y + val
                    elif k in "Zz":
                        nz = val if absolute_xyz else z + val
                    elif k in "Ee":
                        ne = val
                    elif k in "Ii":
                        ai = val
                    elif k in "Jj":
                        aj = val
                de = 0.0
                if ne is not None:
                    de = ne - e if absolute_e else ne
                    e = ne if absolute_e else e + ne
                extruding = de > 0 and (nx != x or ny != y)
                if extruding:
                    if pending_layer or layer is None or (
                            not markers and nz > layer.z + 1e-6):
                        lz = pending_z if pending_z is not None else nz
                        lh = pending_h if pending_h is not None else (
                            lz - layer.z if layer is not None and lz > layer.z else lz)
                        layer = self._new_layer(lz, max(lh, 0.0), pct, rem)
                        if len(self.layers) >= MAX_LAYERS:
                            self.truncated = True
                            break
                        pending_layer = False
                        pending_z = pending_h = None
                    if code in ("G2", "G3") and ai is not None and aj is not None:
                        px, py = x, y
                        for qx, qy in _arc_points(x, y, nx, ny, ai, aj, code == "G2"):
                            layer.add(bucket, px, py, qx, qy, 0.0)
                            px, py = qx, qy
                        layer.ext += de
                    else:
                        layer.add(bucket, x, y, nx, ny, de)
                    if nx < self.minx:
                        self.minx = nx
                    if nx > self.maxx:
                        self.maxx = nx
                    if ny < self.miny:
                        self.miny = ny
                    if ny > self.maxy:
                        self.maxy = ny
                x, y, z = nx, ny, nz
            elif code == "G90":
                # Режим экструдера задают только M82/M83: слайсеры пишут их
                # явно, а Bambu/Prusa ставят M83 и до, и после G90.
                absolute_xyz = True
            elif code == "G91":
                absolute_xyz = False
            elif code == "M82":
                absolute_e = True
            elif code == "M83":
                absolute_e = False
            elif code == "G92":
                for tok in line[code_end + 1:].split(" ") if code_end > 0 else ():
                    if tok[:1] in ("E", "e"):
                        try:
                            e = float(tok[1:])
                        except ValueError:
                            pass
                    elif tok[:1] in ("X", "x"):
                        try:
                            x = float(tok[1:])
                        except ValueError:
                            pass
                    elif tok[:1] in ("Y", "y"):
                        try:
                            y = float(tok[1:])
                        except ValueError:
                            pass
                    elif tok[:1] in ("Z", "z"):
                        try:
                            z = float(tok[1:])
                        except ValueError:
                            pass
            elif code == "M73":
                for m in _M73_RE.finditer(line[code_end + 1:] if code_end > 0 else ""):
                    if m.group(1) is not None:
                        pct = float(m.group(1))
                    elif m.group(2) is not None:
                        rem = float(m.group(2))
                if layer is not None and layer.moves == 0:
                    # M73 стоит перед первым ходом слоя — это прогресс слоя.
                    layer.pct, layer.rem = pct, rem
        self.lines = count
        if layer is not None and layer.pct is None:
            layer.pct, layer.rem = pct, rem


# ------------------------------------------------------------------ файлы
def gcode_member(path: Path, plate: int | None = None) -> str | None:
    """Имя члена 3MF с G-code нужной плиты (или первой попавшейся)."""
    try:
        with zipfile.ZipFile(path) as zf:
            names = [n for n in zf.namelist() if _PLATE_MEMBER_RE.match(n)]
    except (OSError, zipfile.BadZipFile):
        return None
    if not names:
        return None
    if plate:
        for name in names:
            m = _PLATE_MEMBER_RE.match(name)
            if m and int(m.group(1)) == int(plate):
                return name
    names.sort(key=lambda n: int(_PLATE_MEMBER_RE.match(n).group(1)))
    return names[0]


def iter_lines(path: Path, member: str | None = None) -> Iterator[str]:
    """Строки G-code потоково: из файла или из члена 3MF."""
    if member:
        with zipfile.ZipFile(path) as zf, zf.open(member) as raw:
            with io.TextIOWrapper(raw, encoding="utf-8", errors="replace") as text:
                yield from text
        return
    with open(path, "r", encoding="utf-8", errors="replace") as text:
        yield from text


def resolve_job_file(filename: str, roots: Iterable[Path]) -> Path | None:
    """Локальная копия файла задания: только внутри разрешённых каталогов."""
    name = Path(str(filename or "").replace("\\", "/")).name
    if not name:
        return None
    for root in roots:
        try:
            base = Path(root).resolve()
            candidate = (base / name).resolve()
            candidate.relative_to(base)
        except (OSError, ValueError):
            continue
        if candidate.is_file():
            return candidate
    return None


# ------------------------------------------------------------------- кэш
_CACHE: "OrderedDict[tuple, LayerIndex]" = OrderedDict()
_CACHE_LOCK = threading.Lock()


def _cache_key(path: Path, member: str | None) -> tuple:
    st = path.stat()
    return (str(path), int(st.st_mtime_ns), int(st.st_size), member or "")


def index_for(path: Path, plate: int | None = None,
              wait: float = 0.0) -> LayerIndex | None:
    """Индекс файла из кэша; при промахе — запуск разбора в фоне.

    ``wait`` — сколько секунд подождать готовности (малые файлы успевают за
    десятки миллисекунд, и первый ответ уже полный).
    """
    path = Path(path)
    if not path.is_file():
        return None
    member = None
    if path.suffix.lower() == ".3mf":
        member = gcode_member(path, plate)
        if member is None:
            return None
    try:
        key = _cache_key(path, member)
    except OSError:
        return None
    with _CACHE_LOCK:
        idx = _CACHE.get(key)
        if idx is not None:
            _CACHE.move_to_end(key)
        else:
            idx = LayerIndex(f"{path.name}:{member}" if member else path.name)
            _CACHE[key] = idx
            while len(_CACHE) > CACHE_SIZE:
                _CACHE.popitem(last=False)
            worker = threading.Thread(
                target=idx.build, args=(iter_lines(path, member),),
                name="pf-gcode-layers", daemon=True)
            worker.start()
    if wait > 0:
        idx.ready.wait(wait)
    return idx


def clear_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


def parse_text(text: str) -> LayerIndex:
    """Синхронный разбор строки G-code (тесты и мастер подготовки)."""
    idx = LayerIndex("text")
    idx.build(io.StringIO(text))
    return idx
