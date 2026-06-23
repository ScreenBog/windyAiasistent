"""
Инструменты Windy AI Assistant.
Расширяемый реестр + плагины через register_tool().
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
import webbrowser
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote_plus

import requests

import bootstrap  # noqa: F401
import config

logger = logging.getLogger(__name__)

ToolHandler = Callable[[dict[str, Any]], str]
TOOL_REGISTRY: dict[str, ToolHandler] = {}


def register_tool(name: str, handler: ToolHandler, aliases: list[str] | None = None) -> None:
    key = name.strip().lower()
    TOOL_REGISTRY[key] = handler
    for a in aliases or []:
        TOOL_REGISTRY[a.strip().lower()] = handler
    logger.info("tool: %s", key)


def _type_unicode(text: str, interval: float = 0.04) -> None:
    import pyautogui
    if text.isascii():
        pyautogui.write(text, interval=interval)
        return
    safe = text.replace("'", "''")
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", f"Set-Clipboard -Value '{safe}'"],
        check=True, capture_output=True, timeout=5,
    )
    pyautogui.hotkey("ctrl", "v")


# ---------------------------------------------------------------------------
# Telegram (Telethon + Bot API + UI fallback)
# ---------------------------------------------------------------------------

def _tg_ui_send(message: str, contact: str) -> str:
    _tool_open_app({"name": "telegram"})
    time.sleep(2)
    import pyautogui
    if contact:
        pyautogui.hotkey("ctrl", "f")
        time.sleep(0.4)
        _type_unicode(contact)
        time.sleep(0.6)
        pyautogui.press("enter")
        time.sleep(0.4)
    _type_unicode(message, 0.03)
    time.sleep(0.15)
    pyautogui.press("enter")
    return "Сообщение отправлено (UI)"


def _bot_send(chat_id: str, message: str) -> str:
    token = config.TELEGRAM_BOT_TOKEN.strip()
    if not token:
        raise RuntimeError("bot token не задан")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    r = requests.post(url, json={"chat_id": chat_id, "text": message}, timeout=15)
    r.raise_for_status()
    return f"Отправлено в чат {chat_id}"


def _resolve_chat_id(params: dict[str, Any]) -> str:
    cid = str(params.get("chat_id") or config.TELEGRAM_DEFAULT_CHAT_ID or "").strip()
    if cid:
        return cid
    contact = str(params.get("contact") or params.get("to") or "").strip().lower()
    return config.TELEGRAM_CHATS.get(contact, "")


def _tool_telegram_message(params: dict[str, Any]) -> str:
    message = str(params.get("message") or params.get("text") or "").strip()
    contact = str(params.get("contact") or params.get("to") or "").strip()
    if not message:
        return "Сообщение пустое"

    # 1) Telethon (лучше для личных чатов)
    try:
        import telegram_client as tg
        if tg.is_configured():
            return tg.send_message(contact or "default", message)
    except Exception as exc:
        logger.warning("Telethon send: %s", exc)

    # 2) Bot API
    chat_id = _resolve_chat_id(params)
    if chat_id and config.TELEGRAM_BOT_TOKEN:
        try:
            return _bot_send(chat_id, message)
        except Exception as exc:
            logger.warning("Bot send: %s", exc)

    # 3) UI
    try:
        return _tg_ui_send(message, contact)
    except Exception as exc:
        return f"Ошибка Telegram: {exc}"


def _tool_telegram_read_last(params: dict[str, Any]) -> str:
    contact = str(params.get("contact") or params.get("to") or "").strip()
    limit = int(params.get("limit") or 5)

    try:
        import telegram_client as tg
        if tg.is_configured():
            return tg.read_last(contact or "default", limit)
    except Exception as exc:
        logger.warning("Telethon read: %s", exc)

    # Bot API fallback (только сообщения боту)
    token = config.TELEGRAM_BOT_TOKEN.strip()
    chat_id = _resolve_chat_id(params)
    if token and chat_id:
        try:
            off_path = config.DATA_DIR / "bot_offset.json"
            offset = 0
            if off_path.exists():
                import json
                offset = int(json.loads(off_path.read_text()).get("offset", 0))
            r = requests.get(
                f"https://api.telegram.org/bot{token}/getUpdates",
                params={"offset": offset, "timeout": 0},
                timeout=15,
            )
            r.raise_for_status()
            updates = r.json().get("result", [])
            texts: list[str] = []
            max_id = offset
            for u in updates:
                max_id = max(max_id, int(u["update_id"]) + 1)
                m = u.get("message", {})
                if str(m.get("chat", {}).get("id")) != str(chat_id):
                    continue
                t = m.get("text", "")
                if t:
                    texts.append(t)
            if max_id > offset:
                off_path.write_text(f'{{"offset":{max_id}}}', encoding="utf-8")
            if texts:
                return " | ".join(texts[-limit:])
            return "Нет новых сообщений боту"
        except Exception as exc:
            return f"Bot read error: {exc}"

    return (
        "Настрой Telethon (api_id/api_hash + python telegram_client.py) "
        "или bot token + chat_id"
    )


# ---------------------------------------------------------------------------
# Apps / browser / system
# ---------------------------------------------------------------------------

def _tool_open_app(params: dict[str, Any]) -> str:
    name = str(params.get("name") or params.get("app") or "").strip().lower()
    path = params.get("path")
    if path:
        p = Path(str(path))
        if not p.exists():
            return f"Не найден: {p}"
        os.startfile(str(p))  # type: ignore[attr-defined]
        return f"Открываю {p.name}"
    if not name:
        return "Приложение не указано"
    app = config.APP_PATHS.get(name)
    try:
        if not app:
            subprocess.Popen(name, shell=True)
            return f"Запуск {name}"
        if " --" in app or (app.count(" ") and not app.endswith(".exe")):
            subprocess.Popen(app, shell=True)
        else:
            os.startfile(app)  # type: ignore[attr-defined]
        return f"Открываю {name}"
    except Exception as exc:
        return f"Ошибка: {exc}"


def _tool_open_url(params: dict[str, Any]) -> str:
    url = str(params.get("url") or "").strip()
    if not url:
        return "URL пуст"
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    webbrowser.open(url)
    return f"Открываю {url}"


def _tool_youtube_search(params: dict[str, Any]) -> str:
    q = str(params.get("query") or params.get("q") or "").strip()
    if not q:
        return "Запрос пуст"
    webbrowser.open(f"https://www.youtube.com/results?search_query={quote_plus(q)}")
    return f"YouTube: {q}"


def _tool_type_text(params: dict[str, Any]) -> str:
    text = str(params.get("text") or "")
    if not text:
        return "Текст пуст"
    wait = float(params.get("wait", 1.0))
    if wait:
        time.sleep(wait)
    try:
        _type_unicode(text, float(params.get("delay", 0.04)))
        return "Напечатано"
    except Exception as exc:
        return f"Ошибка: {exc}"


def _tool_toggle_vpn(params: dict[str, Any]) -> str:
    bat = str(params.get("path") or config.VPN_TOGGLE_BAT)
    act = str(params.get("action") or "toggle").lower()
    if not bat or not Path(bat).exists():
        return f"VPN bat не найден: {bat}"
    try:
        if act in ("on", "start"):
            subprocess.Popen(["cmd", "/c", "start", "", bat], cwd=str(Path(bat).parent))
            return "VPN on"
        if act in ("off", "stop"):
            subprocess.run(["taskkill", "/F", "/IM", "winws.exe"], check=False)
            return "VPN off"
        chk = subprocess.run(["tasklist", "/FI", "IMAGENAME eq winws.exe"], capture_output=True, text=True)
        if "winws.exe" in chk.stdout:
            subprocess.run(["taskkill", "/F", "/IM", "winws.exe"], check=False)
            return "VPN off"
        subprocess.Popen(["cmd", "/c", "start", "", bat], cwd=str(Path(bat).parent))
        return "VPN on"
    except Exception as exc:
        return f"VPN error: {exc}"


def _tool_run_command(params: dict[str, Any]) -> str:
    cmd = str(params.get("command") or params.get("cmd") or "").strip()
    if not cmd:
        return "Команда пуста"
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=45)
        out = (r.stdout or r.stderr or "").strip()
        return out[:600] if out else f"exit {r.returncode}"
    except Exception as exc:
        return f"Ошибка: {exc}"


def _tool_open_console(params: dict[str, Any]) -> str:
    shell = str(params.get("shell") or "powershell").lower()
    try:
        if shell == "cmd":
            subprocess.Popen(["cmd.exe"], creationflags=subprocess.CREATE_NEW_CONSOLE)
        else:
            subprocess.Popen(["powershell.exe"], creationflags=subprocess.CREATE_NEW_CONSOLE)
        return f"Открыт {shell}"
    except Exception as exc:
        return f"Ошибка: {exc}"


def _tool_noop(params: dict[str, Any]) -> str:
    return str(params.get("message") or "OK")


def _register() -> None:
    register_tool("open_app", _tool_open_app, ["open"])
    register_tool("open_url", _tool_open_url)
    register_tool("youtube_search", _tool_youtube_search, ["search_youtube"])
    register_tool("telegram_message", _tool_telegram_message, ["telegram_send", "send_telegram"])
    register_tool("telegram_read_last", _tool_telegram_read_last, ["telegram_read", "read_telegram"])
    register_tool("type_text", _tool_type_text)
    register_tool("toggle_vpn", _tool_toggle_vpn)
    register_tool("run_command", _tool_run_command)
    register_tool("open_console", _tool_open_console)
    register_tool("noop", _tool_noop)


_register()


class ToolExecutor:
    def __init__(self, registry: dict[str, ToolHandler] | None = None) -> None:
        self.registry = registry or TOOL_REGISTRY

    def execute(self, tool: str, params: dict[str, Any] | None = None) -> str:
        tool = (tool or "").strip().lower()
        params = params or {}
        fn = self.registry.get(tool)
        if not fn:
            return f"Неизвестная команда: {tool}"
        try:
            r = fn(params)
            logger.info("%s → %s", tool, r[:120])
            return r
        except Exception as exc:
            logger.error("%s: %s", tool, exc)
            return f"Ошибка {tool}: {exc}"

    def execute_all(self, actions: list) -> list[str]:
        return [
            self.execute(
                getattr(a, "tool", None) or a.get("tool", ""),
                getattr(a, "params", None) or a.get("params", {}),
            )
            for a in actions
        ]

    def list_tools(self) -> list[str]:
        return sorted(set(TOOL_REGISTRY))