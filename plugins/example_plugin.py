"""
Пример плагина Windy AI Assistant.

Скопируй этот файл, переименуй и добавь свои инструменты.
"""

from __future__ import annotations

import datetime


def setup(register) -> None:
    """Вызывается plugin_manager при старте."""

    def get_time(params: dict) -> str:
        """Текущее время."""
        now = datetime.datetime.now().strftime("%H:%M")
        return f"Сейчас {now}"

    def get_date(params: dict) -> str:
        """Текущая дата."""
        today = datetime.datetime.now().strftime("%d.%m.%Y")
        return f"Сегодня {today}"

    register("get_time", get_time)
    register("get_date", get_date)