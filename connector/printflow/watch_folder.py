"""Watch Folder — авто-импорт 3MF из папки Bambu Studio.

Следит за каталогом ~/PrintFlow-Inbox (настраивается).
Новый .3mf → estimate + thumbnails + plates → событие /api/slicer/push
→ создание черновика заказа или добавление в очередь.

Работает polling-ом 3 сек + inotify там где доступно (Linux).
Не требует внешних зависимостей.
"""
from __future__ import annotations

import hashlib
import re
import shutil
import threading
import time
from pathlib import Path

from .config import UPLOAD_DIR, now_iso
from .farmloop import BEGIN as FARMLOOP_BEGIN

DEFAULT_WATCH = Path.home() / "PrintFlow-Inbox"

ORDER_RE = re.compile(r"[№#](\d{2,6})")
GCODE_ORDER_RE = re.compile(r"PrintFlow-order\s*[:=]\s*(\d+)")


def pick_warehouse_spool(db, material: str) -> dict | None:
    mat = str(material or "").strip()
    if not mat:
        return None
    rows = db.query("SELECT * FROM spools WHERE archived=0")
    candidates = [
        row for row in rows
        if str(row.get("material") or "").strip().lower() == mat.lower()
        and float(row.get("remaining_grams") or 0) > 0
    ]
    if not candidates:
        return None

    def rank(row: dict):
        in_ams = 1 if str(row.get("ams_slot") or "").strip() else 0
        verified = 1 if row.get("verified") else 0
        return (in_ams, verified, float(row.get("remaining_grams") or 0))

    return sorted(candidates, key=rank, reverse=True)[0]


