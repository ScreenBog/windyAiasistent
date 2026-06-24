"""
Инструменты Windy AI Assistant — реестр команд для LLM.
"""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
import tempfile
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
    for alias in aliases or []:
        TOOL_REGISTRY[alias.strip().lower()] = handler


def _safe_call(fn: ToolHandler, params: dict[str, Any], tool_name: str) -> str:
    try:
        return fn(params)
    except Exception as exc:
        logger.error("tool %s failed: %s", tool_name, exc, exc_info=True)
        return f"Ошибка {tool_name}: {exc}"


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


# ── Telegram ──────────────────────────────────────────────────────────────────

def _tg_contact(params: dict[str, Any]) -> str:
    """Контакт из params или default из config."""
    c = str(params.get("contact") or params.get("to") or params.get("name") or "").strip()
    if c:
        return c
    return config.TELEGRAM_DEFAULT_CONTACT.strip()


def _tool_telegram_send_message(params: dict[str, Any]) -> str:
    msg = str(params.get("message") or params.get("text") or "").strip()
    contact = _tg_contact(params)
    if not msg:
        return "Сообщение пустое"
    if not contact:
        return "Укажи контакт (имя, @username или добавь telegram_default_contact в настройках)"
    try:
        import telegram_client as tg
        if tg.is_configured():
            result = tg.send_message(contact, msg)
            if not result.startswith("Ошибка"):
                return result
            logger.warning("telethon send: %s", result)
    except Exception as exc:
        logger.warning("telethon send fallback: %s", exc)
    return _tool_telegram_message_ui({"message": msg, "contact": contact})


def _tool_telegram_message_ui(params: dict[str, Any]) -> str:
    """UI-fallback: открыть Telegram Desktop и напечатать."""
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
    return "Отправлено через UI Telegram"


def _tool_telegram_read_last(params: dict[str, Any]) -> str:
    contact = _tg_contact(params)
    count = int(params.get("count") or params.get("limit") or config.TELEGRAM_READ_DEFAULT_COUNT)
    if not contact:
        return "Укажи контакт: имя, @username или ID. Пример: «прочитай сообщения от Маши»"
    try:
        import telegram_client as tg
        if not tg.is_configured():
            return "Настрой Telegram: API ID и Hash в GUI → Telegram"
        return tg.read_last(contact, count)
    except Exception as exc:
        logger.error("telegram_read_last: %s", exc)
        return f"Ошибка чтения Telegram: {exc}"


def _tool_telegram_get_unread(params: dict[str, Any]) -> str:
    limit = int(params.get("limit") or params.get("count") or 10)
    try:
        import telegram_client as tg
        if not tg.is_configured():
            return "Настрой Telegram: API ID и Hash в GUI → Telegram"
        return tg.get_unread(limit)
    except Exception as exc:
        logger.error("telegram_get_unread: %s", exc)
        return f"Ошибка непрочитанных: {exc}"


def _tool_telegram_list_dialogs(params: dict[str, Any]) -> str:
    limit = int(params.get("limit") or 15)
    try:
        import telegram_client as tg
        if not tg.is_configured():
            return "Telegram не настроен"
        dialogs = tg.list_dialogs(limit)
        if not dialogs:
            return "Диалоги не найдены или нет авторизации"
        lines = [f"{d['name']} ({d['unread']} непроч.)" for d in dialogs[:limit]]
        return " | ".join(lines)
    except Exception as exc:
        return f"Ошибка списка чатов: {exc}"


def _tool_telegram_send_voice(params: dict[str, Any]) -> str:
    """Синтезирует TTS и отправляет голосовое в Telegram."""
    contact = str(params.get("contact") or params.get("to") or "").strip()
    text = str(params.get("message") or params.get("text") or "").strip()
    if not contact:
        return "Укажи контакт"
    if not text:
        return "Текст голосового пуст"

    try:
        import telegram_client as tg
        from voice import synthesize_to_file

        if not tg.is_configured():
            return "Настрой Telethon для отправки голосовых"

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False, dir=config.TEMP_DIR) as f:
            path = Path(f.name)
        try:
            if not synthesize_to_file(text, path):
                return "Не удалось синтезировать голос"
            return tg.send_voice(contact, text, path)
        finally:
            if path.exists():
                try:
                    path.unlink()
                except OSError:
                    pass
    except Exception as exc:
        logger.error("telegram_send_voice: %s", exc)
        return f"Ошибка голосового: {exc}"


