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
    out = dict(app_shell.status())
    raw = str(ctx.arg("installed", "") or "").strip()
    if raw:
        out.update(app_shell.check_version(raw))
    return out
