"""
Автообнаружение установленных приложений Windows.

Источники:
  - Известные пути (Chrome, Telegram, Steam, Discord…)
  - Реестр App Paths
  - Start Menu (.lnk через PowerShell)
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

import bootstrap  # noqa: F401

logger = logging.getLogger(__name__)

# Популярные приложения → типичные пути (проверяются по порядку)
KNOWN_APPS: dict[str, list[str]] = {
    "chrome": [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ],
    "edge": [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ],
    "firefox": [
        r"C:\Program Files\Mozilla Firefox\firefox.exe",
        r"C:\Program Files (x86)\Mozilla Firefox\firefox.exe",
    ],
    "telegram": [
        os.path.expandvars(r"%APPDATA%\Telegram Desktop\Telegram.exe"),
    ],
    "discord": [
        os.path.expandvars(r"%LOCALAPPDATA%\Discord\Update.exe"),
    ],
    "steam": [
        r"C:\Program Files (x86)\Steam\steam.exe",
        r"C:\Program Files\Steam\steam.exe",
    ],
    "spotify": [
        os.path.expandvars(r"%APPDATA%\Spotify\Spotify.exe"),
    ],
    "vscode": [
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe"),
        r"C:\Program Files\Microsoft VS Code\Code.exe",
    ],
    "notepad": ["notepad.exe"],
    "calc": ["calc.exe"],
    "explorer": ["explorer.exe"],
    "powershell": [
        r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
    ],
    "cmd": [r"C:\Windows\System32\cmd.exe"],
}

# Человекочитаемые названия
APP_LABELS: dict[str, str] = {
    "chrome": "Google Chrome",
    "edge": "Microsoft Edge",
    "firefox": "Mozilla Firefox",
    "telegram": "Telegram",
    "discord": "Discord",
    "steam": "Steam",
    "spotify": "Spotify",
    "vscode": "VS Code",
    "notepad": "Блокнот",
    "calc": "Калькулятор",
    "explorer": "Проводник",
    "powershell": "PowerShell",
    "cmd": "Командная строка",
}


def _resolve_glob(path: str) -> str | None:
    if "*" in path:
        parent = Path(path).parent
        if parent.exists():
            matches = sorted(parent.parent.glob(Path(path).name), reverse=True)
            for m in matches:
                if m.is_file():
                    return str(m)
        return None
    p = Path(path)
    return str(p) if p.exists() else None


def _scan_known() -> dict[str, str]:
    found: dict[str, str] = {}
    for name, paths in KNOWN_APPS.items():
        for raw in paths:
            resolved = _resolve_glob(os.path.expandvars(raw))
            if resolved:
                found[name] = resolved
                break
    return found


def _scan_registry() -> dict[str, str]:
    """HKLM/HKCU …\\App Paths\\*.exe"""
    found: dict[str, str] = {}
    try:
        import winreg
    except ImportError:
        return found

    key_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hive, key_path) as root:
                idx = 0
                while True:
                    try:
                        sub = winreg.EnumKey(root, idx)
                        idx += 1
                        with winreg.OpenKey(root, sub) as sk:
                            path, _ = winreg.QueryValueEx(sk, None)
                            if not path or not Path(str(path)).exists():
                                continue
                            key = sub.replace(".exe", "").lower()
                            if key not in found:
                                found[key] = str(path)
                    except OSError:
                        break
        except OSError:
            continue
    return found


def _scan_start_menu() -> dict[str, str]:
    """Ярлыки Start Menu → exe через PowerShell (без pywin32)."""
    found: dict[str, str] = {}
    ps = (
        "$paths = @("
        "'$env:ProgramData\\Microsoft\\Windows\\Start Menu\\Programs',"
        "'$env:APPDATA\\Microsoft\\Windows\\Start Menu\\Programs'"
        "); "
        "foreach ($p in $paths) { "
        "  Get-ChildItem -Path $p -Filter *.lnk -Recurse -ErrorAction SilentlyContinue | "
        "  ForEach-Object { $_.BaseName.ToLower() + '|' + $_.FullName } "
        "}"
    )
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=20,
        )
        for line in (r.stdout or "").splitlines():
            if "|" not in line:
                continue
            name, lnk = line.strip().split("|", 1)
            name = name.strip().lower()
            if len(name) < 2 or name in found:
                continue
            # Резолвим lnk в exe
            try:
                rr = subprocess.run(
                    [
                        "powershell", "-NoProfile", "-Command",
                        f"(New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}').TargetPath",
                    ],
                    capture_output=True, text=True, timeout=5,
                )
                target = (rr.stdout or "").strip()
                if target and Path(target).exists() and target.lower().endswith(".exe"):
                    found[name] = target
            except Exception:
                pass
    except Exception as exc:
        logger.debug("start menu scan: %s", exc)
    return found


def scan_installed_apps(*, include_start_menu: bool = True) -> dict[str, str]:
    """
    Полное сканирование. Возвращает {alias: path}.
    Приоритет: known > registry > start menu.
    """
    merged: dict[str, str] = {}
    merged.update(_scan_registry())
    if include_start_menu:
        merged.update(_scan_start_menu())
    merged.update(_scan_known())  # known перезаписывает с лучшими алиасами

    # Нормализация discord: Update.exe с аргументом
    if "discord" in merged and "Update.exe" in merged["discord"]:
        merged["discord"] = merged["discord"] + " --processStart Discord.exe"

    logger.info("app scan: found %d applications", len(merged))
    return dict(sorted(merged.items()))


def merge_with_manual(scanned: dict[str, str], manual: dict[str, str]) -> dict[str, str]:
    """Объединить найденные и ручные (ручные имеют приоритет)."""
    out = dict(scanned)
    for k, v in manual.items():
        out[k.strip().lower()] = v.strip()
    return dict(sorted(out.items()))


def label_for(name: str) -> str:
    return APP_LABELS.get(name.lower(), name.replace("_", " ").title())