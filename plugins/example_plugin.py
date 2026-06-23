"""Пример плагина — добавь свои инструменты."""

from __future__ import annotations

import datetime


def setup(register) -> None:
    def get_time(_params: dict) -> str:
        return f"Сейчас {datetime.datetime.now():%H:%M}"

    def get_date(_params: dict) -> str:
        return f"Сегодня {datetime.datetime.now():%d.%m.%Y}"

    register("get_time", get_time)
    register("get_date", get_date)