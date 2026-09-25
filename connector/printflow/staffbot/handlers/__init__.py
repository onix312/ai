"""Handlers слой — меню, уведомления, отчёты и ассистент бота.

19.0: web_app-кнопок и внешних адресов нет — отчёты, действия и вопросы
ассистенту живут в самом чате.
"""
from .menu import MenuMixin
from .notify import NotifyMixin
from .report import ReportMixin

__all__ = ["MenuMixin", "NotifyMixin", "ReportMixin"]
