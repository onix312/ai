"""Сборка бота сотрудников из примесей.

`BotCore` (core.py) — цикл, транспорт и диспетчер; примеси добавляют
сценарии: обзоры (views), принтеры (printers), полка и касса (sales),
каталог (catalog), заказы (orders), команда (team), inbox покупателя
(inbox), расписания (schedules). Маршруты всех команд — таблицы в
router.py: чтобы добавить команду, пишем метод в примеси и строку в
таблицу, не трогая диспетчер.
"""
from __future__ import annotations

from .catalog import CatalogMixin
from .core import BotCore
from .inbox import InboxMixin
from .orders import OrdersMixin
from .printers import PrintersMixin
from .schedules import SchedulesMixin
from .sales import SalesMixin
from .team import TeamMixin
from .views import ViewsMixin


class StaffBot(InboxMixin, CatalogMixin, OrdersMixin, SalesMixin,
              PrintersMixin, ViewsMixin, TeamMixin, SchedulesMixin, BotCore):
    """Рабочий бот PrintFlow: цех в кармане владельца и команды."""


# Историческое имя: manager и тесты знают бота сотрудников как TelegramBot.
TelegramBot = StaffBot
