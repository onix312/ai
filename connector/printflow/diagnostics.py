"""Самодиагностика PrintFlow (идея 12).

Один ответ на вопрос «а что у меня вообще происходит?»: живые потоки и их
возраст, размер базы и WAL, последний бэкап, версия схемы и приложения,
ошибки за сутки, состояние фоновых сервисов (боты, облачный мост,
планировщик, обновление).

Раньше это собиралось из трёх мест: `pf doctor` в CLI, карточка «Здоровье»
в настройках и ручное чтение connector.log. Здесь — один источник правды,
который дёргают и панель (`/api/diagnostics`), и бот, и CI.

Никаких секретов: токены и access-коды в ответ не попадают, только признак
«настроен / не настроен».
"""
from __future__ import annotations

import platform
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any

from . import APP_VERSION
from .config import DATA_DIR, DB_FILE, LOG_FILE

# Потоки, которые обязаны жить, когда включена соответствующая подсистема.
# Имя → человекочитаемая подпись.
EXPECTED_THREADS = {
    "pf-http": "HTTP-сервер",
    "pf-manager": "Менеджер парка",
    "pf-live": "Рассылка телеметрии (SSE)",
    "pf-bot": "Telegram-бот сотрудников",
    "pf-client-bot": "Клиентский бот",
    "pf-cloud-bridge": "Облачный мост Bambu",
    "pf-watch-folder": "Watch Folder",
    "pf-updater": "Проверка обновлений",
    "pf-camera": "Камера принтера",
    "pf-virtual": "Виртуальный принтер",
    "pf-scheduler": "Планировщик задач",
}


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size if path.exists() else 0
    except OSError:
        return 0


def _database_stats() -> dict:
    stats = {
        "path": str(DB_FILE),
        "exists": DB_FILE.exists(),
        "size": _file_size(DB_FILE),
        "wal_size": _file_size(DB_FILE.with_name(DB_FILE.name + "-wal")),
        "journal_mode": "",
        "integrity": "",
        "schema_version": 0,
        "tables": 0,
    }
    if not stats["exists"]:
        return stats
    try:
        # Отдельное соединение в режиме только чтения: диагностика не должна
        # ни блокировать запись, ни зависеть от состояния основного пула.
        conn = sqlite3.connect(f"file:{DB_FILE}?mode=ro", uri=True, timeout=2.0)
        try:
            stats["journal_mode"] = str(
                conn.execute("PRAGMA journal_mode").fetchone()[0] or "")
            stats["schema_version"] = int(
                conn.execute("PRAGMA user_version").fetchone()[0] or 0)
            stats["tables"] = int(conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0])
        finally:
            conn.close()
    except sqlite3.Error as exc:
        stats["integrity"] = f"ошибка чтения: {exc}"
    return stats


def _thread_stats() -> dict:
    alive = {t.name: t for t in threading.enumerate()}
    known = []
    for name, label in EXPECTED_THREADS.items():
        prefix_hits = [n for n in alive if n == name or n.startswith(name + "-")]
        known.append({
            "name": name, "label": label, "alive": bool(prefix_hits),
            "count": len(prefix_hits),
        })
    return {
        "total": len(alive),
        "names": sorted(alive),
        "expected": known,
        "missing": [item["label"] for item in known if not item["alive"]],
    }


def _error_scan(hours: int = 24) -> dict:
    """Сколько ошибок в логе за период. Читаем хвост файла, не весь файл."""
    result = {"file": str(LOG_FILE), "exists": LOG_FILE.exists(),
              "size": _file_size(LOG_FILE), "errors": 0, "last": []}
    if not result["exists"]:
        return result
    try:
        with LOG_FILE.open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(max(0, result["size"] - 512 * 1024))
            tail = handle.read().splitlines()
        cutoff = time.time() - hours * 3600
        last = []
        for line in tail[-2000:]:
            lowered = line.lower()
            if '"level": "error"' in lowered or " error " in lowered or "traceback" in lowered:
                result["errors"] += 1
                last.append(line[-400:])
        result["last"] = last[-5:]
        result["window_hours"] = hours
        result["scanned_at"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(cutoff))
    except OSError as exc:
        result["error"] = str(exc)
    return result


def _backup_stats() -> dict:
    from .db import list_backups
    try:
        backups = list_backups()
    except Exception as exc:  # каталог бэкапов может быть недоступен
        return {"count": 0, "last": "", "error": str(exc)}
    last = backups[0] if backups else {}
    return {
        "count": len(backups),
        "last": last.get("name", ""),
        "last_at": last.get("at", ""),
        "last_size": last.get("size", 0),
    }


