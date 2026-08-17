"""Проверка новых версий PrintFlow на GitHub.

Спрашиваем api.github.com раз в 6 часов (или по кнопке). Неудача не страшна:
просто возвращаем None и пробуем позже. Версия из тега релиза сравнивается
с APP_VERSION.
"""
from __future__ import annotations

import json
import time
import urllib.request

REPO = "onix312/ai"
CACHE_SECONDS = 6 * 3600


class UpdateChecker:
    def __init__(self, current: str):
        self.current = current
        self._latest: dict | None = None
        self._checked = 0.0
        self._error = ""

    def check(self, force: bool = False) -> dict | None:
        now = time.time()
        if not force and self._latest is not None and now - self._checked < CACHE_SECONDS:
            return self._latest
        if not force and now - self._checked < 60:
            return self._latest  # не долбим GitHub чаще раза в минуту
        self._checked = now
        try:
            req = urllib.request.Request(
                f"https://api.github.com/repos/{REPO}/releases/latest",
                headers={"User-Agent": "PrintFlow", "Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=8) as response:
                data = json.loads(response.read().decode("utf-8", "ignore"))
            tag = str(data.get("tag_name") or "").lstrip("vV")
            self._latest = {
                "version": tag,
                "name": str(data.get("name") or ""),
                "url": str(data.get("html_url") or ""),
                "published": str(data.get("published_at") or "")[:10],
            }
            self._error = ""
        except Exception as exc:
            self._latest = None
            self._error = str(exc)
        return self._latest

    def report(self, force: bool = False) -> dict:
        latest = self.check(force=force)
        if not latest:
            return {"current": self.current, "latest": None,
                    "update": False, "error": self._error}
        current = [int(x) for x in self.current.split(".") if x.isdigit()] or [0]
        new = [int(x) for x in latest["version"].split(".") if x.isdigit()] or [0]
        return {
            "current": self.current,
            "latest": latest,
            "update": new > current,
            "error": "",
        }
