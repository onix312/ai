"""Надзор за процессами ассистента (18.19, И263): второй лёгкий процесс следит за первым.

Зачем отдельный процесс, если агент и панель сами пишут журналы.

Потому что упавшее не может рассказать о своём падении: если процесс умер, журнал
обрывается на последней удачной записи, и владелец узнаёт об этом утром от клиента.
Надзиратель — намеренно тупой и маленький: он ничего не умеет, кроме «пульс есть /
пульса нет», поэтому у него нет причин падать вместе с подопечным.

Как это устроено:

  * подопечный раз в минуту пишет пульс (`beat`) в `~/.printflow/watchdog.json` —
    свой pid, время и короткую заметку;
  * надзиратель раз в `interval` секунд читает пульс: pid живой и запись свежая —
    молчит; pid мёртв или запись протухла — пишет причину в `watchdog.log` и
    поднимает роль заново (`start_role`);
  * частые падения не превращаются в бесконечный цикл: после `max_restarts`
    подряд надзиратель перестаёт дёргать роль и объявляет эскалацию — это сигнал
    человеку, а не повод стучать вечно.

Никакой сети, никаких облаков: файлы в домашней папке, подъём процессов через
stdlib `subprocess`. На не-Windows надзор работает так же — pid проверяется через
`os.kill(pid, 0)`.

Использование:
  python -m agent.watchdog --beat agent      — отметить пульс (зовёт сам агент)
  python -m agent.watchdog --status          — кто жив, кто протух, сколько раз падал
  python -m agent.watchdog --once            — один проход надзора
  python -m agent.watchdog --watch           — надзор в цикле (для автозапуска)
  python -m agent.watchdog --log             — последние события
  python -m agent.watchdog --arm --roles agent,panel   — включить надзор и сохранить настройки
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime

IS_WINDOWS = sys.platform.startswith("win")

# Роли, за которыми имеет смысл следить на одном ПК владельца.
# `agent` — внешняя программа помощника, `panel` — сам PrintFlow (pf.py serve).
DEFAULT_ROLES = ("agent", "panel")

DEFAULT_INTERVAL_SEC = 30
DEFAULT_STALE_SEC = 90
DEFAULT_MAX_RESTARTS = 5
DEFAULT_COOLDOWN_SEC = 120


def home_dir() -> pathlib.Path:
    """Папка надзора. Меняется переменной, чтобы тесты не трогали домашнюю."""
    custom = os.environ.get("PRINTFLOW_WATCHDOG_DIR", "").strip()
    base = pathlib.Path(custom) if custom else (pathlib.Path.home() / ".printflow")
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return base


def paths() -> dict:
    """Три файла надзора: пульс, настройки, журнал событий."""
    base = home_dir()
    return {
        "heartbeat": str(base / "watchdog.json"),
        "config": str(base / "watchdog_config.json"),
        "log": str(base / "watchdog.log"),
    }


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def _write_json(path: str, data) -> bool:
    """Запись через временный файл: читатель никогда не видит половину пульса."""
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


def pid_alive(pid: int) -> bool:
    """Живой ли процесс. Без psutil: Windows — OpenProcess, остальные — сигнал 0."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if IS_WINDOWS:
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if handle:
                kernel32.CloseHandle(handle)
                return True
            return False
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # процесс есть, просто он чужой
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Пульс
# ---------------------------------------------------------------------------
def beat(role: str, note: str = "", pid: int | None = None) -> dict:
    """Отметить, что роль жива. Зовётся подопечным, а не надзирателем."""
    role = str(role or "").strip() or "agent"
    data = _read_json(paths()["heartbeat"], {})
    if not isinstance(data, dict):
        data = {}
    entry = {
        "pid": int(pid if pid else os.getpid()),
        "at": now_iso(),
        "ts": time.time(),
        "note": str(note or "")[:200],
    }
    data[role] = entry
    entry["ok"] = _write_json(paths()["heartbeat"], data)
    entry["role"] = role
    return entry


