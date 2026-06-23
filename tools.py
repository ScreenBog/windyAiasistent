"""
Инструменты Windy AI Assistant.
Telegram: send/read через Bot API (с UI-fallback для send).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote_plus

import requests

import bootstrap  # noqa: F401
import config

logger = logging.getLogger(__name__)

ToolHandler = Callable[[dict[str, Any]], str]
TOOL_REGISTRY: dict[str, ToolHandler] = {}


# ---------------------------------------------------------------------------
# Telegram Bot API
# ---------------------------------------------------------------------------

@dataclass
class TelegramMessage:
    chat_id: str
    sender: str
    text: str
    date: int


class TelegramBot:
    """
    Обёртка Telegram Bot API.
    Read: getUpdates + offset (сохраняется в data/telegram_offset.json).
    Send: sendMessage.
    """

    def __init__(self, token: str) -> None:
        self.token = token.strip()
        self.base = f"https://api.telegram.org/bot{self.token}"
        self.state_path = config.TELEGRAM_STATE_PATH

    def _api(self, method: str, **params) -> dict:
        try:
            resp = requests.get(f"{self.base}/{method}", params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            if not data.get("ok"):
                raise RuntimeError(data.get("description", "Telegram API error"))
            return data
        except Exception as exc:
            logger.error("Telegram API %s: %s", method, exc)
            raise

    def _post(self, method: str, payload: dict) -> dict:
        try:
            resp = requests.post(f"{self.base}/{method}", json=payload, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            if not data.get("ok"):
                raise RuntimeError(data.get("description", "Telegram API error"))
            return data
        except Exception as exc:
            logger.error("Telegram API %s: %s", method, exc)
            raise

    def _load_offset(self) -> int:
        try:
            if self.state_path.exists():
                return int(json.loads(self.state_path.read_text(encoding="utf-8")).get("offset", 0))
        except Exception:
            pass
        return 0

    def _save_offset(self, offset: int) -> None:
        try:
            self.state_path.write_text(json.dumps({"offset": offset}), encoding="utf-8")
        except Exception as exc:
            logger.warning("Не сохранён offset: %s", exc)

    def test_connection(self) -> str:
        data = self._api("getMe")
        username = data.get("result", {}).get("username", "?")
        return f"Бот @{username} подключён"

    def send_message(self, chat_id: str, text: str) -> str:
        self._post("sendMessage", {"chat_id": chat_id, "text": text})
        return f"Отправлено в чат {chat_id}"

    def fetch_messages(
        self,
        chat_id: str | None = None,
        limit: int = 5,
    ) -> list[TelegramMessage]:
        """
        Читает новые сообщения боту через getUpdates.
        Важно: бот видит только чаты, где ему писали (или группы с ботом).
        """
        offset = self._load_offset()
        # allowed_updates не фильтруем — совместимость с requests params
        data = self._api("getUpdates", offset=offset, timeout=0)
        updates = data.get("result", [])
        messages: list[TelegramMessage] = []
        max_id = offset

        for upd in updates:
            upd_id = int(upd.get("update_id", 0))
            max_id = max(max_id, upd_id + 1)
            msg = upd.get("message") or {}
            cid = str(msg.get("chat", {}).get("id", ""))
            if chat_id and cid != str(chat_id):
                continue
            text = msg.get("text") or msg.get("caption") or ""
            if not text:
                continue
            sender = (
                msg.get("from", {}).get("first_name")
                or msg.get("from", {}).get("username")
                or msg.get("chat", {}).get("title")
                or "?"
            )
            messages.append(TelegramMessage(
                chat_id=cid,
                sender=str(sender),
                text=text,
                date=int(msg.get("date", 0)),
            ))

        if max_id > offset:
            self._save_offset(max_id)

        return messages[-limit:]


def _resolve_chat_id(params: dict[str, Any]) -> str | None:
    """chat_id из params, default или маппинга contact → chat_id."""
    chat_id = str(params.get("chat_id") or config.TELEGRAM_DEFAULT_CHAT_ID or "").strip()
    if chat_id:
        return chat_id
    contact = str(params.get("contact") or params.get("to") or "").strip().lower()
    if contact:
        return config.TELEGRAM_CHATS.get(contact)
    return None


def _get_bot() -> TelegramBot | None:
    token = config.TELEGRAM_BOT_TOKEN.strip()
    if not token:
        return None
    return TelegramBot(token)


# ---------------------------------------------------------------------------
# Утилиты
# ---------------------------------------------------------------------------

def register_tool(name: str, handler: ToolHandler, aliases: list[str] | None = None) -> None:
    key = name.strip().lower()
    TOOL_REGISTRY[key] = handler
    for alias in aliases or []:
        TOOL_REGISTRY[alias.strip().lower()] = handler
    logger.info("Инструмент: %s", key)


def _type_unicode(text: str, interval: float = 0.05) -> None:
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


def _resolve_app_path(name: str) -> str | None:
    return config.APP_PATHS.get(name.strip().lower())


# ---------------------------------------------------------------------------
# Браузер / приложения / Steam (без изменений логики)
# ---------------------------------------------------------------------------

def _tool_open_url(params: dict[str, Any]) -> str:
    url = str(params.get("url") or "").strip()
    if not url:
        return "URL не указан"
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    webbrowser.open(url)
    return f"Открываю {url}"


def _tool_browser_search(params: dict[str, Any]) -> str:
    engine = str(params.get("engine") or "google").lower()
    query = str(params.get("query") or params.get("q") or "").strip()
    if not query:
        return "Пустой запрос"
    urls = {
        "google": f"https://www.google.com/search?q={quote_plus(query)}",
        "yandex": f"https://yandex.ru/search/?text={quote_plus(query)}",
        "bing": f"https://www.bing.com/search?q={quote_plus(query)}",
    }
    webbrowser.open(urls.get(engine, urls["google"]))
    return f"Ищу ({engine}): {query}"


def _tool_youtube_search(params: dict[str, Any]) -> str:
    query = str(params.get("query") or params.get("q") or "").strip()
    if not query:
        return "Пустой запрос"
    webbrowser.open(f"https://www.youtube.com/results?search_query={quote_plus(query)}")
    return f"YouTube: {query}"


def _tool_youtube_play(params: dict[str, Any]) -> str:
    video_id = str(params.get("video_id") or "").strip()
    query = str(params.get("query") or params.get("q") or "").strip()
    if video_id:
        url = f"https://www.youtube.com/watch?v={video_id}"
    elif query:
        url = f"https://www.youtube.com/results?search_query={quote_plus(query)}"
    else:
        return "Укажи query или video_id"
    webbrowser.open(url)
    return "Открываю YouTube"


def _tool_open_app(params: dict[str, Any]) -> str:
    name = str(params.get("name") or params.get("app") or "").strip().lower()
    path = params.get("path")
    if path:
        target = Path(str(path))
        if not target.exists():
            return f"Файл не найден: {target}"
        os.startfile(str(target))  # type: ignore[attr-defined]
        return f"Открываю {target.name}"
    if not name:
        return "Не указано приложение"
    app_path = _resolve_app_path(name)
    try:
        if not app_path:
            subprocess.Popen(name, shell=True)
            return f"Запускаю {name}"
        if " --" in app_path or (app_path.count(" ") and not app_path.endswith(".exe")):
            subprocess.Popen(app_path, shell=True)
        else:
            os.startfile(app_path)  # type: ignore[attr-defined]
        return f"Открываю {name}"
    except Exception as exc:
        return f"Ошибка {name}: {exc}"


def _tool_launch_steam_game(params: dict[str, Any]) -> str:
    game = str(params.get("game") or params.get("name") or "").strip().lower()
    app_id = str(params.get("app_id") or params.get("id") or "").strip()
    if not app_id and game:
        app_id = config.STEAM_GAMES.get(game, "")
    if not app_id:
        return f"Игра не найдена. Добавь в steam_games: {game}"
    try:
        os.startfile(f"steam://run/{app_id}")  # type: ignore[attr-defined]
        return f"Запускаю Steam {app_id}"
    except Exception:
        steam = _resolve_app_path("steam")
        if steam:
            subprocess.Popen([steam, "-applaunch", app_id])
            return f"Запускаю {app_id}"
        return "Ошибка Steam"


def _tool_open_steam(params: dict[str, Any]) -> str:
    return _tool_open_app({"name": "steam"})


# ---------------------------------------------------------------------------
# Telegram send / read
# ---------------------------------------------------------------------------

def _telegram_ui_send(message: str, contact: str) -> str:
    """Fallback: отправка через UI Telegram Desktop."""
    _tool_open_app({"name": "telegram"})
    time.sleep(2.0)
    import pyautogui

    if contact:
        pyautogui.hotkey("ctrl", "f")
        time.sleep(0.4)
        _type_unicode(contact, 0.04)
        time.sleep(0.7)
        pyautogui.press("enter")
        time.sleep(0.5)
    _type_unicode(message, 0.03)
    time.sleep(0.2)
    pyautogui.press("enter")
    return "Сообщение отправлено (UI)"


def _tool_telegram_send(params: dict[str, Any]) -> str:
    message = str(params.get("message") or params.get("text") or "").strip()
    contact = str(params.get("contact") or params.get("to") or "").strip()
    if not message:
        return "Сообщение пустое"

    chat_id = _resolve_chat_id(params)
    bot = _get_bot()

    # Приоритет: Bot API
    if bot and chat_id:
        try:
            return bot.send_message(chat_id, message)
        except Exception as exc:
            logger.warning("Bot API send failed, UI fallback: %s", exc)

    try:
        return _telegram_ui_send(message, contact)
    except Exception as exc:
        return f"Ошибка Telegram send: {exc}"


def _tool_telegram_read(params: dict[str, Any]) -> str:
    """
    Чтение через Bot API (getUpdates).
    Требует: telegram_bot_token + chat_id (или contact в telegram_chats).
    Пользователь должен хотя бы раз написать боту.
    """
    limit = int(params.get("limit") or 5)
    chat_id = _resolve_chat_id(params)
    bot = _get_bot()

    if not bot:
        return (
            "Telegram read: укажи telegram_bot_token в settings.json. "
            "Создай бота через @BotFather."
        )

    if not chat_id:
        return (
            "Telegram read: укажи telegram_default_chat_id или "
            "telegram_chats (имя → chat_id). Узнать chat_id: напиши боту /start, "
            "затем открой https://api.telegram.org/bot<TOKEN>/getUpdates"
        )

    try:
        messages = bot.fetch_messages(chat_id=chat_id, limit=limit)
        if not messages:
            return (
                f"Нет новых сообщений в чате {chat_id}. "
                "Напиши боту любое сообщение и повтори."
            )
        parts = [f"{m.sender}: {m.text}" for m in messages]
        return " | ".join(parts)
    except Exception as exc:
        logger.error("telegram_read: %s", exc)
        return f"Ошибка чтения Telegram: {exc}"


# ---------------------------------------------------------------------------
# Прочие инструменты
# ---------------------------------------------------------------------------

def _tool_type_text(params: dict[str, Any]) -> str:
    text = str(params.get("text") or params.get("message") or "")
    if not text:
        return "Нет текста"
    try:
        import pyautogui

        wait = float(params.get("wait", 1.0))
        if wait > 0:
            time.sleep(wait)
        _type_unicode(text, float(params.get("delay", 0.05)))
        return "Текст напечатан"
    except Exception as exc:
        return f"Ошибка ввода: {exc}"


def _tool_toggle_vpn(params: dict[str, Any]) -> str:
    bat_path = str(params.get("path") or config.VPN_TOGGLE_BAT)
    action = str(params.get("action") or "toggle").lower()
    if not bat_path or not Path(bat_path).exists():
        return f"VPN-скрипт не найден: {bat_path}"
    try:
        if action in ("on", "start", "enable"):
            subprocess.Popen(["cmd", "/c", "start", "", bat_path], cwd=str(Path(bat_path).parent))
            return "VPN включён"
        if action in ("off", "stop", "disable"):
            subprocess.run(["taskkill", "/F", "/IM", "winws.exe"], capture_output=True, check=False)
            return "VPN выключен"
        check = subprocess.run(["tasklist", "/FI", "IMAGENAME eq winws.exe"], capture_output=True, text=True, check=False)
        if "winws.exe" in check.stdout:
            subprocess.run(["taskkill", "/F", "/IM", "winws.exe"], check=False)
            return "VPN выключен"
        subprocess.Popen(["cmd", "/c", "start", "", bat_path], cwd=str(Path(bat_path).parent))
        return "VPN включён"
    except Exception as exc:
        return f"Ошибка VPN: {exc}"


def _tool_volume(params: dict[str, Any]) -> str:
    import pyautogui

    action = str(params.get("action") or "up").lower()
    steps = int(params.get("steps") or 3)
    if action == "mute":
        pyautogui.press("volumemute")
        return "Mute"
    key = "volumeup" if action in ("up", "increase") else "volumedown"
    for _ in range(max(1, steps)):
        pyautogui.press(key)
    return "Громкость изменена"


def _tool_run_command(params: dict[str, Any]) -> str:
    cmd = str(params.get("command") or params.get("cmd") or "").strip()
    if not cmd:
        return "Команда не указана"
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
        out = (r.stdout or r.stderr or "").strip()
        return out[:500] if out else f"Код {r.returncode}"
    except Exception as exc:
        return f"Ошибка: {exc}"


def _tool_noop(params: dict[str, Any]) -> str:
    return str(params.get("message") or "Готово")


def _register_builtin_tools() -> None:
    register_tool("open_app", _tool_open_app, ["open"])
    register_tool("open_url", _tool_open_url)
    register_tool("browser_search", _tool_browser_search, ["search_web", "google_search"])
    register_tool("youtube_search", _tool_youtube_search, ["search_youtube"])
    register_tool("youtube_play", _tool_youtube_play)
    register_tool("open_steam", _tool_open_steam)
    register_tool("launch_steam_game", _tool_launch_steam_game, ["launch_game", "open_game"])
    register_tool("telegram_send", _tool_telegram_send, ["telegram_message", "send_telegram"])
    register_tool("telegram_read", _tool_telegram_read, ["read_telegram"])
    register_tool("type_text", _tool_type_text)
    register_tool("toggle_vpn", _tool_toggle_vpn)
    register_tool("volume", _tool_volume)
    register_tool("run_command", _tool_run_command)
    register_tool("noop", _tool_noop)


_register_builtin_tools()


class ToolExecutor:
    def __init__(self, registry: dict[str, ToolHandler] | None = None) -> None:
        self.registry = registry or TOOL_REGISTRY

    def execute(self, tool: str, params: dict[str, Any] | None = None) -> str:
        tool = (tool or "").strip().lower()
        params = params or {}
        handler = self.registry.get(tool)
        if not handler:
            return f"Неизвестная команда: {tool}"
        try:
            result = handler(params)
            logger.info("%s → %s", tool, result)
            return result
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
        return sorted(set(TOOL_REGISTRY.keys()))