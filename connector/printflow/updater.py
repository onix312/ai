"""Проверка и установка обновлений PrintFlow.

Приложение живёт в папке с исходниками, поэтому обновление — это не установщик,
а обновление самой папки. Поддерживаем два режима:

* **git** — рабочая копия склонирована (есть `.git` и сам `git`). Обновляемся
  через `git fetch` + `git merge --ff-only` по текущей ветке.
* **архив** — папку просто распаковали из ZIP. Тогда скачиваем свежий архив
  ветки с GitHub и аккуратно раскладываем файлы поверх, не трогая каталог
  данных пользователя.

Перед установкой всегда делается резервная копия базы, а обновляться во время
печати нельзя: сначала дождёмся, пока принтеры освободятся. После установки
коннектор перезапускает сам себя тем же способом, каким был запущен.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from .config import DATA_DIR, ROOT, now_iso

REPO = "onix312/ai"
DEFAULT_BRANCH = "main"
CACHE_SECONDS = 6 * 3600
BACKUP_DIR = DATA_DIR / "backups"
BACKUP_KEEP = 10
GIT_TIMEOUT = 120

# Что никогда не перезаписываем архивом: пользовательское и служебное.
ARCHIVE_SKIP = {".git", ".github", "node_modules", "__pycache__", ".venv", "venv"}


def _run(args: list[str], cwd: Path | None = None, timeout: int = GIT_TIMEOUT) -> tuple[int, str]:
    """Запустить команду и вернуть (код возврата, вывод). Без исключений."""
    try:
        proc = subprocess.run(
            args, cwd=str(cwd or ROOT), timeout=timeout,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            # Без окна консоли на Windows — иначе при каждой проверке мигает окно.
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
        return proc.returncode, proc.stdout.decode("utf-8", "replace").strip()
    except FileNotFoundError:
        return 127, "команда не найдена"
    except subprocess.TimeoutExpired:
        return 124, "превышено время ожидания"
    except Exception as exc:  # pragma: no cover — защита от экзотики ОС
        return 1, str(exc)


class UpdateChecker:
    """Проверяет наличие обновлений и умеет их устанавливать."""

    def __init__(self, current: str, db=None, manager=None):
        self.current = current
        self.db = db
        self.manager = manager
        self._latest: dict | None = None
        self._checked = 0.0
        self._error = ""
        self._busy = False
        self._log: list[dict] = []
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------ окружение
    @property
    def mode(self) -> str:
        """Как установлено приложение: `git` или `archive`."""
        if (ROOT / ".git").exists() and _run(["git", "--version"])[0] == 0:
            return "git"
        return "archive"

    def branch(self) -> str:
        """Ветка, за которой следим: текущая в git, иначе из настроек."""
        if self.mode == "git":
            code, out = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
            if code == 0 and out and out != "HEAD":
                return out
        return str(self._setting("update_branch", DEFAULT_BRANCH) or DEFAULT_BRANCH)

    def _setting(self, key: str, default):
        if not self.db:
            return default
        try:
            return self.db.setting(key, default)
        except Exception:
            return default

    def local_sha(self) -> str:
        if self.mode == "git":
            code, out = _run(["git", "rev-parse", "HEAD"])
            if code == 0:
                return out[:40]
        return str(self._setting("installed_sha", "") or "")

    # -------------------------------------------------------------- проверка
    def check(self, force: bool = False) -> dict | None:
        """Узнать, что лежит на GitHub в нашей ветке. None — не получилось."""
        now = time.time()
        if not force and self._latest is not None and now - self._checked < CACHE_SECONDS:
            return self._latest
        if not force and now - self._checked < 60:
            return self._latest  # не долбим GitHub чаще раза в минуту
        self._checked = now
        branch = self.branch()
        try:
            self._latest = self._remote_head(branch)
            self._error = ""
        except Exception as exc:
            self._latest = None
            self._error = str(exc)
        return self._latest

    def _remote_head(self, branch: str) -> dict:
        """Последний коммит ветки на GitHub."""
        req = urllib.request.Request(
            f"https://api.github.com/repos/{REPO}/commits/{branch}",
            headers={"User-Agent": "PrintFlow", "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8", "ignore"))
        commit = data.get("commit") or {}
        message = str(commit.get("message") or "").strip()
        return {
            "sha": str(data.get("sha") or "")[:40],
            "short": str(data.get("sha") or "")[:7],
            "title": message.splitlines()[0] if message else "",
            # Полное тело коммита бывает огромным — интерфейсу хватает выжимки.
            "message": message[:600],
            "author": ((commit.get("author") or {}).get("name") or ""),
            "date": str((commit.get("author") or {}).get("date") or "")[:10],
            "url": str(data.get("html_url") or ""),
            "branch": branch,
        }

    def _pending(self, branch: str, limit: int = 20) -> list[dict]:
        """Список коммитов, которых у нас ещё нет (для списка изменений)."""
        local = self.local_sha()
        if not local:
            return []
        try:
            req = urllib.request.Request(
                f"https://api.github.com/repos/{REPO}/compare/{local}...{branch}",
                headers={"User-Agent": "PrintFlow", "Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=10) as response:
                data = json.loads(response.read().decode("utf-8", "ignore"))
        except Exception:
            return []
        out = []
        for row in (data.get("commits") or [])[-limit:]:
            message = str((row.get("commit") or {}).get("message") or "").strip()
            out.append({
                "short": str(row.get("sha") or "")[:7],
                "title": message.splitlines()[0] if message else "",
                "date": str(((row.get("commit") or {}).get("author") or {}).get("date") or "")[:10],
            })
        out.reverse()
        return out

    # -------------------------------------------------------------- готовность
    def busy_reason(self) -> str:
        """Почему сейчас обновляться нельзя. Пустая строка — можно."""
        if self._busy:
            return "Обновление уже выполняется"
        manager = self.manager
        if manager:
            try:
                for printer in manager.printers.values():
                    snap = printer.snapshot()
                    if snap["printer"]["state"] in ("RUNNING", "PREPARE"):
                        return f"Идёт печать на «{snap['printer'].get('name') or 'принтере'}»"
            except Exception:
                pass
        if self.mode == "git":
            code, out = _run(["git", "status", "--porcelain"])
            if code == 0 and out:
                count = len([ln for ln in out.splitlines() if ln.strip()])
                return f"В папке есть несохранённые изменения ({count} файл(ов))"
        return ""

    def report(self, force: bool = False) -> dict:
        """Полная сводка для интерфейса."""
        enabled = bool(self._setting("update_check_enabled", True))
        base = {
            "current": self.current,
            "mode": self.mode,
            "branch": self.branch(),
            "local": self.local_sha()[:7],
            "auto": bool(self._setting("auto_update_enabled", False)),
            "last_update_at": self._setting("last_update_at", ""),
            "history": self.history(),
        }
        if not enabled:
            return {**base, "latest": None, "update": False, "error": "",
                    "disabled": True, "can_apply": False, "busy_reason": ""}
        latest = self.check(force=force)
        if not latest:
            return {**base, "latest": None, "update": False,
                    "error": self._error, "can_apply": False, "busy_reason": ""}
        has_update = bool(latest["sha"]) and latest["sha"] != self.local_sha()
        busy = self.busy_reason() if has_update else ""
        return {
            **base,
            "latest": latest,
            "update": has_update,
            "commits": self._pending(latest["branch"]) if has_update else [],
            "error": "",
            "can_apply": has_update and not busy,
            "busy_reason": busy,
        }

    # -------------------------------------------------------------- установка
    def apply(self, force: bool = False) -> dict:
        """Установить обновление. Возвращает результат и нужен ли перезапуск."""
        with self._lock:
            if self._busy:
                raise ValueError("Обновление уже выполняется")
            reason = self.busy_reason()
            if reason and not force:
                raise ValueError(reason)
            self._busy = True
        started = time.time()
        before = self.local_sha()
        try:
            backup = self._backup_db()
            if self.mode == "git":
                applied = self._apply_git()
            else:
                applied = self._apply_archive()
            after = self.local_sha()
            deps = self._sync_deps(before, after)
            self._remember(after)
            result = {
                "ok": True, "before": before[:7], "after": after[:7],
                "changed": bool(applied.get("changed")), "files": applied.get("files", 0),
                "deps": deps, "backup": str(backup) if backup else "",
                "seconds": round(time.time() - started, 1),
                "restart_required": bool(applied.get("changed")),
                "detail": applied.get("detail", ""),
            }
            self._note(result)
            return result
        finally:
            self._busy = False
            self._checked = 0.0  # следующая проверка — заново

    def _apply_git(self) -> dict:
        branch = self.branch()
        code, out = _run(["git", "fetch", "--prune", "origin", branch])
        if code != 0:
            raise ValueError(f"Не удалось получить обновление: {out[:300]}")
        before = self.local_sha()
        code, out = _run(["git", "merge", "--ff-only", f"origin/{branch}"])
        if code != 0:
            raise ValueError(
                "Не удалось применить обновление без потери правок: "
                f"{out[:300]}. Сохраните свои изменения и повторите.")
        after = self.local_sha()
        files = 0
        if before and after and before != after:
            code, diff = _run(["git", "diff", "--name-only", f"{before}..{after}"])
            files = len([ln for ln in diff.splitlines() if ln.strip()]) if code == 0 else 0
        return {"changed": before != after, "files": files, "detail": out[:300]}

    def _apply_archive(self) -> dict:
        """Обновление распакованной папки: качаем архив ветки и копируем поверх."""
        branch = self.branch()
        latest = self.check(force=True)
        if not latest:
            raise ValueError(self._error or "Не удалось узнать версию на GitHub")
        url = f"https://codeload.github.com/{REPO}/tar.gz/refs/heads/{branch}"
        req = urllib.request.Request(url, headers={"User-Agent": "PrintFlow"})
        with tempfile.TemporaryDirectory(prefix="pf-update-") as tmp:
            tmp_path = Path(tmp)
            archive = tmp_path / "update.tar.gz"
            with urllib.request.urlopen(req, timeout=120) as response:
                archive.write_bytes(response.read())
            with tarfile.open(archive, "r:gz") as tar:
                members = [m for m in tar.getmembers() if not m.name.startswith("/")
                           and ".." not in Path(m.name).parts]
                tar.extractall(tmp_path, members=members)
            roots = [p for p in tmp_path.iterdir() if p.is_dir()]
            if not roots:
                raise ValueError("Архив обновления пуст")
            files = self._copy_tree(roots[0], ROOT)
        self._set_setting("installed_sha", latest["sha"])
        return {"changed": files > 0, "files": files, "detail": f"обновлено файлов: {files}"}

    def _copy_tree(self, src: Path, dst: Path) -> int:
        """Скопировать дерево поверх, пропуская служебные каталоги."""
        count = 0
        for item in src.rglob("*"):
            rel = item.relative_to(src)
            if set(rel.parts) & ARCHIVE_SKIP:
                continue
            target = dst / rel
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() and target.read_bytes() == item.read_bytes():
                continue
            shutil.copy2(item, target)
            count += 1
        return count

    def _sync_deps(self, before: str, after: str) -> bool:
        """Доставить зависимости, если requirements.txt изменился."""
        requirements = ROOT / "connector" / "requirements.txt"
        if not requirements.is_file():
            return False
        changed = True
        if self.mode == "git" and before and after and before != after:
            code, out = _run(["git", "diff", "--name-only", f"{before}..{after}",
                              "--", "connector/requirements.txt"])
            changed = bool(out.strip()) if code == 0 else True
        if not changed:
            return False
        code, _ = _run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
                        "-q", "-r", str(requirements)], timeout=600)
        return code == 0

    def _backup_db(self) -> Path | None:
        """Копия базы перед обновлением. Держим последние BACKUP_KEEP штук."""
        from .config import DB_FILE
        if not Path(DB_FILE).is_file():
            return None
        try:
            BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            target = BACKUP_DIR / f"printflow-{stamp}.sqlite3"
            if self.db is not None:
                # Через SQLite API — копия консистентна даже под нагрузкой.
                self.db.backup_to(target)
            else:
                shutil.copy2(DB_FILE, target)
            old = sorted(BACKUP_DIR.glob("printflow-*.sqlite3"))[:-BACKUP_KEEP]
            for path in old:
                path.unlink(missing_ok=True)
            return target
        except Exception:
            return None

    def _set_setting(self, key: str, value) -> None:
        if not self.db:
            return
        try:
            self.db.set_settings({key: value})
        except Exception:
            pass

    def _remember(self, sha: str) -> None:
        self._set_setting("installed_sha", sha)
        self._set_setting("last_update_at", now_iso())

    def _note(self, result: dict) -> None:
        """Записать результат в журнал обновлений и ленту событий."""
        entry = {"at": now_iso(), **result}
        self._log.insert(0, entry)
        del self._log[20:]
        if self.db and result.get("changed"):
            try:
                self.db.add_event(
                    "update", "Установлено обновление",
                    f"{result['before']} → {result['after']} · файлов: {result['files']}",
                    data={"before": result["before"], "after": result["after"]})
            except Exception:
                pass

    def history(self) -> list[dict]:
        return list(self._log)

    # ------------------------------------------------------------- перезапуск
    def restart(self, delay: float = 1.0) -> None:
        """Перезапустить коннектор тем же способом, каким он был запущен."""
        def worker():
            time.sleep(max(0.2, delay))
            try:
                if self.manager:
                    self.manager.shutdown()
            except Exception:
                pass
            try:
                if self.db:
                    self.db.close()
            except Exception:
                pass
            try:
                os.execv(sys.executable, [sys.executable, *sys.argv])
            except Exception:
                # Если подменить процесс не вышло — выходим с кодом 3,
                # скрипты запуска перезапустят нас сами.
                os._exit(3)
        threading.Thread(target=worker, name="pf-restart", daemon=True).start()

    # --------------------------------------------------------------- автомат
    def start_auto(self) -> None:
        """Фоновый цикл: проверка обновлений и, если разрешено, установка."""
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._auto_loop, name="pf-updater", daemon=True)
        self._thread.start()

    def shutdown(self) -> None:
        self._stop.set()

    def _auto_loop(self) -> None:
        # Первая проверка — через минуту после старта, чтобы не мешать запуску.
        if self._stop.wait(60):
            return
        while not self._stop.is_set():
            try:
                self._auto_tick()
            except Exception:
                pass
            interval = max(600.0, float(self._setting("update_check_hours", 6) or 6) * 3600)
            if self._stop.wait(interval):
                return

    def _auto_tick(self) -> None:
        if not self._setting("update_check_enabled", True):
            return
        report = self.report()
        if not report.get("update"):
            return
        latest = report.get("latest") or {}
        if not self._setting("auto_update_enabled", False):
            # Тихо подсказываем: обновление есть, ставить будет человек.
            self._announce(latest)
            return
        if not report.get("can_apply"):
            return  # печатаем или в папке правки — попробуем в следующий раз
        result = self.apply()
        if result.get("restart_required"):
            self._notify(f"⬆ PrintFlow обновлён до {result['after']}. Перезапускаюсь.")
            self.restart(delay=2.0)

    def _announce(self, latest: dict) -> None:
        """Один раз на версию сообщить, что вышло обновление."""
        sha = latest.get("sha") or ""
        if not sha or self._setting("update_seen_sha", "") == sha:
            return
        self._set_setting("update_seen_sha", sha)
        if self.db:
            try:
                self.db.add_event("update", "Доступно обновление",
                                  f"{latest.get('short')} · {latest.get('title')}",
                                  data={"sha": sha, "url": latest.get("url")})
            except Exception:
                pass
        self._notify(f"⬆ Доступно обновление PrintFlow: {latest.get('title') or latest.get('short')}")

    def _notify(self, text: str) -> None:
        manager = self.manager
        if manager and getattr(manager, "notify_async", None):
            try:
                manager.notify_async(text)
            except Exception:
                pass