def beat_forever(role: str, interval_sec: int = 30) -> None:
    """Пульс в фоне: подопечный зовёт это один раз и больше о надзоре не думает.

    Поток намеренно демонический и безобидный: если он почему-то умрёт, хуже
    будет только надзору (роль сочтёт протухшей), сам процесс продолжит работу.
    """
    interval = max(5, min(3600, int(interval_sec or 30)))

    def _loop() -> None:
        while True:
            try:
                beat(role)
            except Exception:
                pass
            time.sleep(interval)

    thread = threading.Thread(target=_loop, name=f"watchdog-beat-{role}", daemon=True)
    thread.start()
    return None


def heartbeats() -> dict:
    data = _read_json(paths()["heartbeat"], {})
    return data if isinstance(data, dict) else {}


def status(stale_sec: int | None = None, roles=None) -> dict:
    """Кто жив прямо сейчас и чей пульс протух.

    «Протух» — это два разных случая, и они не сливаются: pid мёртв (процесс
    упал) или pid жив, а пульса нет (процесс завис). Причина в ответе разная,
    потому что лечится это по-разному.
    """
    config = load_config()
    stale_sec = int(stale_sec or config["stale_sec"] or DEFAULT_STALE_SEC)
    wanted = tuple(roles or config["roles"] or DEFAULT_ROLES)
    data = heartbeats()
    rows = []
    for role in wanted:
        entry = data.get(role) or {}
        pid = int(entry.get("pid") or 0)
        ts = float(entry.get("ts") or 0)
        age = int(time.time() - ts) if ts else -1
        alive = pid_alive(pid) if pid else False
        stale = (not pid) or (not alive) or age < 0 or age > stale_sec
        if not pid:
            reason = "Пульса не было ни разу"
        elif not alive:
            reason = f"Процесс {pid} не отвечает"
        elif age < 0:
            reason = "В пульсе нет времени"
        elif age > stale_sec:
            reason = f"Пульс старше {stale_sec} с ({age} с)"
        else:
            reason = ""
        counters = config.get("restarts") or {}
        row = {
            "role": role, "pid": pid, "alive": alive, "age_sec": age,
            "stale": bool(stale), "reason": reason,
            "restarts": int(counters.get(role, 0) or 0),
            "last_beat": str(entry.get("at") or ""),
            "note": str(entry.get("note") or ""),
        }
        rows.append(row)
    stale_rows = [r for r in rows if r["stale"]]
    return {
        "ok": not stale_rows,
        "checked_at": now_iso(),
        "stale_sec": stale_sec,
        "roles": rows,
        "stale": [r["role"] for r in stale_rows],
        "heartbeat_file": paths()["heartbeat"],
        "reason": "" if not stale_rows else "; ".join(
            f"{r['role']}: {r['reason']}" for r in stale_rows),
        "hint": "Надзор видит пульс каждой роли; протухший пульс — повод поднять роль заново",
    }


# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------
def default_config() -> dict:
    return {
        "roles": list(DEFAULT_ROLES),
        "interval_sec": DEFAULT_INTERVAL_SEC,
        "stale_sec": DEFAULT_STALE_SEC,
        "max_restarts": DEFAULT_MAX_RESTARTS,
        "cooldown_sec": DEFAULT_COOLDOWN_SEC,
        "enabled": False,
        "commands": {},
        "restarts": {},
        "last_restart": {},
        "escalated": [],
        "updated_at": "",
    }


def load_config() -> dict:
    """Настройки надзора с значениями по умолчанию для всего, чего нет в файле."""
    base = default_config()
    data = _read_json(paths()["config"], {})
    if isinstance(data, dict):
        for key, value in data.items():
            if key in ("roles", "commands", "restarts", "last_restart", "escalated"):
                if isinstance(value, (list, dict)):
                    base[key] = value
            elif key in ("interval_sec", "stale_sec", "max_restarts", "cooldown_sec"):
                try:
                    base[key] = int(value)
                except (TypeError, ValueError):
                    pass
            elif key == "enabled":
                base[key] = bool(value)
            elif key == "updated_at":
                base[key] = str(value)
    base["roles"] = [str(r) for r in (base["roles"] or DEFAULT_ROLES)]
    return base


