"""
Автообучение Windy — упрощённая система обратной связи.

Возможности:
  - Периодическое сканирование .lnk и установленных программ
  - Сбор статистики популярных команд
  - Пометка ответов как «неверно» с сохранением исправлений
  - Подсказки для system prompt (что пользователь исправлял)
"""

from __future__ import annotations

import json
import logging
import threading
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import bootstrap  # noqa: F401
import config

logger = logging.getLogger(__name__)

LEARNING_PATH = config.DATA_DIR / "learning.json"
_lock = threading.Lock()

_DEFAULT: dict[str, Any] = {
    "scanned_apps": {},
    "scanned_at": "",
    "command_counts": {},
    "corrections": [],
    "wrong_marks": [],
}


def _load() -> dict[str, Any]:
    if not LEARNING_PATH.exists():
        return json.loads(json.dumps(_DEFAULT))
    try:
        data = json.loads(LEARNING_PATH.read_text(encoding="utf-8"))
        for key, val in _DEFAULT.items():
            data.setdefault(key, val if not isinstance(val, dict) else {})
        return data
    except Exception as exc:
        logger.warning("learning load failed: %s", exc)
        return json.loads(json.dumps(_DEFAULT))


def _save(data: dict[str, Any]) -> None:
    try:
        LEARNING_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.warning("learning save failed: %s", exc)


def scan_apps_async(*, on_done: Any = None) -> None:
    """Фоновое сканирование ярлыков и программ."""

    def _work() -> None:
        try:
            import app_scanner
            found = app_scanner.scan_installed_apps(include_start_menu=True)
            with _lock:
                data = _load()
                data["scanned_apps"] = found
                data["scanned_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                _save(data)
            logger.info("learning scan: %d apps", len(found))
            if on_done:
                on_done(found)
        except Exception as exc:
            logger.error("learning scan: %s", exc)

    threading.Thread(target=_work, daemon=True, name="windy-learn-scan").start()


def get_scanned_apps() -> dict[str, str]:
    with _lock:
        return dict(_load().get("scanned_apps") or {})


def record_command(command: str, response: str, *, macros: list | None = None, model: str = "") -> str:
    """Записать выполненную команду; вернуть id записи."""
    entry_id = datetime.now().strftime("%Y%m%d%H%M%S%f")
    norm = _normalize_command(command)
    with _lock:
        data = _load()
        counts: Counter = Counter(data.get("command_counts") or {})
        counts[norm] += 1
        data["command_counts"] = dict(counts)
        marks = data.setdefault("wrong_marks", [])
        marks.append({
            "id": entry_id,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "command": command[:500],
            "response": response[:500],
            "macros": macros or [],
            "model": model,
            "wrong": False,
        })
        data["wrong_marks"] = marks[-config.LEARNING_MAX_ENTRIES :]
        _save(data)
    return entry_id


def mark_wrong(
    command: str,
    *,
    feedback: str = "",
    entry_id: str | None = None,
    suggested_macros: list | None = None,
) -> str:
    """Пометить последний (или указанный) ответ как неверный."""
    with _lock:
        data = _load()
        marks = data.get("wrong_marks") or []
        target = None
        if entry_id:
            target = next((m for m in reversed(marks) if m.get("id") == entry_id), None)
        if not target:
            norm = _normalize_command(command)
            for m in reversed(marks):
                if _normalize_command(m.get("command", "")) == norm:
                    target = m
                    break
        if not target and marks:
            target = marks[-1]

        if not target:
            return "Нет записи для пометки — выполни команду сначала"

        target["wrong"] = True
        target["feedback"] = feedback[:300]
        corrections = data.setdefault("corrections", [])
        corrections.append({
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "command": target.get("command", command)[:500],
            "wrong_response": target.get("response", "")[:500],
            "wrong_macros": target.get("macros") or [],
            "feedback": feedback[:300],
            "suggested_macros": suggested_macros or [],
        })
        data["corrections"] = corrections[-config.LEARNING_MAX_CORRECTIONS :]
        _save(data)
    logger.info("marked wrong: %s", (target or {}).get("command", command)[:80])
    return "Записано как неверный ответ. Windy учтёт это в следующих подсказках."


def get_popular_commands(limit: int = 10) -> list[tuple[str, int]]:
    with _lock:
        counts = _load().get("command_counts") or {}
    items = sorted(counts.items(), key=lambda x: -x[1])
    return items[:limit]


def get_prompt_hints(max_items: int = 5) -> str:
    """Краткие подсказки из исправлений и популярных команд для system prompt."""
    with _lock:
        data = _load()
    lines: list[str] = []

    corrections = (data.get("corrections") or [])[-max_items:]
    for c in corrections:
        cmd = c.get("command", "")
        fb = c.get("feedback", "")
        if cmd:
            hint = f"«{cmd}» — пользователь считал ответ неверным"
            if fb:
                hint += f" ({fb})"
            lines.append(hint)

    popular = get_popular_commands(5)
    if popular:
        top = ", ".join(f"{k}({v})" for k, v in popular[:5])
        lines.append(f"Частые команды: {top}")

    return "\n".join(lines) if lines else ""


def merge_scanned_into_config() -> int:
    """Дополнить APP_PATHS из последнего скана (не перезаписывая ручные)."""
    scanned = get_scanned_apps()
    if not scanned:
        return 0
    added = 0
    for key, path in scanned.items():
        if key not in config.APP_PATHS and key not in config.APP_PATHS_MANUAL:
            config.APP_PATHS[key] = path
            added += 1
    if added:
        try:
            config.save_settings()
        except Exception as exc:
            logger.warning("merge scan save: %s", exc)
    return added


def _normalize_command(command: str) -> str:
    import re
    c = (command or "").strip().lower()
    c = re.sub(r"\s+", " ", c)
    for prefix in ("эй винди", "hey винди", "винди", "эй windy"):
        if c.startswith(prefix):
            c = c[len(prefix) :].strip(" ,.!")
    return c[:200]