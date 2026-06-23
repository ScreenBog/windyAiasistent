"""
История команд ассистента (для GUI и отладки).
"""

from __future__ import annotations

import json
import logging
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

import bootstrap  # noqa: F401
import config

logger = logging.getLogger(__name__)

HISTORY_PATH = config.DATA_DIR / "command_history.json"
_MAX = 100
_buffer: deque[dict[str, Any]] = deque(maxlen=_MAX)


def load_history() -> None:
    global _buffer
    try:
        if HISTORY_PATH.exists():
            data = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
            _buffer = deque(data[-_MAX:], maxlen=_MAX)
    except Exception as exc:
        logger.warning("history load: %s", exc)


def add_entry(command: str, response: str) -> None:
    entry = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "command": command[:500],
        "response": response[:500],
    }
    _buffer.append(entry)
    try:
        HISTORY_PATH.write_text(
            json.dumps(list(_buffer), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.warning("history save: %s", exc)


def get_history(limit: int = 20) -> list[dict[str, Any]]:
    return list(_buffer)[-limit:]


load_history()