def save_config(roles=None, interval_sec: int | None = None, stale_sec: int | None = None,
                max_restarts: int | None = None, enabled: bool | None = None) -> dict:
    """Сохранить настройки: что сторожить, как часто, сколько раз поднимать."""
    config = load_config()
    if roles:
        cleaned = [str(r).strip() for r in (roles if isinstance(roles, (list, tuple)) else str(roles).split(","))]
        config["roles"] = [r for r in cleaned if r] or list(DEFAULT_ROLES)
    if interval_sec:
        config["interval_sec"] = max(5, min(3600, int(interval_sec)))
    if stale_sec:
        config["stale_sec"] = max(15, min(7200, int(stale_sec)))
    if max_restarts:
        config["max_restarts"] = max(1, min(100, int(max_restarts)))
    if enabled is not None:
        config["enabled"] = bool(enabled)
        if not config["enabled"]:
            config["restarts"] = {}
            config["escalated"] = []
    config["updated_at"] = now_iso()
    ok = _write_json(paths()["config"], config)
    config["ok"] = ok
    return config


# ---------------------------------------------------------------------------
# Журнал событий надзора
# ---------------------------------------------------------------------------
LOG_LIMIT = 2000


def log_event(role: str, kind: str, detail: str = "") -> dict:
    """Строка в watchdog.log. Формат простой: время, роль, что случилось, подробность."""
    path = paths()["log"]
    at = now_iso()
    line = f"{at}\t{str(role)[:40]}\t{str(kind)[:40]}\t{str(detail)[:400]}\n"
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line)
    except OSError:
        return {"ok": False, "at": at, "role": role, "kind": kind, "detail": detail,
                "reason": f"Журнал недоступен: {path}"}
    return {"ok": True, "at": at, "role": role, "kind": kind, "detail": str(detail)[:400]}


