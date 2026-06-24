"""
История команд ассистента (для GUI, обучения и отладки).
"""

from __future__ import annotations

import json
import logging
import uuid
from collections import deque
from datetime import datetime
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


def add_entry(
    command: str,
    response: str,
    *,
    macros: list | None = None,
    model: str = "",
    entry_id: str | None = None,
) -> str:
    eid = entry_id or uuid.uuid4().hex[:12]
    entry = {
        "id": eid,
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "command": command[:500],
        "response": response[:500],
        "macros": macros or [],
        "model": model,
        "wrong": False,
        "feedback": "",
    }
    _buffer.append(entry)
    try:
        HISTORY_PATH.write_text(
            json.dumps(list(_buffer), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.warning("history save: %s", exc)

    if config.LEARNING_ENABLED:
        try:
            import learning
            learning.record_command(command, response, macros=macros, model=model)
        except Exception as exc:
            logger.debug("learning record: %s", exc)

    return eid


def mark_last_wrong(command: str, feedback: str = "") -> bool:
    """Пометить последнюю запись с такой командой как неверную."""
    norm = command.strip().lower()
    for entry in reversed(_buffer):
        if entry.get("command", "").strip().lower() == norm or not norm:
            entry["wrong"] = True
            entry["feedback"] = feedback[:300]
            try:
                HISTORY_PATH.write_text(
                    json.dumps(list(_buffer), ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except Exception as exc:
                logger.warning("history mark save: %s", exc)
            return True
    return False


def get_last_entry() -> dict[str, Any] | None:
    return _buffer[-1] if _buffer else None


def get_history(limit: int = 20) -> list[dict[str, Any]]:
    return list(_buffer)[-limit:]


load_history()