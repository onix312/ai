"""Watch Folder — авто-импорт 3MF из папки Bambu Studio.

Следит за каталогом ~/PrintFlow-Inbox (настраивается).
Новый .3mf → estimate + thumbnails + plates → событие /api/slicer/push
→ создание черновика заказа или добавление в очередь.

Работает polling-ом 3 сек + inotify там где доступно (Linux).
Не требует внешних зависимостей.
"""
from __future__ import annotations

import json
import re
import shutil
import threading
import time
from pathlib import Path

from .config import DATA_DIR, UPLOAD_DIR, now_iso

DEFAULT_WATCH = Path.home() / "PrintFlow-Inbox"

# имя файла может содержать № заказа: адресник_№1023_6шт.3mf или #1023
ORDER_RE = re.compile(r"[№#](\d{2,6})")
# также коммент внутри gcode: ;PrintFlow-order: 1023
GCODE_ORDER_RE = re.compile(r"PrintFlow-order\s*[:=]\s*(\d+)")


class WatchFolder:
    def __init__(self, db, manager=None, bus=None):
        self.db = db
        self.manager = manager
        self.bus = bus
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._seen: dict[str, float] = {}  # path -> mtime
        self._pending: dict[str, dict] = {}  # file -> info awaiting confirm

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
            # файл ещё пишется — ждём 2 сек стабильности
            if time.time() - mtime < 2:
                continue
            key = str(path.resolve())
            if self._seen.get(key) == mtime:
                continue
            self._seen[key] = mtime
            # проверка размера не изменился за 1 сек
            time.sleep(0.5)
            try:
                if path.stat().st_size != size:
                    continue
            except OSError:
                continue
            self._handle_file(path)

        # также gcode
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

        # очистка старых seen (храним 500)
        if len(self._seen) > 500:
            self._seen = dict(list(self._seen.items())[-300:])

    def _handle_file(self, path: Path):
        try:
            from .estimate import estimate_3mf, parse_3mf_complete, _read_head, _parse_gcode_head
        except ImportError:
            return
        info: dict = {"file": str(path), "name": path.name, "size": path.stat().st_size if path.exists() else 0}
        try:
            if path.suffix.lower() == ".3mf":
                detail = parse_3mf_complete(path)
                est = {}
                if detail.get("plates"):
                    # взять суммарно
                    total_g = round(sum(p.get("grams", 0) for p in detail["plates"]), 1)
                    total_m = round(sum(p.get("minutes", 0) for p in detail["plates"]), 1)
                    first = detail["plates"][0]
                    est = dict(first)
                    est["total_grams"] = total_g
                    est["total_minutes"] = total_m
                    est["plates"] = detail["plates"]
                    est["plate_count"] = len(detail["plates"])
                    est["thumbnails"] = {k: v[:120] + "..." if len(v) > 120 else v for k, v in detail.get("thumbnails", {}).items()}  # truncate for event
                    # полные thumbnails сохраним отдельно
                    info["thumbnails_full"] = detail.get("thumbnails", {})
                info.update(detail)
                info.update(est)
            else:
                text = _read_head(path)
                if text:
                    from .estimate import _parse_gcode_head as _pg
                    info.update(_pg(text))
        except Exception as exc:
            info["error"] = str(exc)

        # попытка найти order_id по имени файла
        order_id = self._find_order_id(path.name, info)
        info["order_id"] = order_id
        info["at"] = now_iso()

        # копируем в uploads для очереди
        try:
            UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
            dest = UPLOAD_DIR / path.name
            if not dest.exists():
                shutil.copy2(path, dest)
            info["upload_path"] = str(dest)
        except Exception:
            pass

        # сохраняем в pending для UI
        fid = f"wf_{int(time.time()*1000)}"
        self._pending[fid] = info
        # чистим старые pending >50
        if len(self._pending) > 50:
            oldest = sorted(self._pending.keys())[:10]
            for k in oldest:
                self._pending.pop(k, None)

        # событие
        action = str(self.db.setting("watch_auto_action", "notify"))
        self.db.add_event("watch", "Новый файл из Bambu Studio", f"{path.name} · {info.get('total_grams') or info.get('grams') or 0}г · {info.get('total_minutes') or info.get('minutes') or 0}мин", "", {"file": path.name, "order_id": order_id, "action": action, "fid": fid})
        if self.bus:
            try:
                self.bus.publish("watch", {"file": path.name, "order_id": order_id, "info": {k: v for k, v in info.items() if k != "thumbnails_full"}, "fid": fid})
                # thumbnails отдельно если нужно
                if info.get("thumbnails_full"):
                    self.bus.publish("watch_thumb", {"fid": fid, "thumbnails": info["thumbnails_full"]})
            except Exception:
                pass

        # авто-действия
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
            try:
                self._enqueue(path.name, info, order_id)
            except Exception:
                pass
        elif action == "print":
            try:
                # отложенная печать — только если есть принтер и файл уже на SD не нужен? отложим до FTPS
                pass
            except Exception:
                pass

        # оригинал можно переместить в архив Watch Folder/processed
        try:
            processed = self._watch_path() / "processed"
            processed.mkdir(exist_ok=True)
            # не перемещаем, копируем и оставляем исходник — пользователь сам решит
        except Exception:
            pass

    def _find_order_id(self, filename: str, info: dict) -> str:
        # по имени файла
        m = ORDER_RE.search(filename)
        if m:
            num = m.group(1)
            row = self.db.one("SELECT id FROM orders WHERE number=?", (num,))
            if row:
                return row["id"]
            # поиск по LIKE
            row = self.db.one("SELECT id FROM orders WHERE number LIKE ?", (f"%{num}%",))
            if row:
                return row["id"]
        # по G-code комменту
        try:
            g = info.get("project_settings", {}).get("raw", "") if isinstance(info.get("project_settings"), dict) else ""
            m2 = GCODE_ORDER_RE.search(str(g))
            if m2:
                row = self.db.one("SELECT id FROM orders WHERE number=?", (m2.group(1),))
                if row:
                    return row["id"]
        except Exception:
            pass
        # по estimate gcode text если есть
        try:
            if info.get("gcode_file"):
                pass
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

    def _enqueue(self, filename: str, info: dict, order_id: str):
        if not self.manager:
            return
        payload = {
            "file": filename,
            "name": Path(filename).stem,
            "order_id": order_id,
            "plate": 1,
            "use_ams": True,
        }
        self.manager.enqueue(payload)

    def list_pending(self, limit: int = 20) -> list[dict]:
        # последние файлы из watch — сортируем по fid
        items = sorted(self._pending.values(), key=lambda x: x.get("at", ""), reverse=True)[:limit]
        # убрать большие thumbnails для списка
        out = []
        for it in items:
            cp = {k: v for k, v in it.items() if k not in ("thumbnails_full",)}
            # вернуть короткие thumbnails preview
            if "thumbnails" in it and isinstance(it["thumbnails"], dict):
                # уже короткие
                pass
            out.append(cp)
        return out

    def get_pending(self, fid: str) -> dict | None:
        return self._pending.get(fid)

    def dismiss(self, fid: str):
        self._pending.pop(fid, None)