def tail_log(limit: int = 40) -> list[dict]:
    """Последние события надзора, свежие сверху."""
    try:
        limit = max(1, min(LOG_LIMIT, int(limit or 40)))
    except (TypeError, ValueError):
        limit = 40
    try:
        with open(paths()["log"], "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()[-limit:]
    except OSError:
        return []
    rows = []
    for line in lines:
        parts = line.rstrip("\n").split("\t")
        if len(parts) < 3:
            continue
        rows.append({
            "at": parts[0], "role": parts[1], "kind": parts[2],
            "detail": parts[3] if len(parts) > 3 else "",
        })
    rows.reverse()
    return rows


def clear_log() -> bool:
    try:
        open(paths()["log"], "w", encoding="utf-8").close()
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Подъём и остановка ролей
# ---------------------------------------------------------------------------
def role_command(role: str, config: dict | None = None) -> list[str]:
    """Чем поднимать роль. Своё можно задать в настройках, по умолчанию — репозиторий."""
    config = config or load_config()
    custom = (config.get("commands") or {}).get(role)
    if isinstance(custom, (list, tuple)) and custom:
        return [str(part) for part in custom]
    if isinstance(custom, str) and custom.strip():
        return custom.split()
    root = pathlib.Path(__file__).resolve().parents[1]
    if role == "agent":
        return [sys.executable, "-m", "agent"]
    if role == "panel":
        return [sys.executable, str(root / "pf.py"), "serve"]
    if role == "watchdog":
        return [sys.executable, "-m", "agent.watchdog", "--watch"]
    return [sys.executable, "-m", "agent"]


def _spawn(command: list[str], role: str) -> subprocess.Popen:
    out_path = home_dir() / f"watchdog-{role}.out"
    flags = 0
    if IS_WINDOWS:
        # Свой процесс и без консольного окна: надзиратель поднимает роль, а не
        # показывает пользователю второй чёрный прямоугольник.
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    with open(out_path, "a", encoding="utf-8") as out:
        return subprocess.Popen(command, cwd=str(pathlib.Path(__file__).resolve().parents[1]),
                                stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                creationflags=flags)


def start_role(role: str, command: list[str] | None = None) -> dict:
    """Поднять роль и сразу отметить ей пульс — чтобы надзиратель не считал её мёртвой."""
    config = load_config()
    cmd = command or role_command(role, config)
    if not cmd:
        return {"ok": False, "role": role, "reason": "Не знаю, чем поднимать роль"}
    try:
        process = _spawn(cmd, role)
    except OSError as exc:
        log_event(role, "start_failed", str(exc))
        return {"ok": False, "role": role, "command": cmd, "reason": f"Не поднялась: {exc}"}
    # Пульс пишет сам процесс; здесь только стартовая отметка с реальным pid,
    # иначе между стартом и первым пульсом надзор успеет поднять дубль.
    beat(role, note="поднят надзирателем", pid=process.pid)
    log_event(role, "started", f"pid={process.pid} cmd={' '.join(cmd)}")
    return {"ok": True, "role": role, "pid": process.pid, "command": cmd, "reason": ""}


def stop_role(role: str, timeout: float = 5.0) -> dict:
    """Попросить роль остановиться, потом настоять. Мягко там, где это возможно."""
    entry = heartbeats().get(role) or {}
    pid = int(entry.get("pid") or 0)
    if not pid or not pid_alive(pid):
        return {"ok": False, "role": role, "pid": pid, "reason": "Процесс уже не отвечает"}
    try:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=10)
        else:
            os.kill(pid, signal.SIGTERM)
            deadline = time.time() + timeout
            while time.time() < deadline and pid_alive(pid):
                time.sleep(0.2)
            if pid_alive(pid):
                os.kill(pid, signal.SIGKILL)
    except OSError as exc:
        return {"ok": False, "role": role, "pid": pid, "reason": f"Не остановился: {exc}"}
    log_event(role, "stopped", f"pid={pid}")
    return {"ok": True, "role": role, "pid": pid, "reason": ""}


def restart_role(role: str, reason: str = "") -> dict:
    """Поднять заново: сначала убрать остаток (если висит), потом старт."""
    stop = stop_role(role)
    start = start_role(role)
    if start.get("ok"):
        _bump_restart(role, reason)
    return {"ok": bool(start.get("ok")), "role": role, "reason": reason,
            "stopped": stop, "started": start}


def _bump_restart(role: str, reason: str = "") -> dict:
    config = load_config()
    restarts = dict(config.get("restarts") or {})
    last = dict(config.get("last_restart") or {})
    restarts[role] = int(restarts.get(role, 0) or 0) + 1
    last[role] = now_iso()
    config["restarts"] = restarts
    config["last_restart"] = last
    config["updated_at"] = now_iso()
    _write_json(paths()["config"], config)
    return {"role": role, "restarts": restarts[role], "reason": reason}


def escalate(role: str, restarts: int, max_restarts: int) -> dict:
    """Перестать дёргать роль и сказать об этом человеку.

    Молчаливый бесконечный цикл «упало — поднял» хуже падения: он жжёт время и
    прячет настоящую причину. Поэтому после порога надзиратель сдаётся вслух.
    """
    config = load_config()
    escalated = list(config.get("escalated") or [])
    if role not in escalated:
        escalated.append(role)
    config["escalated"] = escalated
    config["updated_at"] = now_iso()
    _write_json(paths()["config"], config)
    log_event(role, "escalated", f"падений подряд {restarts}, порог {max_restarts} — нужен человек")
    return {"ok": True, "role": role, "restarts": restarts, "max_restarts": max_restarts,
            "reason": f"{role}: {restarts} падений подряд, надзор сдался — нужен человек, "
                      f"проверьте причину (журнал {pathlib.Path(paths()['log']).name})"}


