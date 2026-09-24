"""Бот сотрудников PrintFlow — тонкий: уведомления, Mini App и текст.

Архитектура clean_layers:
- core/config, core/db, core/api_client, core/staff_service (только чтение БД)
- router
- report (формулировки) + handlers/menu (web_app), handlers/notify (уведомления),
  handlers/report (18.12.3: сводка, заказы, полка, очередь, кадр, деньги —
  текстом с картинками)
- scenes SQLite с нуля
- ui keyboards
- miniapp auth HMAC
"""
from .bot import StaffBot, TelegramBot
from .router import suggest_command
from .ui import HELP, REPLY_ALIASES, STATE_RU
from .scenes import BotScenes, SELL

__all__ = ["StaffBot", "TelegramBot", "suggest_command", "HELP", "REPLY_ALIASES", "STATE_RU", "BotScenes", "SELL"]