# ── Система / медиа ───────────────────────────────────────────────────────────

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
        return f"Яркость: {exc}"


def _tool_volume(params: dict[str, Any]) -> str:
    import pyautogui
    act = str(params.get("action") or "up").lower()
    steps = int(params.get("steps") or 3)
    if act == "mute":
        pyautogui.press("volumemute")
        return "Звук выключен"
    key = "volumeup" if act in ("up", "increase") else "volumedown"
    for _ in range(max(1, steps)):
        pyautogui.press(key)
    return "Громкость изменена"


def _tool_voice_note(params: dict[str, Any]) -> str:
    return reminders.save_voice_note(str(params.get("text") or params.get("note") or ""))


def _tool_set_reminder(params: dict[str, Any]) -> str:
    return reminders.add_reminder(
        str(params.get("text") or ""),
        str(params.get("when") or params.get("time") or ""),
    )


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


def _tool_list_apps(params: dict[str, Any]) -> str:
    if not config.APP_PATHS:
        return "Список приложений пуст — настрой в GUI"
    lines = [f"{k}: {v}" for k, v in sorted(config.APP_PATHS.items())]
    return " | ".join(lines[:15])


def _parse_launch_spec(spec: str) -> list[str]:
    """
    Разбор команды запуска в argv.
    ВАЖНО: shlex.split ломает пути с пробелами (C:\\Program Files\\...),
    поэтому plain .exe пути не токенизируем.
    """
    spec = spec.strip()
    if not spec:
        return []

    # Discord-style: "Update.exe --processStart Discord.exe"
    if " --" in spec:
        idx = spec.find(" --")
        exe = spec[:idx].strip()
        rest = spec[idx:].strip()  # "--processStart Discord.exe"
        try:
            return [exe] + shlex.split(rest, posix=False)
        except ValueError:
            return [exe, rest]

    # Обычный путь (может содержать пробелы) — одним аргументом
    return [spec]


def _launch_executable(spec: str) -> tuple[bool, str]:
    """
    Запуск приложения Windows. Возвращает (успех, детали).
    Поддерживает: полный путь, notepad.exe, discord Update.exe --processStart.
    """
    argv = _parse_launch_spec(spec)
    if not argv:
        return False, "путь пуст"

    exe = argv[0]
    exe_path = Path(exe)

    # Системные команды в PATH (notepad.exe, calc.exe)
    if not exe_path.is_absolute() and "\\" not in exe and "/" not in exe:
        try:
            proc = subprocess.Popen(argv, shell=False)
            logger.info("launched PATH app %s pid=%s", exe, proc.pid)
            return True, f"PID {proc.pid}"
        except FileNotFoundError:
            pass

    if not exe_path.exists():
        return False, f"файл не найден: {exe}"

    # Простой exe без аргументов — os.startfile надёжнее на Windows
    if len(argv) == 1:
        try:
            os.startfile(exe)  # type: ignore[attr-defined]
            logger.info("startfile: %s", exe)
            return True, exe
        except Exception as exc:
            logger.warning("startfile failed %s: %s", exe, exc)

    try:
        cwd = str(exe_path.parent) if exe_path.parent.exists() else None
        proc = subprocess.Popen(argv, cwd=cwd, shell=False)
        logger.info("launched %s pid=%s argv=%s", exe, proc.pid, argv[1:])
        return True, f"PID {proc.pid}"
    except Exception as exc:
        logger.warning("Popen failed %s: %s — trying shell", spec, exc)
        try:
            subprocess.Popen(f'"{argv[0]}"' + (" " + " ".join(argv[1:]) if len(argv) > 1 else ""), shell=True)
            return True, "shell"
        except Exception as exc2:
            logger.error("launch failed %s: %s", spec, exc2)
            return False, str(exc2)