def cooldown_ok(role: str, config: dict | None = None) -> tuple[bool, str]:
    """Не дёргать роль чаще, чем раз в cooldown: падение и подъём — не спринт."""
    config = config or load_config()
    last = (config.get("last_restart") or {}).get(role)
    if not last:
        return True, ""
    try:
        stamp = datetime.strptime(str(last), "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return True, ""
    waited = (datetime.now() - stamp).total_seconds()
    if waited < int(config.get("cooldown_sec") or DEFAULT_COOLDOWN_SEC):
        return False, f"Отдых после подъёма: {int(waited)} с из {config['cooldown_sec']} с"
    return True, ""


# ---------------------------------------------------------------------------
# Сам надзор
# ---------------------------------------------------------------------------
def pass_once(roles=None, restart: bool = True) -> dict:
    """Один проход: проверить пульс, записать причину, поднять упавших.

    Флаг `restart=False` нужен панели и проверкам: посмотреть правду, ничего не
    трогая, — это отдельная операция, а не «надзор вполсилы».
    """
    config = load_config()
    state = status(roles=roles)
    actions = []
    for row in state["roles"]:
        if not row["stale"]:
            continue
        log_event(row["role"], "stale", row["reason"])
        if not restart:
            actions.append({"role": row["role"], "action": "seen", "reason": row["reason"]})
            continue
        restarts = int(row["restarts"] or 0)
        max_restarts = int(config.get("max_restarts") or DEFAULT_MAX_RESTARTS)
        if restarts >= max_restarts:
            actions.append({"role": row["role"], "action": "escalated", **escalate(row["role"], restarts, max_restarts)})
            continue
        allowed, why = cooldown_ok(row["role"], config)
        if not allowed:
            actions.append({"role": row["role"], "action": "waiting", "reason": why})
            continue
        result = restart_role(row["role"], row["reason"])
        actions.append({"role": row["role"], "action": "restarted" if result["ok"] else "restart_failed",
                        "reason": row["reason"], "pid": result["started"].get("pid"),
                        "detail": "" if result["ok"] else result["started"].get("reason", "")})
    return {"ok": True, "checked_at": state["checked_at"], "stale": state["stale"],
            "actions": actions, "roles": state["roles"], "reason": state["reason"]}


def watch(roles=None, interval_sec: int | None = None, max_passes: int | None = None) -> dict:
    """Надзор в цикле. Ctrl+C или max_passes останавливают его без следов."""
    config = load_config()
    interval = int(interval_sec or config.get("interval_sec") or DEFAULT_INTERVAL_SEC)
    interval = max(5, min(3600, interval))
    log_event("watchdog", "watching", f"интервал {interval} с, роли {','.join(roles or config['roles'])}")
    passes = 0
    last = None
    try:
        while True:
            last = pass_once(roles)
            passes += 1
            if max_passes and passes >= max_passes:
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        log_event("watchdog", "stopped", f"остановлен вручную после {passes} проходов")
    return {"ok": True, "passes": passes, "interval_sec": interval, "last": last,
            "log": paths()["log"]}


def self_check() -> dict:
    """Проверка самого надзирателя: жив ли он и можно ли ему верить.

    Восемь вопросов, на которые надзиратель обязан ответить «да» до того, как
    владелец доверит ему подъём процессов.
    """
    checks = []
    files = paths()

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    add("папка надзора", home_dir().is_dir(), str(home_dir()))
    add("файлы пульса/настроек/журнала", True, ", ".join(pathlib.Path(p).name for p in files.values()))
    probe = _write_json(files["config"] + ".probe", {"probe": 1})
    add("запись в папку", probe)
    try:
        os.unlink(files["config"] + ".probe")
    except OSError:
        pass
    add("проверка pid", pid_alive(os.getpid()), f"свой pid {os.getpid()} отвечает")
    add("чужой pid отвергается", not pid_alive(999999), "999999 считается мёртвым")
    commands = {role: role_command(role) for role in DEFAULT_ROLES}
    add("команды ролей", all(commands.values()), "; ".join(f"{r}: {' '.join(c)}" for r, c in commands.items()))
    config = load_config()
    add("настройки читаются", bool(config.get("roles")), f"роли {','.join(config['roles'])}")
    state = status()
    add("надзор не следит за собой", "watchdog" not in (config.get("roles") or []),
        "роль watchdog в списке отсутствует")
    ok = all(c["ok"] for c in checks)
    return {"ok": ok, "checks": checks, "status": state, "config": config,
            "reason": "" if ok else "; ".join(c["check"] for c in checks if not c["ok"]),
            "hint": "Надзор без пульса бесполезен: проверка отвечает, может ли он вообще видеть и поднимать"}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Надзор за процессами ассистента (И263)")
    parser.add_argument("--beat", metavar="ROLE", help="Отметить пульс роли (зовёт подопечный)")
    parser.add_argument("--note", default="", help="Заметка к пульсу")
    parser.add_argument("--status", action="store_true", help="Кто жив и у кого пульс протух")
    parser.add_argument("--once", action="store_true", help="Один проход надзора с подъёмом упавших")
    parser.add_argument("--dry", action="store_true", help="Посмотреть правду, ничего не поднимая")
    parser.add_argument("--watch", action="store_true", help="Надзор в цикле")
    parser.add_argument("--passes", type=int, default=0, help="Сколько проходов сделать в цикле (0 — бесконечно)")
    parser.add_argument("--log", action="store_true", help="Последние события надзора")
    parser.add_argument("--clear-log", action="store_true", help="Очистить журнал надзора")
    parser.add_argument("--self-check", action="store_true", help="Проверка самого надзирателя")
    parser.add_argument("--start", metavar="ROLE", help="Поднять роль вручную")
    parser.add_argument("--stop", metavar="ROLE", help="Остановить роль")
    parser.add_argument("--restart", metavar="ROLE", help="Перезапустить роль")
    parser.add_argument("--arm", action="store_true", help="Включить надзор и сохранить настройки")
    parser.add_argument("--disarm", action="store_true", help="Выключить надзор")
    parser.add_argument("--roles", default="", help="Роли через запятую: agent,panel")
    parser.add_argument("--interval", type=int, default=0, help="Период проверки, секунд")
    parser.add_argument("--stale", type=int, default=0, help="Через сколько секунд пульс считается протухшим")
    parser.add_argument("--max-restarts", type=int, default=0, help="Сколько подъёмов подряд до эскалации")
    args = parser.parse_args(argv)

    roles = [r.strip() for r in args.roles.split(",") if r.strip()] or None

    if args.beat:
        print(json.dumps(beat(args.beat, args.note), ensure_ascii=False, indent=2))
        return 0
    if args.start:
        print(json.dumps(start_role(args.start), ensure_ascii=False, indent=2))
        return 0
    if args.stop:
        print(json.dumps(stop_role(args.stop), ensure_ascii=False, indent=2))
        return 0
    if args.restart:
        print(json.dumps(restart_role(args.restart, "вручную"), ensure_ascii=False, indent=2))
        return 0
    if args.arm or args.disarm:
        enabled = bool(args.arm)
        result = save_config(roles=roles, interval_sec=args.interval or None,
                             stale_sec=args.stale or None,
                             max_restarts=args.max_restarts or None, enabled=enabled)
        log_event("watchdog", "armed" if enabled else "disarmed", ",".join(result.get("roles") or []))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1
    if args.clear_log:
        print(json.dumps({"ok": clear_log()}, ensure_ascii=False))
        return 0
    if args.log:
        print(json.dumps(tail_log(40), ensure_ascii=False, indent=2))
        return 0
    if args.self_check:
        print(json.dumps(self_check(), ensure_ascii=False, indent=2))
        return 0
    if args.once or args.dry:
        print(json.dumps(pass_once(roles, restart=not args.dry), ensure_ascii=False, indent=2))
        return 0
    if args.watch:
        print(json.dumps(watch(roles, args.interval or None, args.passes or None),
                         ensure_ascii=False, indent=2))
        return 0

    # По умолчанию — правда без действий: владелец смотрит, потом решает.
    print(json.dumps(status(roles=roles), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
