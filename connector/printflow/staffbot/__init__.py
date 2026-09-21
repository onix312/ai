"""Бот сотрудников PrintFlow — тонкий, notify_only + Mini App.

Архитектура clean_layers:
- core/config, core/db, core/api_client
- router
- handlers/menu (кнопка web_app), handlers/notify (все уведомления)
- scenes SQLite с нуля
- ui keyboards
- miniapp auth HMAC
"""
from .bot import StaffBot, TelegramBot
from .router import suggest_command
from .ui import HELP, REPLY_ALIASES, STATE_RU
from .scenes import BotScenes, SELL

__all__ = ["StaffBot", "TelegramBot", "suggest_command", "HELP", "REPLY_ALIASES", "STATE_RU", "BotScenes", "SELL"]
