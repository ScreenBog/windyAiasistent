"""
Автообнаружение установленных приложений Windows 11.

Источники (по приоритету при merge):
  - KNOWN_APPS — проверенные пути + glob
  - Реестр App Paths
  - Start Menu + Desktop (.lnk)
  - SCAN_ALIASES — русские/разговорные имена → canonical key
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

import bootstrap  # noqa: F401

logger = logging.getLogger(__name__)

# Популярные приложения → типичные пути Windows 11
KNOWN_APPS: dict[str, list[str]] = {
    "chrome": [
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
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
        os.path.expandvars(r"%LOCALAPPDATA%\Mozilla Firefox\firefox.exe"),
    ],
    "telegram": [
        os.path.expandvars(r"%APPDATA%\Telegram Desktop\Telegram.exe"),
        r"C:\Program Files\WindowsApps\TelegramMessengerLLP.TelegramDesktop_*\Telegram.exe",
        r"C:\Program Files\Telegram Desktop\Telegram.exe",
    ],
    "discord": [
        os.path.expandvars(r"%LOCALAPPDATA%\Discord\Update.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Discord\app-*\Discord.exe"),
    ],
    "steam": [
        os.path.expandvars(r"%ProgramFiles(x86)%\Steam\steam.exe"),
        r"C:\Program Files (x86)\Steam\steam.exe",
        r"C:\Program Files\Steam\steam.exe",
        os.path.expandvars(r"%ProgramFiles%\Steam\steam.exe"),
    ],
    "spotify": [
        os.path.expandvars(r"%APPDATA%\Spotify\Spotify.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WindowsApps\Spotify.exe"),
    ],
    "vscode": [
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe"),
        r"C:\Program Files\Microsoft VS Code\Code.exe",
        r"C:\Program Files (x86)\Microsoft VS Code\Code.exe",
    ],
    "code": [
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe"),
        r"C:\Program Files\Microsoft VS Code\Code.exe",
    ],
    "notepad": ["notepad.exe"],
    "calc": ["calc.exe"],
    "explorer": ["explorer.exe"],
    "mspaint": ["mspaint.exe"],
    "snippingtool": [
        r"C:\Program Files\WindowsApps\Microsoft.ScreenSketch_*\SnippingTool\SnippingTool.exe",
        "snippingtool.exe",
    ],
    "powershell": [
        r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
    ],
    "cmd": [r"C:\Windows\System32\cmd.exe"],
    "zoom": [
        os.path.expandvars(r"%APPDATA%\Zoom\bin\Zoom.exe"),
        r"C:\Program Files\Zoom\bin\Zoom.exe",
    ],
    "slack": [
        os.path.expandvars(r"%LOCALAPPDATA%\slack\slack.exe"),
        r"C:\Program Files\Slack\slack.exe",
    ],
}

# Имена из .lnk / реестра → canonical key
SCAN_ALIASES: dict[str, str] = {
    "google chrome": "chrome",
    "chrome": "chrome",
    "microsoft edge": "edge",
    "edge": "edge",
    "mozilla firefox": "firefox",
    "firefox": "firefox",
    "telegram": "telegram",
    "telegram desktop": "telegram",
    "discord": "discord",
    "steam": "steam",
    "spotify": "spotify",
    "visual studio code": "vscode",
    "vs code": "vscode",
    "code": "code",
    "блокнот": "notepad",
    "калькулятор": "calc",
    "проводник": "explorer",
    "zoom": "zoom",
    "slack": "slack",
}

APP_LABELS: dict[str, str] = {
    "chrome": "Google Chrome",
    "edge": "Microsoft Edge",
    "firefox": "Mozilla Firefox",
    "telegram": "Telegram",
    "discord": "Discord",
    "steam": "Steam",
    "spotify": "Spotify",
    "vscode": "VS Code",
    "code": "VS Code",
    "notepad": "Блокнот",
    "calc": "Калькулятор",
    "explorer": "Проводник",
    "powershell": "PowerShell",
    "cmd": "Командная строка",
    "zoom": "Zoom",
    "slack": "Slack",
}


def resolve_canonical_key(name: str) -> str:
    """Нормализовать имя к canonical key сканера."""
    key = (name or "").strip().lower()
    return SCAN_ALIASES.get(key, key)


def lookup_in_catalog(name: str, catalog: dict[str, str]) -> tuple[str, str] | None:
    """Найти exe по alias в уже собранном каталоге."""
    key = resolve_canonical_key(name)
    if key in catalog:
        return catalog[key], key
    for alias, canon in SCAN_ALIASES.items():
        if alias in key or key in alias:
            if canon in catalog:
                return catalog[canon], canon
    return None


def _resolve_path_pattern(raw: str) -> str | None:
    """Разрешить путь с glob и env-переменными."""
    raw = os.path.expandvars(raw.strip())
    if "*" in raw:
        parent_glob = raw.rsplit("\\", 1)[0] if "\\" in raw else raw
        name_glob = raw.rsplit("\\", 1)[-1]
        base = Path(parent_glob.split("*")[0])
        search_root = base.parent if base.name else base
        if not search_root.exists():
            search_root = Path(os.path.expandvars("%LOCALAPPDATA%"))
        try:
            for candidate in sorted(search_root.rglob(name_glob), reverse=True):
                if candidate.is_file():
                    return str(candidate)
        except OSError:
            pass
        # Discord app-* special case
        if "Discord" in raw and "app-*" in raw:
            discord_dir = Path(os.path.expandvars(r"%LOCALAPPDATA%\Discord"))
            if discord_dir.exists():
                for app_dir in sorted(discord_dir.glob("app-*"), reverse=True):
                    exe = app_dir / "Discord.exe"
                    if exe.exists():
                        return str(exe)
        return None
    p = Path(raw)
    return str(p) if p.exists() else None


def _find_discord() -> str | None:
    """Discord: прямой exe или Update.exe --processStart."""
    local = Path(os.path.expandvars(r"%LOCALAPPDATA%\Discord"))
    if local.exists():
        for app_dir in sorted(local.glob("app-*"), reverse=True):
            exe = app_dir / "Discord.exe"
            if exe.exists():
                return str(exe)
        update = local / "Update.exe"
        if update.exists():
            return f"{update} --processStart Discord.exe"
    return None


def _scan_known() -> dict[str, str]:
    found: dict[str, str] = {}
    for name, paths in KNOWN_APPS.items():
        for raw in paths:
            resolved = _resolve_path_pattern(raw)
            if resolved:
                found[name] = resolved
                break
    discord = _find_discord()
    if discord:
        found["discord"] = discord
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
                            canon = SCAN_ALIASES.get(key, key)
                            if canon not in found:
                                found[canon] = str(path)
                    except OSError:
                        break
        except OSError:
            continue
    return found


def _resolve_lnk_batch(lnk_paths: list[str]) -> dict[str, str]:
    """Пакетное разрешение .lnk → exe через один вызов PowerShell."""
    found: dict[str, str] = {}
    if not lnk_paths:
        return found
    # Ограничим batch для скорости
    batch = lnk_paths[:120]
    joined = ",".join(f"'{p.replace(chr(39), chr(39)*2)}'" for p in batch)
    ps = (
        f"$lnks = @({joined}); "
        "$sh = New-Object -ComObject WScript.Shell; "
        "foreach ($l in $lnks) { "
        "  try { "
        "    $n = [System.IO.Path]::GetFileNameWithoutExtension($l).ToLower(); "
        "    $t = $sh.CreateShortcut($l).TargetPath; "
        "    if ($t -and (Test-Path $t) -and $t -like '*.exe') { Write-Output ($n + '|' + $t) } "
        "  } catch {} "
        "}"
    )
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=25,
            encoding="utf-8", errors="replace",
        )
        for line in (r.stdout or "").splitlines():
            if "|" not in line:
                continue
            name, target = line.strip().split("|", 1)
            name = name.strip().lower()
            canon = SCAN_ALIASES.get(name, name)
            if len(canon) < 2:
                continue
            if canon not in found:
                found[canon] = target.strip()
    except Exception as exc:
        logger.debug("lnk batch: %s", exc)
    return found


def _collect_lnk_paths() -> list[str]:
    """Собрать пути .lnk из Start Menu и Desktop."""
    roots = [
        os.path.expandvars(r"%ProgramData%\Microsoft\Windows\Start Menu\Programs"),
        os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs"),
        os.path.expandvars(r"%USERPROFILE%\Desktop"),
        os.path.expandvars(r"%PUBLIC%\Desktop"),
    ]
    lnks: list[str] = []
    for root in roots:
        rp = Path(root)
        if not rp.exists():
            continue
        try:
            for p in rp.rglob("*.lnk"):
                lnks.append(str(p))
        except OSError:
            continue
    return lnks


def _scan_shortcuts() -> dict[str, str]:
    """Start Menu + Desktop ярлыки."""
    return _resolve_lnk_batch(_collect_lnk_paths())


def scan_installed_apps(*, include_start_menu: bool = True) -> dict[str, str]:
    """
    Полное сканирование. Возвращает {canonical_alias: path}.
    Приоритет при merge: known > shortcuts > registry.
    """
    merged: dict[str, str] = {}
    merged.update(_scan_registry())
    if include_start_menu:
        merged.update(_scan_shortcuts())
    merged.update(_scan_known())

    # Discord: всегда нормализуем Update.exe
    discord = merged.get("discord", "")
    if discord and "Update.exe" in discord and "--processStart" not in discord:
        merged["discord"] = discord + " --processStart Discord.exe"

    # Дублируем code → vscode если нужно
    if "vscode" in merged and "code" not in merged:
        merged["code"] = merged["vscode"]

    logger.info("app scan: found %d applications", len(merged))
    return dict(sorted(merged.items()))


def merge_with_manual(scanned: dict[str, str], manual: dict[str, str]) -> dict[str, str]:
    """Объединить найденные и ручные (ручные имеют приоритет)."""
    out = dict(scanned)
    for k, v in manual.items():
        out[k.strip().lower()] = v.strip()
    return dict(sorted(out.items()))


def label_for(name: str) -> str:
    key = resolve_canonical_key(name)
    return APP_LABELS.get(key, name.replace("_", " ").title())