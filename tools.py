"""
Инструменты Windy AI Assistant.
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
import reminders

logger = logging.getLogger(__name__)

ToolHandler = Callable[[dict[str, Any]], str]
TOOL_REGISTRY: dict[str, ToolHandler] = {}


def register_tool(name: str, handler: ToolHandler, aliases: list[str] | None = None) -> None:
    TOOL_REGISTRY[name.strip().lower()] = handler
    for a in aliases or []:
        TOOL_REGISTRY[a.strip().lower()] = handler


def _type_unicode(text: str, interval: float = 0.04) -> None:
    import pyautogui
    if text.isascii():
        pyautogui.write(text, interval=interval)
        return
    s = text.replace("'", "''")
    subprocess.run(["powershell", "-NoProfile", "-Command", f"Set-Clipboard -Value '{s}'"], check=True, capture_output=True, timeout=5)
    pyautogui.hotkey("ctrl", "v")


# --- Telegram ---

def _tool_telegram_send_message(params: dict[str, Any]) -> str:
    msg = str(params.get("message") or params.get("text") or "").strip()
    contact = str(params.get("contact") or params.get("to") or "").strip()
    if not msg:
        return "Сообщение пустое"
    try:
        import telegram_client as tg
        if tg.is_configured():
            return tg.send_message(contact, msg)
    except Exception as exc:
        logger.warning("telethon send: %s", exc)
    return _tool_telegram_message_ui({"message": msg, "contact": contact})


def _tool_telegram_message_ui(params: dict[str, Any]) -> str:
    msg = str(params.get("message") or "").strip()
    contact = str(params.get("contact") or "").strip()
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
    _type_unicode(msg, 0.03)
    pyautogui.press("enter")
    return "Отправлено (UI)"


def _tool_telegram_read_last(params: dict[str, Any]) -> str:
    contact = str(params.get("contact") or params.get("to") or "").strip()
    limit = int(params.get("limit") or 5)
    try:
        import telegram_client as tg
        if tg.is_configured():
            return tg.read_last(contact, limit)
    except Exception as exc:
        return f"Telegram read: {exc}"
    return "Настрой Telethon (api_id/hash)"


def _tool_telegram_get_unread(params: dict[str, Any]) -> str:
    limit = int(params.get("limit") or 10)
    try:
        import telegram_client as tg
        if tg.is_configured():
            return tg.get_unread(limit)
    except Exception as exc:
        return f"Unread: {exc}"
    return "Настрой Telethon"


# --- System / media ---

def _tool_set_brightness(params: dict[str, Any]) -> str:
    level = int(params.get("level") or params.get("percent") or 70)
    level = max(0, min(100, level))
    action = str(params.get("action") or "").lower()
    try:
        if action in ("up", "increase"):
            level = min(100, level + 10)
        elif action in ("down", "decrease"):
            level = max(0, level - 10)
        ps = f"(Get-WmiObject -Namespace root/WMI -Class WmiMonitorBrightnessMethods).WmiSetBrightness(1,{level})"
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True, capture_output=True, timeout=10)
        return f"Яркость {level}%"
    except Exception as exc:
        return f"Яркость: {exc} (нужен внешний монитор WMI)"


def _tool_volume(params: dict[str, Any]) -> str:
    import pyautogui
    act = str(params.get("action") or "up").lower()
    steps = int(params.get("steps") or 3)
    if act == "mute":
        pyautogui.press("volumemute")
        return "Mute"
    key = "volumeup" if act in ("up", "increase") else "volumedown"
    for _ in range(max(1, steps)):
        pyautogui.press(key)
    return "Громкость изменена"


def _tool_voice_note(params: dict[str, Any]) -> str:
    return reminders.save_voice_note(str(params.get("text") or params.get("note") or ""))


def _tool_set_reminder(params: dict[str, Any]) -> str:
    return reminders.add_reminder(str(params.get("text") or ""), str(params.get("when") or params.get("time") or ""))


def _tool_list_reminders(params: dict[str, Any]) -> str:
    return reminders.list_reminders()


def _tool_search_files(params: dict[str, Any]) -> str:
    query = str(params.get("query") or params.get("name") or "").strip()
    if not query:
        return "Укажи имя файла"
    roots = params.get("roots") or config.FILE_SEARCH_ROOTS
    limit = int(params.get("limit") or 8)
    found: list[str] = []
    for root in roots:
        rp = Path(str(root))
        if not rp.exists():
            continue
        try:
            for p in rp.rglob(f"*{query}*"):
                if p.is_file():
                    found.append(str(p))
                    if len(found) >= limit:
                        return " | ".join(found)
        except PermissionError:
            continue
    return " | ".join(found) if found else f"Не найдено: {query}"


def _tool_browser_tab(params: dict[str, Any]) -> str:
    import pyautogui
    act = str(params.get("action") or "next").lower()
    if act in ("next", "right"):
        pyautogui.hotkey("ctrl", "tab")
    elif act in ("prev", "previous", "left"):
        pyautogui.hotkey("ctrl", "shift", "tab")
    elif act == "close":
        pyautogui.hotkey("ctrl", "w")
    elif act == "new":
        pyautogui.hotkey("ctrl", "t")
    else:
        return f"Неизвестное действие: {act}"
    return f"Вкладка: {act}"


def _tool_open_app(params: dict[str, Any]) -> str:
    name = str(params.get("name") or params.get("app") or "").strip().lower()
    path = params.get("path")
    if path:
        p = Path(str(path))
        if not p.exists():
            return f"Не найден: {p}"
        os.startfile(str(p))  # type: ignore
        return f"Открываю {p.name}"
    if not name:
        return "Приложение не указано"
    app = config.APP_PATHS.get(name)
    try:
        if not app:
            subprocess.Popen(name, shell=True)
            return f"Запуск {name}"
        if " --" in app:
            subprocess.Popen(app, shell=True)
        else:
            os.startfile(app)  # type: ignore
        return f"Открываю {name}"
    except Exception as exc:
        return f"Ошибка: {exc}"


def _tool_launch_game(params: dict[str, Any]) -> str:
    game = str(params.get("game") or params.get("name") or "").strip().lower()
    app_id = str(params.get("app_id") or config.STEAM_GAMES.get(game, ""))
    if not app_id:
        return f"Игра не найдена: {game}"
    try:
        os.startfile(f"steam://run/{app_id}")  # type: ignore
        return f"Запуск {app_id}"
    except Exception:
        steam = config.APP_PATHS.get("steam")
        if steam:
            subprocess.Popen([steam, "-applaunch", app_id])
            return f"Steam {app_id}"
        return "Ошибка Steam"


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
    time.sleep(float(params.get("wait", 1.0)))
    _type_unicode(text)
    return "Напечатано"


def _tool_toggle_vpn(params: dict[str, Any]) -> str:
    bat = str(params.get("path") or config.VPN_TOGGLE_BAT)
    act = str(params.get("action") or "toggle").lower()
    if not bat or not Path(bat).exists():
        return f"VPN bat не найден"
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
        return str(exc)


def _tool_run_command(params: dict[str, Any]) -> str:
    cmd = str(params.get("command") or params.get("cmd") or "").strip()
    if not cmd:
        return "Команда пуста"
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=45)
        out = (r.stdout or r.stderr or "").strip()
        return out[:600] if out else f"exit {r.returncode}"
    except Exception as exc:
        return str(exc)


def _tool_open_console(params: dict[str, Any]) -> str:
    shell = str(params.get("shell") or "powershell").lower()
    try:
        exe = "cmd.exe" if shell == "cmd" else "powershell.exe"
        subprocess.Popen([exe], creationflags=subprocess.CREATE_NEW_CONSOLE)
        return f"Открыт {shell}"
    except Exception as exc:
        return str(exc)


def _tool_noop(params: dict[str, Any]) -> str:
    return str(params.get("message") or "OK")


def _register() -> None:
    register_tool("open_app", _tool_open_app, ["open"])
    register_tool("launch_game", _tool_launch_game, ["launch_steam_game", "open_game"])
    register_tool("open_url", _tool_open_url)
    register_tool("youtube_search", _tool_youtube_search)
    register_tool("telegram_send_message", _tool_telegram_send_message, ["telegram_message", "telegram_send"])
    register_tool("telegram_read_last", _tool_telegram_read_last, ["telegram_read"])
    register_tool("telegram_get_unread", _tool_telegram_get_unread)
    register_tool("type_text", _tool_type_text)
    register_tool("toggle_vpn", _tool_toggle_vpn)
    register_tool("run_command", _tool_run_command)
    register_tool("open_console", _tool_open_console)
    register_tool("set_brightness", _tool_set_brightness, ["brightness"])
    register_tool("volume", _tool_volume)
    register_tool("voice_note", _tool_voice_note, ["save_note"])
    register_tool("set_reminder", _tool_set_reminder, ["reminder"])
    register_tool("list_reminders", _tool_list_reminders)
    register_tool("search_files", _tool_search_files, ["find_file"])
    register_tool("browser_tab", _tool_browser_tab)
    register_tool("noop", _tool_noop)


_register()


class ToolExecutor:
    def __init__(self, registry: dict[str, ToolHandler] | None = None) -> None:
        self.registry = registry or TOOL_REGISTRY

    def execute(self, tool: str, params: dict[str, Any] | None = None) -> str:
        tool = (tool or "").strip().lower()
        fn = self.registry.get(tool)
        if not fn:
            return f"Неизвестная команда: {tool}"
        try:
            return fn(params or {})
        except Exception as exc:
            logger.error("%s: %s", tool, exc)
            return f"Ошибка {tool}: {exc}"

    def execute_all(self, actions: list) -> list[str]:
        return [self.execute(getattr(a, "tool", None) or a.get("tool", ""), getattr(a, "params", None) or a.get("params", {})) for a in actions]

    def list_tools(self) -> list[str]:
        return sorted(set(TOOL_REGISTRY))