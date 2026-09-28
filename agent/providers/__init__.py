"""Capability providers for NOZZA Assistant.

Providers own integrations with external surfaces. Browser, desktop, PrintFlow
and personal capabilities are migrated first; files, Telegram and Windows follow
in separate slices. The executor keeps policy/confirmation; providers only
implement the capability behind already validated skills.
"""
from .registry import catalog, for_skill

__all__ = ["catalog", "for_skill"]
