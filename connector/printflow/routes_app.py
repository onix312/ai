"""Маршруты оболочки-приложения: чем телефон кассира отличается от вкладки.

Один GET: есть ли собранный APK, какая у него версия и куда идти за скачиванием.
Сам файл раздаёт обычный статический слой (`site/app/`), поэтому отдельного
бинарного транспорта нет, а имя файла в URL остаётся человекочитаемым.
"""
from __future__ import annotations

from typing import Any

from . import app_shell
from .router import Ctx, router


@router.get("/api/app/android", doc="APK-оболочка кассы: наличие сборки и версия")
def app_android(api: Any, ctx: Ctx):
    """Статус сборки; с `?installed=<versionCode>` — ещё и «нужно ли обновиться».

    Оболочка спрашивает это при старте: свой versionCode она знает, и если на
    сервере лежит более свежая сборка, телефон предлагает обновиться в один
    тап — иначе «переустановить APK на каждый телефон» было бы ручным
    обходом магазинов.
    """
    raw_target = ctx.body.get("app", "") if isinstance(ctx.body, dict) else ""
    if not raw_target:
        raw_target = ctx.query.get("app", "kassa")
    if isinstance(raw_target, (list, tuple)):
        raw_target = raw_target[0] if raw_target else "kassa"
    target = str(raw_target or "kassa").strip().lower()
    if target not in {"kassa", "pult"}:
        target = "kassa"
    out = dict(app_shell.status(target=target))
    raw = ctx.body.get("installed", "") if isinstance(ctx.body, dict) else ""
    if not raw:
        raw = ctx.query.get("installed", "")
    if isinstance(raw, (list, tuple)):
        raw = raw[0] if raw else ""
    raw = str(raw or "").strip()
    if raw:
        out.update(app_shell.check_version(raw, target=target))
    return out