class WatchFolder:
    def __init__(self, db, manager=None, bus=None):
        self.db = db
        self.manager = manager
        self.bus = bus
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._seen: dict[str, float] = {}
        self._pending: dict[str, dict] = {}

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="pf-watch-folder", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _watch_path(self) -> Path:
        try:
            p = Path(str(self.db.setting("watch_folder_path", str(DEFAULT_WATCH)))).expanduser()
            return p
        except Exception:
            return DEFAULT_WATCH

    def _enabled(self) -> bool:
        return bool(self.db.setting("watch_folder_enabled", False))

    def _loop(self):
        while not self._stop.wait(3):
            try:
                if not self._enabled():
                    continue
                self._scan()
            except Exception:
                continue

    def _scan(self):
        watch = self._watch_path()
        if not watch.exists():
            return
        for path in watch.glob("*.3mf"):
            try:
                st = path.stat()
                mtime = st.st_mtime
                size = st.st_size
            except OSError:
                continue
            if time.time() - mtime < 2:
                continue
            key = str(path.resolve())
            if self._seen.get(key) == mtime:
                continue
            self._seen[key] = mtime
            time.sleep(0.5)
            try:
                if path.stat().st_size != size:
                    continue
            except OSError:
                continue
            self._handle_file(path)

        for path in watch.glob("*.gcode"):
            try:
                st = path.stat()
                mtime = st.st_mtime
            except OSError:
                continue
            if time.time() - mtime < 2:
                continue
            key = str(path.resolve())
            if self._seen.get(key) == mtime:
                continue
            self._seen[key] = mtime
            self._handle_file(path)

        if len(self._seen) > 500:
            self._seen = dict(list(self._seen.items())[-300:])

    def _handle_file(self, path: Path):
        try:
            from .estimate import parse_3mf_complete, _read_head
        except ImportError:
            return
        info: dict = {"file": str(path), "name": path.name, "size": path.stat().st_size if path.exists() else 0}
        try:
            if path.suffix.lower() == ".3mf":
                detail = parse_3mf_complete(path)
                est = {}
                if detail.get("plates"):
                    total_g = round(sum(p.get("grams", 0) for p in detail["plates"]), 1)
                    total_m = round(sum(p.get("minutes", 0) for p in detail["plates"]), 1)
                    first = detail["plates"][0]
                    est = dict(first)
                    est["total_grams"] = total_g
                    est["total_minutes"] = total_m
                    est["plates"] = detail["plates"]
                    est["plate_count"] = len(detail["plates"])
                    est["thumbnails"] = {k: v[:120] + "..." if len(v) > 120 else v for k, v in detail.get("thumbnails", {}).items()}
                    info["thumbnails_full"] = detail.get("thumbnails", {})
                info.update(detail)
                info.update(est)
            else:
                text = _read_head(path)
                if text:
                    from .estimate import _parse_gcode_head as _pg
                    info.update(_pg(text))
                    info["farmloop"] = FARMLOOP_BEGIN in text
        except Exception as exc:
            info["error"] = str(exc)

        order_id = (self._find_order_id(path.name, info)
                    if self.db.setting("watch_link_order", True) else "")
        info["order_id"] = order_id
        info["at"] = now_iso()

        try:
            UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
            dest = UPLOAD_DIR / path.name
            if not dest.exists():
                shutil.copy2(path, dest)
            info["upload_path"] = str(dest)
        except Exception:
            pass

        fid = f"wf_{int(time.time()*1000)}"
        self._pending[fid] = info
        if len(self._pending) > 50:
            oldest = sorted(self._pending.keys())[:10]
            for k in oldest:
                self._pending.pop(k, None)

        action = str(self.db.setting("watch_auto_action", "notify"))
        if action not in {"notify", "queue"}:
            action = "notify"
        self.db.add_event("watch", "Новый файл из Bambu Studio", f"{path.name} · {info.get('total_grams') or info.get('grams') or 0}г · {info.get('total_minutes') or info.get('minutes') or 0}мин", "", {"file": path.name, "order_id": order_id, "action": action, "fid": fid})
        if self.bus:
            try:
                self.bus.publish("watch", {"file": path.name, "order_id": order_id, "info": {k: v for k, v in info.items() if k != "thumbnails_full"}, "fid": fid})
                if info.get("thumbnails_full"):
                    self.bus.publish("watch_thumb", {"fid": fid, "thumbnails": info["thumbnails_full"]})
            except Exception:
                pass

        watch_create = bool(self.db.setting("watch_create_order", False) or self.db.setting("slicer_auto_create_order", False))
        if watch_create and not order_id:
            try:
                order = self._create_order_from_info(path.name, info)
                info["created_order_id"] = order.get("id")
                info["created_order_number"] = order.get("number")
                self.db.add_event("watch", "Создан черновик заказа из 3MF", f"№{order.get('number')} · {path.name}", "", {"order_id": order["id"]})
                order_id = order["id"]
                info["order_id"] = order_id
            except Exception:
                pass

        if action == "queue":
            ok, reason = self._enqueue(path.name, info, order_id)
            info["queue_ok"] = ok
            if not ok:
                info["queue_error"] = reason
                self.db.add_event("watch", "Файл не попал в очередь",
                                  f"{path.name} · {reason}", "",
                                  {"file": path.name, "order_id": order_id, "fid": fid})
                if self.bus:
                    try:
                        self.bus.publish("watch", {"file": path.name, "order_id": order_id,
                                                   "fid": fid, "error": reason})
                    except Exception as exc:
                        self.db.add_event("watch", "Сбой уведомления об очереди",
                                          str(exc), "", {"file": path.name, "fid": fid})

        # оригинал файла остаётся в watch-папке; папка processed нужна для явного переноса.
        try:
            processed = self._watch_path() / "processed"
            processed.mkdir(exist_ok=True)
        except Exception:
            pass

    def _find_order_id(self, filename: str, info: dict) -> str:
        m = ORDER_RE.search(filename)
        if m:
            num = m.group(1)
            row = self.db.one("SELECT id FROM orders WHERE number=?", (num,))
            if row:
                return row["id"]
            row = self.db.one("SELECT id FROM orders WHERE number LIKE ?", (f"%{num}%",))
            if row:
                return row["id"]
        try:
            g = info.get("project_settings", {}).get("raw", "") if isinstance(info.get("project_settings"), dict) else ""
            m2 = GCODE_ORDER_RE.search(str(g))
            if m2:
                row = self.db.one("SELECT id FROM orders WHERE number=?", (m2.group(1),))
                if row:
                    return row["id"]
        except Exception:
            pass
        return ""

    def _create_order_from_info(self, filename: str, info: dict) -> dict:
        from .repo import Repo
        repo = Repo(self.db)
        product = Path(filename).stem.replace("_", " ").strip()[:80] or "Изделие из Bambu Studio"
        grams = info.get("total_grams") or info.get("grams") or 0
        minutes = info.get("total_minutes") or info.get("minutes") or 0
        hours = round(minutes / 60, 2) if minutes else 0
        material = info.get("material") or (info.get("filaments", [{}])[0].get("type") if info.get("filaments") else "") or ""
        color = info.get("color") or ""
        return repo.save_order({
            "product": product,
            "material": material,
            "color": color,
            "grams": grams,
            "hours": hours,
            "qty": 1,
            "file": filename,
            "status": "new",
            "notes": f"Авто из 3MF: {filename}",
            "channel": "shop",
        })

    def _file_hash(self, filename: str) -> str:
        try:
            candidates = []
            try:
                candidates.append(self._watch_path() / filename)
            except Exception:
                pass
            candidates.append(UPLOAD_DIR / filename)
            for cand in candidates:
                try:
                    if cand.exists() and cand.stat().st_size > 0:
                        h = hashlib.sha256()
                        with open(cand, "rb") as f:
                            for chunk in iter(lambda: f.read(1024*1024), b""):
                                h.update(chunk)
                        return h.hexdigest()[:16]
                except Exception:
                    continue
        except Exception:
            pass
        return ""

    def _enqueue(self, filename: str, info: dict, order_id: str) -> tuple[bool, str]:
        """Поставить файл в очередь печати, вернув (получилось, причина).

        18.12+: Watch+AMS (13) + persistence (12) через spool_mapping_repo.
        """
        if not self.manager:
            return False, "нет подключения к менеджеру печати"

        file_hash = self._file_hash(filename)
        ams_mapping: list[int] = []
        printer_id_for_map = ""

        try:
            import json as _json
            # 12: spool_mapping_repo
            if file_hash:
                try:
                    from .spool_mapping_repo import load_mapping
                    loaded = load_mapping(self.db, file_hash, "")
                    if loaded:
                        ams_mapping = loaded
                except Exception:
                    pass
            # legacy setting fallback
            if not ams_mapping:
                raw = self.db.setting(f"ams_map_{filename}", "")
                if raw:
                    try:
                        ams_mapping = _json.loads(raw) if isinstance(raw, str) else raw
                    except Exception:
                        ams_mapping = []
            # auto-map if still empty
            if not ams_mapping and self.manager:
                try:
                    from .estimate import auto_ams_map
                    filaments = []
                    if info.get("filaments"):
                        filaments = info["filaments"]
                    elif info.get("material"):
                        filaments = [{"type": info.get("material"), "color": info.get("color_hex") or "#CCCCCC"}]
                    if filaments:
                        printers = list(getattr(self.manager, "printers", {}).values())
                        for pr in printers:
                            try:
                                snap = pr.snapshot()
                                trays = (snap.get("ams") or {}).get("trays", [])
                                if trays:
                                    ams_mapping = auto_ams_map(filaments, trays)
                                    printer_id_for_map = pr.id
                                    break
                            except Exception:
                                continue
                except Exception:
                    pass
        except Exception:
            ams_mapping = []

        payload = {
            "file": filename,
            "name": Path(filename).stem,
            "order_id": order_id,
            "plate": 1,
            "use_ams": True,
            "ams_mapping": ams_mapping,
        }
        # spool auto-pick by material
        try:
            mat = str(info.get("material") or "").strip()
            if mat:
                from .accounting import Accounting
                acc = Accounting(self.db)
                # pick_spool by material
                spool = acc.pick_spool(material=mat)
                if spool:
                    payload["spool_id"] = spool["id"]
                    payload["material"] = mat
        except Exception:
            pass

        try:
            mat = str(info.get("material") or "").strip()
            if mat:
                from .accounting import Accounting
                acc = Accounting(self.db)
                spool = acc.pick_spool(material=mat)
                if spool:
                    payload["spool_id"] = spool["id"]
                    payload["material"] = mat
        except Exception:
            pass

        try:
            result = self.manager.enqueue(payload)
        except Exception as exc:
            return False, str(exc) or type(exc).__name__
        if isinstance(result, dict) and (result.get("error") or result.get("ok") is False):
            return False, str(result.get("error") or "менеджер отклонил файл")

        try:
            if ams_mapping:
                import json as _json
                self.db.set_setting(f"ams_map_{filename}", _json.dumps(ams_mapping))
                if file_hash:
                    from .spool_mapping_repo import save_mapping
                    save_mapping(self.db, file_hash, filename, printer_id_for_map, ams_mapping)
        except Exception:
            pass
        return True, ""

    def list_pending(self, limit: int = 20) -> list[dict]:
        items = sorted(self._pending.items(), key=lambda kv: kv[1].get("at", ""), reverse=True)[:limit]
        out = []
        for fid, it in items:
            cp = {k: v for k, v in it.items() if k not in ("thumbnails_full",)}
            cp["fid"] = fid
            out.append(cp)
        return out

    def get_pending(self, fid: str) -> dict | None:
        return self._pending.get(fid)

    def dismiss(self, fid: str):
        self._pending.pop(fid, None)
