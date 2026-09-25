"""Бот сотрудников PrintFlow — уведомления, кнопки и ассистент (19.0).

Архитектура clean_layers:
- core/config, core/db, core/api_client, core/staff_service (только чтение БД)
- router
- report (формулировки) + handlers/menu, handlers/notify (уведомления),
  handlers/report (сводка, заказы, полка, очередь, кадр, деньги — текстом
  с картинками; действия по уведомлениям), assistant (вопросы мозгом помощника)
- scenes SQLite с нуля
- ui keyboards — только callback-кнопки
"""
from .bot import StaffBot, TelegramBot
from .router import suggest_command
from .ui import HELP, REPLY_ALIASES, STATE_RU
from .scenes import ASK, BotScenes, SELL

__all__ = ["StaffBot", "TelegramBot", "suggest_command", "HELP", "REPLY_ALIASES", "STATE_RU", "BotScenes", "SELL", "ASK"]
