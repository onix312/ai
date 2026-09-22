"""Handlers слой — меню, уведомления и текстовые отчёты бота.

18.12.3: у бота появился третий миксин — `ReportMixin`: сводка, принтеры,
заказы, полка, очередь, кадр и деньги отвечают текстом и фотографиями, пока
Mini App недоступен.
"""
from .menu import MenuMixin
from .notify import NotifyMixin
from .report import ReportMixin

__all__ = ["MenuMixin", "NotifyMixin", "ReportMixin"]
