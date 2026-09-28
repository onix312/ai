"""Capability providers for NOZZA Assistant.

Providers own integrations with external surfaces (browser, desktop, later files,
PrintFlow, Telegram). The executor keeps policy/confirmation; providers only
implement the capability behind already validated skills.
"""
from .registry import catalog, for_skill

__all__ = ["catalog", "for_skill"]
