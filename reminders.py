"""
Голосовые заметки и напоминания.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import bootstrap  # noqa: F401
import config

logger = logging.getLogger(__name__)

NOTES_DIR = config.DATA_DIR / "notes"
REMINDERS_PATH = config.DATA_DIR / "reminders.json"
NOTES_DIR.mkdir(exist_ok=True)

_on_reminder: Callable[[str], None] | None = None
_thread: threading.Thread | None = None
_running = False


def set_reminder_callback(cb: Callable[[str], None] | None) -> None:
    global _on_reminder
    _on_reminder = cb


def _load_reminders() -> list[dict[str, Any]]:
    try:
        if REMINDERS_PATH.exists():
            return json.loads(REMINDERS_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("reminders load: %s", exc)
    return []


def _save_reminders(items: list[dict[str, Any]]) -> None:
    REMINDERS_PATH.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def save_voice_note(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return "Заметка пуста"
    fname = datetime.now().strftime("%Y%m%d_%H%M%S") + ".txt"
    path = NOTES_DIR / fname
    path.write_text(text, encoding="utf-8")
    logger.info("note saved: %s", path)
    return f"Заметка сохранена: {fname}"


def add_reminder(text: str, when: str) -> str:
    """
    when: HH:MM или YYYY-MM-DD HH:MM
    """
    text = (text or "").strip()
    when = (when or "").strip()
    if not text or not when:
        return "Укажи текст и время напоминания"
    items = _load_reminders()
    items.append({"text": text, "when": when, "done": False})
    _save_reminders(items)
    return f"Напоминание на {when}: {text}"


def list_reminders() -> str:
    items = [r for r in _load_reminders() if not r.get("done")]
    if not items:
        return "Напоминаний нет"
    return " | ".join(f"{r['when']}: {r['text']}" for r in items[-10:])


def _check_due() -> None:
    now = datetime.now()
    items = _load_reminders()
    changed = False
    for r in items:
        if r.get("done"):
            continue
        try:
            when_str = r.get("when", "")
            if len(when_str) <= 5:
                due = datetime.strptime(f"{now.date()} {when_str}", "%Y-%m-%d %H:%M")
            else:
                due = datetime.strptime(when_str, "%Y-%m-%d %H:%M")
            if now >= due:
                r["done"] = True
                changed = True
                msg = f"Напоминание: {r.get('text', '')}"
                logger.info(msg)
                if _on_reminder:
                    try:
                        _on_reminder(msg)
                    except Exception:
                        pass
        except ValueError:
            continue
    if changed:
        _save_reminders(items)


def _loop() -> None:
    while _running:
        try:
            _check_due()
        except Exception as exc:
            logger.error("reminder loop: %s", exc)
        time.sleep(30)


def start_reminder_service() -> None:
    global _running, _thread
    if _running:
        return
    _running = True
    _thread = threading.Thread(target=_loop, daemon=True, name="Reminders")
    _thread.start()
    logger.info("reminder service started")


def stop_reminder_service() -> None:
    global _running
    _running = False