def _tool_open_app(params: dict[str, Any]) -> str:
    raw_name = str(params.get("name") or params.get("app") or "").strip()
    path = params.get("path")

    if path:
        ok, detail = _launch_executable(str(path))
        return f"Открываю {Path(str(path)).name}" if ok else f"Не удалось: {detail}"

    if not raw_name:
        apps = ", ".join(sorted(config.APP_PATHS)[:12]) or "настрой в GUI"
        return f"Приложение не указано. Доступны: {apps}"

    app_path, key = config.resolve_app_path(raw_name)
    if not app_path:
        logger.warning("app not found: %r (normalized=%s)", raw_name, config.normalize_app_name(raw_name))
        return (
            f"Приложение «{raw_name}» не найдено. "
            f"Добавь в GUI → Приложения. Известны: {', '.join(sorted(config.APP_PATHS)[:8])}"
        )

    ok, detail = _launch_executable(app_path)
    if ok:
        return f"Запущено: {key} ({detail})"
    return f"Ошибка запуска {key}: {detail}"


def _tool_launch_game(params: dict[str, Any]) -> str:
    game = str(params.get("game") or params.get("name") or "").strip().lower()
    app_id = str(params.get("app_id") or config.STEAM_GAMES.get(game, ""))
    if not app_id:
        return f"Игра не найдена: {game}"
    try:
        os.startfile(f"steam://run/{app_id}")  # type: ignore[attr-defined]
        return f"Запуск {game or app_id}"
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
        return "VPN bat не найден"
    try:
        if act in ("on", "start"):
            subprocess.Popen(["cmd", "/c", "start", "", bat], cwd=str(Path(bat).parent))
            return "VPN включён"
        if act in ("off", "stop"):
            subprocess.run(["taskkill", "/F", "/IM", "winws.exe"], check=False)
            return "VPN выключен"
        chk = subprocess.run(["tasklist", "/FI", "IMAGENAME eq winws.exe"], capture_output=True, text=True)
        if "winws.exe" in chk.stdout:
            subprocess.run(["taskkill", "/F", "/IM", "winws.exe"], check=False)
            return "VPN выключен"
        subprocess.Popen(["cmd", "/c", "start", "", bat], cwd=str(Path(bat).parent))
        return "VPN включён"
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
    register_tool("list_apps", _tool_list_apps)
    register_tool("launch_game", _tool_launch_game, ["launch_steam_game", "open_game"])
    register_tool("open_url", _tool_open_url)
    register_tool("youtube_search", _tool_youtube_search)
    register_tool("telegram_send_message", _tool_telegram_send_message, ["telegram_message", "telegram_send"])
    register_tool("telegram_read_last", _tool_telegram_read_last, ["telegram_read", "read_telegram"])
    register_tool("telegram_get_unread", _tool_telegram_get_unread, ["telegram_unread"])
    register_tool("telegram_list_dialogs", _tool_telegram_list_dialogs, ["list_telegram_chats"])
    register_tool("telegram_send_voice", _tool_telegram_send_voice)
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


def _parse_action(action: Any) -> tuple[str, dict[str, Any]]:
    """
    Универсальный парсер action: brain.Action dataclass или dict.
    Никогда не вызывает action.get() на dataclass — исправляет AttributeError.
    """
    if action is None:
        return "", {}
    if isinstance(action, dict):
        tool = str(action.get("tool") or action.get("name") or "").strip().lower()
        params = action.get("params") or action.get("arguments") or {}
    else:
        tool = str(getattr(action, "tool", None) or getattr(action, "name", None) or "").strip().lower()
        params = getattr(action, "params", None)
        if params is None:
            params = getattr(action, "arguments", None)
        if params is None:
            params = {}

    if not isinstance(params, dict):
        params = {"value": params}
    return tool, params


class ToolExecutor:
    def __init__(self, registry: dict[str, ToolHandler] | None = None) -> None:
        self.registry = registry or TOOL_REGISTRY

    def execute(self, tool: str, params: dict[str, Any] | None = None) -> str:
        tool = (tool or "").strip().lower()
        fn = self.registry.get(tool)
        if not fn:
            return f"Неизвестная команда: {tool}"
        return _safe_call(fn, params or {}, tool)

    def execute_all(self, actions: list) -> list[str]:
        results: list[str] = []
        for i, action in enumerate(actions):
            try:
                tool, params = _parse_action(action)
                if not tool:
                    results.append(f"action[{i}]: пустой tool")
                    continue
                logger.info("execute tool=%s params=%s", tool, params)
                results.append(self.execute(tool, params))
            except Exception as exc:
                logger.error("execute_all[%d]: %s", i, exc, exc_info=True)
                results.append(f"Ошибка action[{i}]: {exc}")
        return results

    def list_tools(self) -> list[str]:
        return sorted(set(TOOL_REGISTRY))