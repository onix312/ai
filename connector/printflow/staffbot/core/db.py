"""Инициализация таблиц бота — чистый слой, без бизнес-логики."""
from __future__ import annotations


def ensure_scenes_table(db) -> None:
    """Создать таблицу bot_scenes если её нет (SQLite с нуля)."""
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS bot_scenes (
            chat_id TEXT PRIMARY KEY,
            scene TEXT NOT NULL,
            data TEXT NOT NULL DEFAULT '{}',
            expires_at TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT ''
        )
        """
    )
    # индекс для sweep
    try:
        db.execute("CREATE INDEX IF NOT EXISTS idx_bot_scenes_expires ON bot_scenes(expires_at)")
    except Exception:
        pass