def collect(api=None, *, hours: int = 24) -> dict:
    """Собрать снимок состояния системы."""
    from .db import SCHEMA_VERSION
    report: dict = {
        "ok": True,
        "version": APP_VERSION,
        "schema": {"current": SCHEMA_VERSION, **{
            k: v for k, v in _database_stats().items() if k == "schema_version"}},
        "python": sys.version.split()[0],
        "platform": f"{platform.system()} {platform.release()}".strip(),
        "uptime_sec": round(time.time() - (getattr(api, "started_at", None) or time.time())),
        "data_dir": str(DATA_DIR),
        "database": _database_stats(),
        "threads": _thread_stats(),
        "errors": _error_scan(hours),
        "backups": _backup_stats(),
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    report["schema"]["actual"] = report["database"].get("schema_version", 0)
    report["schema"]["matches"] = report["schema"]["actual"] in (0, SCHEMA_VERSION)

    if api is not None:
        settings = {}
        try:
            settings = api.db.settings()
        except Exception:
            pass
        report["services"] = {
            "telegram_bot": bool(settings.get("telegram_token")),
            "client_bot": bool(settings.get("client_bot_token")),
            "bambu_cloud": bool(settings.get("cloud_token")),
            "studio_gateway": bool(settings.get("studio_gateway_enabled")),
            "automation_rules": _rule_count(api),
        }
        try:
            manager = getattr(api, "manager", None)
            printers = manager.snapshot().get("printers", []) if manager else []
            report["farm"] = {
                "printers": len(printers),
                "online": sum(1 for p in printers if (p.get("connection") or {}).get("connected")),
                "printing": sum(1 for p in printers
                                if (p.get("printer") or {}).get("state") in
                                ("RUNNING", "PAUSE", "PREPARE", "SLICING")),
            }
            report["queue"] = len(manager.queue()) if manager else 0
        except Exception as exc:
            report["farm"] = {"error": str(exc)}
        report["router"] = _router_stats()
        report["outbox"] = _outbox_stats(api.db)
    report["ok"] = not report["threads"]["missing"] and report["schema"]["matches"]
    return report


def _rule_count(api) -> int:
    try:
        return int(api.db.one(
            "SELECT COUNT(*) AS n FROM automation_rules WHERE enabled=1")["n"])
    except Exception:
        return 0


def _outbox_stats(db) -> dict:
    """Глубина очередей исходящих Telegram (Н11).

    Это главный признак «бот молчит»: сообщения копятся, когда токен мёртв,
    Telegram троттлит или сеть лежит. Раньше глубина была видна только
    внутри `Outbox.stats()`, то есть не видна нигде.
    """
    out: dict[str, Any] = {}
    for label, table in (("staff", "telegram_outbox"), ("client", "client_bot_outbox")):
        try:
            row = db.one(
                f"SELECT COUNT(*) AS pending,"
                " SUM(CASE WHEN state='failed' THEN 1 ELSE 0 END) AS failed,"
                " SUM(CASE WHEN attempts>0 THEN 1 ELSE 0 END) AS retried,"
                " MAX(last_error) AS last_error"
                f" FROM {table} WHERE state!='sent'") or {}
            out[label] = {
                "pending": int(row.get("pending") or 0),
                "failed": int(row.get("failed") or 0),
                "retried": int(row.get("retried") or 0),
                "last_error": str(row.get("last_error") or "")[:200],
            }
        except Exception as exc:   # таблица отсутствует в базе до 14.0
            out[label] = {"pending": 0, "error": str(exc)[:120]}
    out["total_pending"] = sum(v.get("pending", 0) for v in out.values()
                               if isinstance(v, dict))
    return out


def _router_stats() -> dict:
    try:
        from .router import router
        routes = router.routes()
        return {
            "registered": len(routes),
            "public": sum(1 for r in routes if r.public),
            "audited": sum(1 for r in routes if r.audit),
            "idempotent": sum(1 for r in routes if r.idempotent),
        }
    except Exception as exc:
        return {"error": str(exc)}


def human_report(report: dict) -> str:
    """Текстовая версия для Telegram-бота и `pf doctor`."""
    lines = [
        f"PrintFlow {report.get('version')} · схема {report['schema'].get('actual')}",
        f"Python {report.get('python')} · {report.get('platform')}",
        f"Аптайм: {report.get('uptime_sec', 0) // 60} мин",
    ]
    db = report.get("database", {})
    lines.append(f"База: {db.get('size', 0) // 1024} КБ, таблиц {db.get('tables', 0)}, "
                 f"WAL {db.get('wal_size', 0) // 1024} КБ")
    threads = report.get("threads", {})
    missing = threads.get("missing") or []
    lines.append(f"Потоков: {threads.get('total', 0)}"
                 + (f" · не запущены: {', '.join(missing)}" if missing else " · все на месте"))
    backups = report.get("backups", {})
    lines.append(f"Бэкапы: {backups.get('count', 0)} шт., последний {backups.get('last_at') or '—'}")
    errors = report.get("errors", {})
    lines.append(f"Ошибок в логе за {errors.get('window_hours', 24)} ч: {errors.get('errors', 0)}")
    outbox = report.get("outbox") or {}
    if outbox:
        parts = [f"{label} {item.get('pending', 0)}"
                 for label, item in outbox.items() if isinstance(item, dict)
                 and label != "total_pending"]
        lines.append(f"Не доставлено в Telegram: {outbox.get('total_pending', 0)}"
                     + (f" ({', '.join(parts)})" if parts else ""))
    farm = report.get("farm") or {}
    if farm:
        lines.append(f"Парк: {farm.get('online', 0)}/{farm.get('printers', 0)} онлайн, "
                     f"печатают {farm.get('printing', 0)}, в очереди {report.get('queue', 0)}")
    return "\n".join(lines)
