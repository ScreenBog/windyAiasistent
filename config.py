"""
Настройки Windy AI Assistant — settings.json + hot-reload.

Секции:
  - Аудио / VAD (continuous listening)
  - Whisper STT (cuda→cpu, float16→int8 fallback)
  - Ollama LLM
  - Telegram (Telethon)
  - GUI
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import bootstrap  # noqa: F401
from bootstrap import PROJECT_DIR as BASE_DIR

logger = logging.getLogger(__name__)

# ── Пути ──────────────────────────────────────────────────────────────────────
PROMPTS_DIR = BASE_DIR / "prompts"
PLUGINS_DIR = BASE_DIR / "plugins"
DATA_DIR = BASE_DIR / "data"
SYSTEM_PROMPT_PATH = PROMPTS_DIR / "system.txt"
SETTINGS_PATH = BASE_DIR / "settings.json"
TEMP_DIR = BASE_DIR / "temp"
LOG_DIR = BASE_DIR / "logs"
MODELFILE_PATH = BASE_DIR / "Modelfile"
TELEGRAM_SESSION_PATH = DATA_DIR / "windy_telegram"

for _d in (TEMP_DIR, LOG_DIR, PLUGINS_DIR, DATA_DIR, PROMPTS_DIR):
    _d.mkdir(exist_ok=True)

# ── Аудио (константы) ─────────────────────────────────────────────────────────
SAMPLE_RATE = 16_000
CHANNELS = 1
DTYPE = "float32"

# ── Тексты ассистента ─────────────────────────────────────────────────────────
ASSISTANT_NAME = "Винди"
STARTUP_GREETING = "Винди на связи. Скажи «Эй Винди», когда понадоблюсь."
ERROR_GENERIC = "Произошла ошибка. Попробуй ещё раз."
NO_SPEECH = "Я тебя не расслышал."
CONFIRM_WAKE = "Слушаю."

# ── Wake-word polling ─────────────────────────────────────────────────────────
WAKE_CHUNK_SEC = 3.0
WAKE_POLL_INTERVAL = 0.2

# ── VAD defaults (continuous listening) ─────────────────────────────────────
# Чувствительность 0.0–1.0: выше → легче начать запись, дольше ждёт паузу.
VAD_SENSITIVITY = 0.60
VAD_CHUNK_MS = 60                    # меньший чанк → точнее границы речи
VAD_RMS_SMOOTH_WINDOW = 8            # сглаживание RMS (окно чанков)
VAD_ENERGY_WEIGHT = 0.30             # вес energy в комбинированном уровне
VAD_PEAK_WEIGHT = 0.15               # вес пика — ловит тихие согласные
VAD_NOISE_CALIBRATION_SEC = 0.65     # калибровка фонового шума перед записью
VAD_NOISE_MULT_ON = 2.8              # порог старта = noise × mult
VAD_NOISE_MULT_OFF = 1.45            # порог тишины (гистерезис)
VAD_SPEECH_THRESHOLD = 0.008
VAD_SILENCE_THRESHOLD = 0.004
VAD_ATTACK_SEC = 0.18                # N чанков подряд выше порога → старт речи
VAD_RELEASE_SEC = 3.2                # тишина для завершения записи (основной)
VAD_SILENCE_SEC = 3.2                # alias для GUI / settings.json
VAD_HANGOVER_SEC = 1.8               # после речи — терпим короткие паузы
VAD_MAX_RECORD_SEC = 60.0            # макс. длина одной команды
VAD_MIN_SPEECH_SEC = 0.35
VAD_WAIT_SPEECH_SEC = 18.0           # ждём начала речи после wake
VAD_PRE_ROLL_SEC = 1.4               # буфер до детекта речи (не обрезает начало)
VAD_END_PADDING_SEC = 0.45           # не обрезать хвост речи
VAD_TRIM_TRAILING = True             # мягкая обрезка хвостовой тишины
VAD_LONG_SPEECH_BONUS_SEC = 0.8      # длинные фразы → больше терпимости к паузам
VAD_ADAPTIVE_NOISE_ALPHA = 0.018     # медленная подстройка шума во время записи
VAD_TTS_OVERLAP_SEC = 2.0            # микрофон открыт во время TTS «Слушаю»
VAD_MIC_WARMUP_SEC = 0.12            # пауза после открытия потока
VAD_DEBUG_LOG = False                # логировать уровень громкости (отладка)
VAD_DEBUG_LOG_INTERVAL_SEC = 0.45
POST_TTS_DELAY_SEC = 0.0             # устарело: микрофон открывается параллельно TTS
MIC_DEVICE_ID: int | None = None     # None = системный микрофон по умолчанию

# ── TTS ───────────────────────────────────────────────────────────────────────
TTS_USE_SAPI_FALLBACK = True

# ── Ollama ────────────────────────────────────────────────────────────────────
OLLAMA_TIMEOUT = 90
OLLAMA_JSON_RETRIES = 2

# ── Mutable (загружаются из settings.json) ───────────────────────────────────
WAKE_WORD = "винди"
WAKE_WORD_ALIASES: tuple[str, ...] = ()
WHISPER_MODEL = "small"
WHISPER_DEVICE = "cpu"
WHISPER_COMPUTE_TYPE = "int8"
WHISPER_LANGUAGE = "ru"
OLLAMA_HOST = "http://127.0.0.1:11434"
OLLAMA_MODEL = "qwen2.5:3b-windy"
OLLAMA_MODEL_FAST = ""              # пусто = OLLAMA_MODEL (быстрые команды)
OLLAMA_MODEL_SLOW = ""              # пусто = OLLAMA_MODEL (сложные сценарии)
HYBRID_MODELS_ENABLED = True
SIMPLE_COMMAND_MAX_WORDS = 6
LEARNING_ENABLED = True
LEARNING_AUTO_SCAN = True
LEARNING_MAX_ENTRIES = 200
LEARNING_MAX_CORRECTIONS = 50
OLLAMA_TEMPERATURE = 0.25
OLLAMA_NUM_CTX = 4096
OLLAMA_NUM_GPU = -1
OLLAMA_TOP_P = 0.9
OLLAMA_REPEAT_PENALTY = 1.1
TTS_VOICE = "ru-RU-DmitryNeural"
TTS_RATE = "+0%"
TTS_VOLUME = "+0%"
APP_PATHS: dict[str, str] = {}
APP_PATHS_MANUAL: dict[str, str] = {}  # добавленные вручную (не перезаписываются сканом)
STEAM_GAMES: dict[str, str] = {}
VPN_TOGGLE_BAT = ""
TELEGRAM_API_ID = 0
TELEGRAM_API_HASH = ""
TELEGRAM_PHONE = ""                   # +79991234567 для авторизации Telethon
TELEGRAM_BOT_TOKEN = ""
TELEGRAM_DEFAULT_CHAT_ID = ""
TELEGRAM_DEFAULT_CONTACT = ""         # имя контакта по умолчанию для read/send
TELEGRAM_CHATS: dict[str, str] = {}   # alias → chat_id
TELEGRAM_READ_DEFAULT_COUNT = 5
TELEGRAM_DIALOGS_LIMIT = 60
FILE_SEARCH_ROOTS: list[str] = []
LOG_LEVEL = "INFO"
PLUGINS_ENABLED = True
GUI_THEME = "dark"
GUI_ACCENT = "#6366f1"
GUI_ACCENT_HOVER = "#4f46e5"
GUI_VERSION = "v9.1"
GUI_SUCCESS = "#22c55e"
GUI_WARNING = "#f59e0b"
GUI_DANGER = "#ef4444"
WHISPER_MODELS = ("tiny", "base", "small", "medium", "large-v3")

WHISPER_BEAM_SIZE = 5
WHISPER_BEST_OF = 3
WHISPER_NO_SPEECH_THRESHOLD = 0.45
WHISPER_COMPRESSION_RATIO_THRESHOLD = 2.4
WHISPER_LOG_PROB_THRESHOLD = -1.0
TTS_EDGE_RETRIES = 3
TTS_EDGE_TIMEOUT_SEC = 75
TTS_SAPI_RATE = 0                    # -10..10 (проценты), 0 = по умолчанию

# Русские/разговорные имена → alias в APP_PATHS
APP_ALIASES: dict[str, str] = {
    "телеграм": "telegram",
    "телеграмм": "telegram",
    "телега": "telegram",
    "хром": "chrome",
    "гугл хром": "chrome",
    "google chrome": "chrome",
    "гугол хром": "chrome",
    "дискорд": "discord",
    "стим": "steam",
    "блокнот": "notepad",
    "калькулятор": "calc",
    "проводник": "explorer",
    "эдж": "edge",
    "microsoft edge": "edge",
    "спотифай": "spotify",
    "vscode": "code",
    "vs code": "code",
    "вс код": "code",
}

# Популярные сайты → URL (для open_browser)
BROWSER_SITES: dict[str, str] = {
    "youtube": "https://www.youtube.com",
    "ютуб": "https://www.youtube.com",
    "ютюб": "https://www.youtube.com",
    "vk": "https://vk.com/im",
    "вк": "https://vk.com/im",
    "вконтакте": "https://vk.com/im",
    "вконтакт": "https://vk.com/im",
    "grok": "https://grok.com",
    "грок": "https://grok.com",
    "grok.x.ai": "https://grok.com",
    "chatgpt": "https://chatgpt.com",
    "чатгпт": "https://chatgpt.com",
    "openai": "https://chatgpt.com",
    "google": "https://www.google.com",
    "гугл": "https://www.google.com",
    "гугол": "https://www.google.com",
    "steam": "https://store.steampowered.com",
    "стим": "https://store.steampowered.com",
    "twitch": "https://www.twitch.tv",
    "твич": "https://www.twitch.tv",
    "github": "https://github.com",
    "гитхаб": "https://github.com",
    "reddit": "https://www.reddit.com",
    "реддит": "https://www.reddit.com",
    "twitter": "https://x.com",
    "твиттер": "https://x.com",
    "x": "https://x.com",
    "instagram": "https://www.instagram.com",
    "инстаграм": "https://www.instagram.com",
    "discord": "https://discord.com/app",
    "дискорд": "https://discord.com/app",
    "telegram": "https://web.telegram.org",
    "телеграм": "https://web.telegram.org",
    "телеграмм": "https://web.telegram.org",
    "yandex": "https://ya.ru",
    "яндекс": "https://ya.ru",
    "wikipedia": "https://ru.wikipedia.org",
    "википедия": "https://ru.wikipedia.org",
    "netflix": "https://www.netflix.com",
    "нетфликс": "https://www.netflix.com",
    "spotify": "https://open.spotify.com",
    "спотифай": "https://open.spotify.com",
    "habr": "https://habr.com",
    "хабр": "https://habr.com",
}

# Сайты, которые по умолчанию открываем в браузере (не как .exe)
BROWSER_APP_ALIASES: frozenset[str] = frozenset({
    "discord", "дискорд", "telegram", "телеграм", "телеграмм", "телега",
    "steam", "стим", "spotify", "спотифай",
})

BROWSER_PREFERRED: frozenset[str] = frozenset(
    k for k in BROWSER_SITES if k not in BROWSER_APP_ALIASES
)

# Быстрый доступ в GUI (sidebar)
BROWSER_QUICK_SITES: tuple[str, ...] = (
    "youtube", "вк", "грок", "google", "steam", "twitch", "github", "chatgpt", "яндекс",
)

GOOGLE_SEARCH_URL = "https://www.google.com/search?q="
YANDEX_SEARCH_URL = "https://ya.ru/search/?text="

_BROWSER_PREFIXES = (
    "открой", "открыть", "откройте", "зайди на", "зайди в", "перейди на", "перейди в",
    "open", "go to", "launch", "включи сайт", "запусти сайт",
)

# Типы JSON-макросов (генерирует LLM)
MACRO_TYPES: tuple[str, ...] = (
    "LAUNCH_APP",
    "FOCUS",
    "SHELL_CMD",
    "KEY",
    "TYPE",
    "SLEEP",
    "OPEN_BROWSER",
    "TELEGRAM_SEND",
    "TELEGRAM_READ",
    "OPEN_VK",
)

# Запрещённые фрагменты для SHELL_CMD (подстроки, lower case)
FORBIDDEN_SHELL_PATTERNS: tuple[str, ...] = (
    "format ",
    "format c",
    "del /f",
    "del /s",
    "rd /s",
    "rmdir /s",
    "remove-item -recurse",
    "remove-item -force",
    "shutdown",
    "restart-computer",
    "stop-computer",
    "reg delete",
    "reg add",
    "diskpart",
    "cipher /w",
    "bcdedit",
    "takeown",
    "icacls",
    "net user",
    "net localgroup",
    "wmic process call terminate",
    ":(){",  # fork bomb
    "rm -rf",
    "mkfs",
    "dd if=",
    "chmod 777",
    "> nul &",
    "invoke-expression",
    "iex ",
    "downloadstring",
    "curl |",
    "wget |",
    "start-process powershell -verb runas",
)

# Полностью запрещённые команды (точное совпадение после strip lower)
FORBIDDEN_COMMANDS: frozenset[str] = frozenset({
    "format c:",
    "format c: /y",
    "shutdown /s /t 0",
    "shutdown -s -t 0",
})

_SEARCH_PREFIXES: tuple[tuple[str, str], ...] = (
    ("поищи в гугле", "google"),
    ("поиск в гугле", "google"),
    ("найди в гугле", "google"),
    ("поищи в google", "google"),
    ("найди в google", "google"),
    ("поищи в яндексе", "yandex"),
    ("найди в яндексе", "yandex"),
    ("поищи", "google"),
    ("найди", "google"),
    ("загугли", "google"),
    ("google", "google"),
    ("гугл", "google"),
)

# Дефолтные пути Windows 11 (если settings.json пуст / скан не нашёл)
DEFAULT_APP_PATHS: dict[str, str] = {
    "chrome": r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "edge": r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "firefox": r"C:\Program Files\Mozilla Firefox\firefox.exe",
    "telegram": os.path.expandvars(r"%APPDATA%\Telegram Desktop\Telegram.exe"),
    "discord": os.path.expandvars(r"%LOCALAPPDATA%\Discord\Update.exe --processStart Discord.exe"),
    "steam": r"C:\Program Files (x86)\Steam\steam.exe",
    "spotify": os.path.expandvars(r"%APPDATA%\Spotify\Spotify.exe"),
    "vscode": os.path.expandvars(r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe"),
    "notepad": "notepad.exe",
    "explorer": "explorer.exe",
    "calc": "calc.exe",
    "mspaint": "mspaint.exe",
    "snippingtool": "snippingtool.exe",
}

# Приоритет при автозаполнении APP_PATHS
_PRIORITY_APPS: tuple[str, ...] = (
    "chrome", "edge", "firefox", "telegram", "discord", "steam",
    "spotify", "vscode", "notepad", "calc", "explorer",
)

_scanned_cache: dict[str, str] | None = None


def normalize_browser_query(query: str) -> str:
    """Убрать «открой» и лишние слова из запроса браузера."""
    import re
    q = (query or "").strip().lower()
    q = re.sub(r"[^\w\sа-яё.\-/@:]", " ", q, flags=re.I)
    q = re.sub(r"\s+", " ", q).strip()
    for prefix in _BROWSER_PREFIXES:
        if q.startswith(prefix + " "):
            q = q[len(prefix) + 1 :].strip()
    for tail in (" в браузере", " сайт", " страницу"):
        if q.endswith(tail):
            q = q[: -len(tail)].strip()
    return q


def resolve_browser_target(query: str) -> tuple[str, str, str]:
    """
    Разбор запроса open_browser.
    Возвращает (kind, target, label):
      kind = url | google | yandex | domain
    """
    import re
    raw = (query or "").strip()
    if not raw:
        return "empty", "", ""

    low = raw.lower().strip()
    # Прямой URL
    if low.startswith(("http://", "https://")) or re.match(r"^[\w.-]+\.[a-z]{2,}(/|$)", low):
        url = raw if "://" in raw else f"https://{raw}"
        return "url", url, url

    q = normalize_browser_query(raw)
    if not q:
        return "empty", "", ""

    # Поисковые запросы
    for prefix, engine in _SEARCH_PREFIXES:
        if q.startswith(prefix + " "):
            term = q[len(prefix) + 1 :].strip()
            if term:
                if engine == "yandex":
                    return "yandex", term, f"Яндекс: {term}"
                return "google", term, f"Google: {term}"
        if q == prefix:
            return "url", BROWSER_SITES.get("google", GOOGLE_SEARCH_URL), "Google"

    # Известный сайт
    if q in BROWSER_SITES:
        return "url", BROWSER_SITES[q], q

    # Частичное совпадение (youtube music → youtube)
    for key, url in sorted(BROWSER_SITES.items(), key=lambda x: -len(x[0])):
        if q == key or q.startswith(key + " ") or key in q.split():
            return "url", url, key

    # Похоже на домен
    if "." in q and " " not in q:
        return "url", f"https://{q}", q

    # Fallback — Google-поиск
    return "google", q, f"поиск: {q}"


def should_prefer_browser(name: str) -> bool:
    """True если запрос лучше открыть в браузере, а не как приложение."""
    key = normalize_browser_query(name)
    if not key:
        return False
    return key in BROWSER_PREFERRED


def is_shell_command_allowed(command: str) -> tuple[bool, str]:
    """Проверка безопасности SHELL_CMD перед выполнением."""
    cmd = (command or "").strip()
    if not cmd:
        return False, "пустая команда"
    low = cmd.lower()
    if low in FORBIDDEN_COMMANDS:
        return False, "команда в чёрном списке"
    for pattern in FORBIDDEN_SHELL_PATTERNS:
        if pattern in low:
            return False, f"запрещённый фрагмент: {pattern.strip()}"
    return True, ""


def resolve_ollama_model(*, complex_task: bool) -> str:
    """Гибрид: быстрая модель для простых команд, медленная для сложных."""
    if not HYBRID_MODELS_ENABLED:
        return OLLAMA_MODEL
    if complex_task:
        return OLLAMA_MODEL_SLOW or OLLAMA_MODEL
    return OLLAMA_MODEL_FAST or OLLAMA_MODEL


def is_ambiguous_app_site(name: str) -> bool:
    """Сайт, у которого есть и веб-версия, и десктоп-приложение (steam, discord…)."""
    key = normalize_browser_query(name)
    app_key = normalize_app_name(name)
    return key in BROWSER_APP_ALIASES or app_key in BROWSER_APP_ALIASES


def normalize_app_name(name: str) -> str:
    """Нормализация имени приложения (русский → alias)."""
    import re
    n = (name or "").strip().lower()
    n = re.sub(r"[^\w\sа-яё\-]", " ", n, flags=re.I)
    n = re.sub(r"\s+", " ", n).strip()
    for prefix in ("открой", "запусти", "включи", "open", "start"):
        if n.startswith(prefix + " "):
            n = n[len(prefix) + 1 :].strip()
    return APP_ALIASES.get(n, n)


def _get_scanned_apps(*, refresh: bool = False) -> dict[str, str]:
    global _scanned_cache
    if _scanned_cache is None or refresh:
        try:
            import app_scanner
            _scanned_cache = app_scanner.scan_installed_apps(include_start_menu=True)
        except Exception as exc:
            logger.warning("app scan cache failed: %s", exc)
            _scanned_cache = {}
    return _scanned_cache


def invalidate_app_cache() -> None:
    global _scanned_cache
    _scanned_cache = None


def get_effective_app_paths() -> dict[str, str]:
    """
    Полный каталог приложений для LAUNCH_APP.
    Приоритет: ручные → settings → скан → defaults.
    """
    merged: dict[str, str] = {}
    merged.update(DEFAULT_APP_PATHS)
    merged.update(_get_scanned_apps())
    merged.update(APP_PATHS)
    merged.update(APP_PATHS_MANUAL)
    return {k: v for k, v in merged.items() if v and _path_exists(v)}


def resolve_app_path(name: str) -> tuple[str | None, str, str]:
    """
    Найти путь к приложению для LAUNCH_APP.
    Порядок: manual → APP_PATHS → scan (aliases) → defaults → fuzzy.
    Возвращает (path, canonical_key, source).
    """
    key = normalize_app_name(name)
    if not key:
        return None, "", ""

    try:
        import app_scanner
        canon = app_scanner.resolve_canonical_key(key)
        if canon:
            key = canon
    except Exception:
        pass

    # 1) Ручные пути (высший приоритет)
    if key in APP_PATHS_MANUAL and _path_exists(APP_PATHS_MANUAL[key]):
        return APP_PATHS_MANUAL[key], key, "manual"

    # 2) settings.json
    if key in APP_PATHS and _path_exists(APP_PATHS[key]):
        return APP_PATHS[key], key, "settings"

    # 3) Автоскан
    scanned = _get_scanned_apps()
    if key in scanned and _path_exists(scanned[key]):
        return scanned[key], key, "scan"

    try:
        import app_scanner
        hit = app_scanner.lookup_in_catalog(key, scanned)
        if hit:
            path, alias = hit
            return path, alias, "scan-alias"
    except Exception:
        pass

    for k, v in scanned.items():
        if key in k or k in key:
            if _path_exists(v):
                return v, k, "scan-fuzzy"

    # 4) Defaults
    if key in DEFAULT_APP_PATHS and _path_exists(DEFAULT_APP_PATHS[key]):
        logger.info("app %s from defaults: %s", key, DEFAULT_APP_PATHS[key])
        return DEFAULT_APP_PATHS[key], key, "default"

    # 5) Fuzzy в APP_PATHS
    for k, v in APP_PATHS.items():
        if key in k or k in key:
            if _path_exists(v):
                return v, k, "settings-fuzzy"

    return None, key, ""


def _path_exists(spec: str) -> bool:
    spec = (spec or "").strip()
    if not spec:
        return False
    exe = spec.split(" --")[0].strip()
    if Path(exe).exists():
        return True
    # notepad.exe, calc.exe — в PATH
    if exe.endswith(".exe") and "/" not in exe and "\\" not in exe:
        return True
    return False


def validate_app_paths() -> None:
    """Убрать битые пути и подставить из автоскана."""
    global APP_PATHS
    if not APP_PATHS:
        return
    scanned = _get_scanned_apps()
    fixed: dict[str, str] = {}
    for key, path in APP_PATHS.items():
        if _path_exists(path):
            fixed[key] = path
            continue
        logger.warning("broken app path %s: %s", key, path)
        for src in (scanned, DEFAULT_APP_PATHS):
            if key in src and _path_exists(src[key]):
                fixed[key] = src[key]
                logger.info("fixed %s → %s", key, src[key])
                break
    APP_PATHS = fixed


def ensure_app_paths() -> None:
    """Заполнить/починить APP_PATHS: скан + defaults + ручные."""
    global APP_PATHS
    try:
        import app_scanner
        found = app_scanner.scan_installed_apps(include_start_menu=True)
        for k in _PRIORITY_APPS:
            if k in found and _path_exists(found[k]):
                if k not in APP_PATHS or not _path_exists(APP_PATHS.get(k, "")):
                    APP_PATHS[k] = found[k]
        for k, v in APP_PATHS_MANUAL.items():
            if _path_exists(v):
                APP_PATHS[k] = v
        validate_app_paths()
        if APP_PATHS:
            logger.info("app paths ready: %d (%s)", len(APP_PATHS), ", ".join(sorted(APP_PATHS)[:8]))
    except Exception as exc:
        logger.warning("ensure_app_paths: %s", exc)


def vad_sensitivity_scale() -> tuple[float, float, float, float]:
    """
    Масштабирование порогов VAD по чувствительности (0..1).
    Возвращает (speech_mult, silence_mult, release_sec_mult, attack_mult).
    """
    s = max(0.0, min(1.0, VAD_SENSITIVITY))
    # Высокая чувствительность → ниже пороги, дольше release, быстрее attack
    speech_mult = 1.35 - s * 0.75
    silence_mult = 1.25 - s * 0.65
    release_sec_mult = 0.80 + s * 0.50
    attack_mult = 1.25 - s * 0.55
    return speech_mult, silence_mult, release_sec_mult, attack_mult


def vad_release_sec() -> float:
    """Эффективное время тишины для завершения записи."""
    _, _, release_m, _ = vad_sensitivity_scale()
    base = VAD_RELEASE_SEC if VAD_RELEASE_SEC > 0 else VAD_SILENCE_SEC
    return base * release_m


def _detect_gpu() -> tuple[str, str]:
    """Определить CUDA; на GTX 10xx используем int8, не float16."""
    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() > 0:
            return "cuda", "int8"
    except Exception:
        pass
    return "cpu", "int8"


def resolve_whisper_backend() -> list[tuple[str, str]]:
    """
    Цепочка fallback для Whisper:
      запрошенный device/compute → cuda/int8 → cpu/int8.
    float16 и auto принудительно заменяются на int8.
    """
    device, compute = WHISPER_DEVICE.lower(), WHISPER_COMPUTE_TYPE.lower()
    if device == "auto":
        device, compute = _detect_gpu()
    if compute in ("auto", "float16", "float32"):
        compute = "int8"

    chain = [(device, compute)]
    if (device, compute) != ("cuda", "int8"):
        chain.append(("cuda", "int8"))
    if (device, compute) != ("cpu", "int8"):
        chain.append(("cpu", "int8"))

    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for item in chain:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _apply_dict(data: dict[str, Any]) -> None:
    global WAKE_WORD, WAKE_WORD_ALIASES, WHISPER_MODEL, WHISPER_DEVICE
    global WHISPER_COMPUTE_TYPE, WHISPER_LANGUAGE, OLLAMA_HOST, OLLAMA_MODEL
    global OLLAMA_TEMPERATURE, OLLAMA_NUM_CTX, OLLAMA_NUM_GPU, OLLAMA_TOP_P
    global OLLAMA_REPEAT_PENALTY, OLLAMA_MODEL_FAST, OLLAMA_MODEL_SLOW
    global HYBRID_MODELS_ENABLED, SIMPLE_COMMAND_MAX_WORDS
    global LEARNING_ENABLED, LEARNING_AUTO_SCAN, LEARNING_MAX_ENTRIES, LEARNING_MAX_CORRECTIONS
    global TTS_VOICE, TTS_RATE, TTS_VOLUME
    global VAD_SENSITIVITY, VAD_SPEECH_THRESHOLD, VAD_SILENCE_THRESHOLD
    global VAD_SILENCE_SEC, VAD_RELEASE_SEC, VAD_HANGOVER_SEC, VAD_MAX_RECORD_SEC
    global VAD_MIN_SPEECH_SEC, VAD_WAIT_SPEECH_SEC, VAD_PRE_ROLL_SEC
    global VAD_ATTACK_SEC, VAD_END_PADDING_SEC, VAD_TRIM_TRAILING
    global VAD_LONG_SPEECH_BONUS_SEC, VAD_ADAPTIVE_NOISE_ALPHA
    global VAD_TTS_OVERLAP_SEC, VAD_MIC_WARMUP_SEC, VAD_DEBUG_LOG
    global VAD_DEBUG_LOG_INTERVAL_SEC, VAD_PEAK_WEIGHT, VAD_CHUNK_MS
    global VAD_NOISE_MULT_ON, VAD_NOISE_MULT_OFF, VAD_NOISE_CALIBRATION_SEC
    global POST_TTS_DELAY_SEC, MIC_DEVICE_ID
    global TTS_EDGE_TIMEOUT_SEC, TTS_SAPI_RATE
    global APP_PATHS, APP_PATHS_MANUAL, STEAM_GAMES, VPN_TOGGLE_BAT, TELEGRAM_API_ID
    global TELEGRAM_API_HASH, TELEGRAM_PHONE, TELEGRAM_BOT_TOKEN, TELEGRAM_DEFAULT_CHAT_ID
    global TELEGRAM_DEFAULT_CONTACT, TELEGRAM_CHATS, TELEGRAM_READ_DEFAULT_COUNT
    global TELEGRAM_DIALOGS_LIMIT, FILE_SEARCH_ROOTS, LOG_LEVEL, PLUGINS_ENABLED, GUI_THEME, GUI_ACCENT

    WAKE_WORD = str(data.get("wake_word", WAKE_WORD))
    WAKE_WORD_ALIASES = tuple(str(a).lower() for a in data.get("wake_word_aliases", WAKE_WORD_ALIASES))

    WHISPER_MODEL = str(data.get("whisper_model", WHISPER_MODEL))
    dev = str(data.get("whisper_device", WHISPER_DEVICE)).lower()
    comp = str(data.get("whisper_compute_type", WHISPER_COMPUTE_TYPE)).lower()
    if dev == "auto":
        WHISPER_DEVICE, ac = _detect_gpu()
        WHISPER_COMPUTE_TYPE = ac if comp == "auto" else ("int8" if comp == "float16" else comp)
    else:
        WHISPER_DEVICE = dev
        WHISPER_COMPUTE_TYPE = "int8" if comp in ("auto", "float16") else comp

    WHISPER_LANGUAGE = str(data.get("whisper_language", WHISPER_LANGUAGE))
    OLLAMA_HOST = str(data.get("ollama_host", OLLAMA_HOST))
    OLLAMA_MODEL = str(data.get("ollama_model", OLLAMA_MODEL))
    OLLAMA_MODEL_FAST = str(data.get("ollama_model_fast", OLLAMA_MODEL_FAST))
    OLLAMA_MODEL_SLOW = str(data.get("ollama_model_slow", OLLAMA_MODEL_SLOW))
    HYBRID_MODELS_ENABLED = bool(data.get("hybrid_models_enabled", HYBRID_MODELS_ENABLED))
    SIMPLE_COMMAND_MAX_WORDS = int(data.get("simple_command_max_words", SIMPLE_COMMAND_MAX_WORDS) or 6)
    LEARNING_ENABLED = bool(data.get("learning_enabled", LEARNING_ENABLED))
    LEARNING_AUTO_SCAN = bool(data.get("learning_auto_scan", LEARNING_AUTO_SCAN))
    LEARNING_MAX_ENTRIES = int(data.get("learning_max_entries", LEARNING_MAX_ENTRIES) or 200)
    LEARNING_MAX_CORRECTIONS = int(data.get("learning_max_corrections", LEARNING_MAX_CORRECTIONS) or 50)
    OLLAMA_TEMPERATURE = float(data.get("ollama_temperature", OLLAMA_TEMPERATURE))
    OLLAMA_NUM_CTX = int(data.get("ollama_num_ctx", OLLAMA_NUM_CTX))
    OLLAMA_NUM_GPU = int(data.get("ollama_num_gpu", OLLAMA_NUM_GPU))
    OLLAMA_TOP_P = float(data.get("ollama_top_p", OLLAMA_TOP_P))
    OLLAMA_REPEAT_PENALTY = float(data.get("ollama_repeat_penalty", OLLAMA_REPEAT_PENALTY))
    TTS_VOICE = str(data.get("tts_voice", TTS_VOICE))
    TTS_RATE = str(data.get("tts_rate", TTS_RATE))
    TTS_VOLUME = str(data.get("tts_volume", TTS_VOLUME))

    VAD_SENSITIVITY = float(data.get("vad_sensitivity", VAD_SENSITIVITY))
    VAD_SPEECH_THRESHOLD = float(data.get("vad_speech_threshold", VAD_SPEECH_THRESHOLD))
    VAD_SILENCE_THRESHOLD = float(data.get("vad_silence_threshold", VAD_SILENCE_THRESHOLD))
    VAD_SILENCE_SEC = float(data.get("vad_silence_sec", VAD_SILENCE_SEC))
    VAD_RELEASE_SEC = float(data.get("vad_release_sec", data.get("vad_silence_sec", VAD_RELEASE_SEC)))
    VAD_HANGOVER_SEC = float(data.get("vad_hangover_sec", VAD_HANGOVER_SEC))
    VAD_MAX_RECORD_SEC = float(data.get("vad_max_record_sec", VAD_MAX_RECORD_SEC))
    VAD_MIN_SPEECH_SEC = float(data.get("vad_min_speech_sec", VAD_MIN_SPEECH_SEC))
    VAD_WAIT_SPEECH_SEC = float(data.get("vad_wait_speech_sec", VAD_WAIT_SPEECH_SEC))
    VAD_PRE_ROLL_SEC = float(data.get("vad_pre_roll_sec", VAD_PRE_ROLL_SEC))
    VAD_ATTACK_SEC = float(data.get("vad_attack_sec", VAD_ATTACK_SEC))
    VAD_END_PADDING_SEC = float(data.get("vad_end_padding_sec", VAD_END_PADDING_SEC))
    VAD_TRIM_TRAILING = bool(data.get("vad_trim_trailing", VAD_TRIM_TRAILING))
    VAD_LONG_SPEECH_BONUS_SEC = float(data.get("vad_long_speech_bonus_sec", VAD_LONG_SPEECH_BONUS_SEC))
    VAD_ADAPTIVE_NOISE_ALPHA = float(data.get("vad_adaptive_noise_alpha", VAD_ADAPTIVE_NOISE_ALPHA))
    VAD_TTS_OVERLAP_SEC = float(data.get("vad_tts_overlap_sec", VAD_TTS_OVERLAP_SEC))
    VAD_MIC_WARMUP_SEC = float(data.get("vad_mic_warmup_sec", VAD_MIC_WARMUP_SEC))
    VAD_DEBUG_LOG = bool(data.get("vad_debug_log", VAD_DEBUG_LOG))
    VAD_DEBUG_LOG_INTERVAL_SEC = float(data.get("vad_debug_log_interval_sec", VAD_DEBUG_LOG_INTERVAL_SEC))
    VAD_PEAK_WEIGHT = float(data.get("vad_peak_weight", VAD_PEAK_WEIGHT))
    VAD_CHUNK_MS = int(data.get("vad_chunk_ms", VAD_CHUNK_MS))
    VAD_NOISE_MULT_ON = float(data.get("vad_noise_mult_on", VAD_NOISE_MULT_ON))
    VAD_NOISE_MULT_OFF = float(data.get("vad_noise_mult_off", VAD_NOISE_MULT_OFF))
    VAD_NOISE_CALIBRATION_SEC = float(data.get("vad_noise_calibration_sec", VAD_NOISE_CALIBRATION_SEC))
    POST_TTS_DELAY_SEC = float(data.get("post_tts_delay_sec", POST_TTS_DELAY_SEC))
    TTS_EDGE_TIMEOUT_SEC = int(data.get("tts_edge_timeout_sec", TTS_EDGE_TIMEOUT_SEC))
    TTS_SAPI_RATE = int(data.get("tts_sapi_rate", TTS_SAPI_RATE))

    mic = data.get("mic_device_id")
    MIC_DEVICE_ID = int(mic) if mic is not None and str(mic).strip() != "" else None

    APP_PATHS = {str(k).lower(): str(v) for k, v in (data.get("app_paths") or {}).items()}
    APP_PATHS_MANUAL = {
        str(k).lower(): str(v) for k, v in (data.get("app_paths_manual") or APP_PATHS_MANUAL or {}).items()
    }
    STEAM_GAMES = {str(k).lower(): str(v) for k, v in (data.get("steam_games") or {}).items()}
    VPN_TOGGLE_BAT = str(data.get("vpn_toggle_bat", VPN_TOGGLE_BAT))
    TELEGRAM_API_ID = int(data.get("telegram_api_id", TELEGRAM_API_ID) or 0)
    TELEGRAM_API_HASH = str(data.get("telegram_api_hash", TELEGRAM_API_HASH))
    TELEGRAM_PHONE = str(data.get("telegram_phone", TELEGRAM_PHONE))
    TELEGRAM_BOT_TOKEN = str(data.get("telegram_bot_token", TELEGRAM_BOT_TOKEN))
    TELEGRAM_DEFAULT_CHAT_ID = str(data.get("telegram_default_chat_id", TELEGRAM_DEFAULT_CHAT_ID))
    TELEGRAM_DEFAULT_CONTACT = str(data.get("telegram_default_contact", TELEGRAM_DEFAULT_CONTACT))
    TELEGRAM_CHATS = {str(k).lower(): str(v) for k, v in (data.get("telegram_chats") or {}).items()}
    TELEGRAM_READ_DEFAULT_COUNT = int(data.get("telegram_read_default_count", TELEGRAM_READ_DEFAULT_COUNT) or 5)
    TELEGRAM_DIALOGS_LIMIT = int(data.get("telegram_dialogs_limit", TELEGRAM_DIALOGS_LIMIT) or 60)
    FILE_SEARCH_ROOTS = list(
        data.get("file_search_roots")
        or FILE_SEARCH_ROOTS
        or [str(Path.home() / "Desktop"), str(Path.home() / "Documents")]
    )
    LOG_LEVEL = str(data.get("log_level", LOG_LEVEL))
    PLUGINS_ENABLED = bool(data.get("plugins_enabled", PLUGINS_ENABLED))
    GUI_THEME = str(data.get("gui_theme", GUI_THEME))
    GUI_ACCENT = str(data.get("gui_accent", GUI_ACCENT))


def to_dict() -> dict[str, Any]:
    return {
        "wake_word": WAKE_WORD,
        "wake_word_aliases": list(WAKE_WORD_ALIASES),
        "whisper_model": WHISPER_MODEL,
        "whisper_device": WHISPER_DEVICE,
        "whisper_compute_type": WHISPER_COMPUTE_TYPE,
        "whisper_language": WHISPER_LANGUAGE,
        "ollama_host": OLLAMA_HOST,
        "ollama_model": OLLAMA_MODEL,
        "ollama_model_fast": OLLAMA_MODEL_FAST,
        "ollama_model_slow": OLLAMA_MODEL_SLOW,
        "hybrid_models_enabled": HYBRID_MODELS_ENABLED,
        "simple_command_max_words": SIMPLE_COMMAND_MAX_WORDS,
        "learning_enabled": LEARNING_ENABLED,
        "learning_auto_scan": LEARNING_AUTO_SCAN,
        "learning_max_entries": LEARNING_MAX_ENTRIES,
        "learning_max_corrections": LEARNING_MAX_CORRECTIONS,
        "ollama_temperature": OLLAMA_TEMPERATURE,
        "ollama_num_ctx": OLLAMA_NUM_CTX,
        "ollama_num_gpu": OLLAMA_NUM_GPU,
        "ollama_top_p": OLLAMA_TOP_P,
        "ollama_repeat_penalty": OLLAMA_REPEAT_PENALTY,
        "tts_voice": TTS_VOICE,
        "tts_rate": TTS_RATE,
        "tts_volume": TTS_VOLUME,
        "vad_sensitivity": VAD_SENSITIVITY,
        "vad_speech_threshold": VAD_SPEECH_THRESHOLD,
        "vad_silence_threshold": VAD_SILENCE_THRESHOLD,
        "vad_silence_sec": VAD_SILENCE_SEC,
        "vad_release_sec": VAD_RELEASE_SEC,
        "vad_hangover_sec": VAD_HANGOVER_SEC,
        "vad_max_record_sec": VAD_MAX_RECORD_SEC,
        "vad_min_speech_sec": VAD_MIN_SPEECH_SEC,
        "vad_wait_speech_sec": VAD_WAIT_SPEECH_SEC,
        "vad_pre_roll_sec": VAD_PRE_ROLL_SEC,
        "vad_attack_sec": VAD_ATTACK_SEC,
        "vad_end_padding_sec": VAD_END_PADDING_SEC,
        "vad_trim_trailing": VAD_TRIM_TRAILING,
        "vad_long_speech_bonus_sec": VAD_LONG_SPEECH_BONUS_SEC,
        "vad_adaptive_noise_alpha": VAD_ADAPTIVE_NOISE_ALPHA,
        "vad_tts_overlap_sec": VAD_TTS_OVERLAP_SEC,
        "vad_mic_warmup_sec": VAD_MIC_WARMUP_SEC,
        "vad_debug_log": VAD_DEBUG_LOG,
        "vad_debug_log_interval_sec": VAD_DEBUG_LOG_INTERVAL_SEC,
        "vad_peak_weight": VAD_PEAK_WEIGHT,
        "vad_chunk_ms": VAD_CHUNK_MS,
        "vad_noise_mult_on": VAD_NOISE_MULT_ON,
        "vad_noise_mult_off": VAD_NOISE_MULT_OFF,
        "vad_noise_calibration_sec": VAD_NOISE_CALIBRATION_SEC,
        "post_tts_delay_sec": POST_TTS_DELAY_SEC,
        "tts_edge_timeout_sec": TTS_EDGE_TIMEOUT_SEC,
        "tts_sapi_rate": TTS_SAPI_RATE,
        "mic_device_id": MIC_DEVICE_ID,
        "app_paths": APP_PATHS,
        "app_paths_manual": APP_PATHS_MANUAL,
        "steam_games": STEAM_GAMES,
        "vpn_toggle_bat": VPN_TOGGLE_BAT,
        "telegram_api_id": TELEGRAM_API_ID,
        "telegram_api_hash": TELEGRAM_API_HASH,
        "telegram_phone": TELEGRAM_PHONE,
        "telegram_bot_token": TELEGRAM_BOT_TOKEN,
        "telegram_default_chat_id": TELEGRAM_DEFAULT_CHAT_ID,
        "telegram_default_contact": TELEGRAM_DEFAULT_CONTACT,
        "telegram_chats": TELEGRAM_CHATS,
        "telegram_read_default_count": TELEGRAM_READ_DEFAULT_COUNT,
        "telegram_dialogs_limit": TELEGRAM_DIALOGS_LIMIT,
        "file_search_roots": FILE_SEARCH_ROOTS,
        "log_level": LOG_LEVEL,
        "plugins_enabled": PLUGINS_ENABLED,
        "gui_theme": GUI_THEME,
        "gui_accent": GUI_ACCENT,
    }


def load_settings(path: Path | None = None) -> None:
    path = path or SETTINGS_PATH
    if not path.exists():
        return
    try:
        _apply_dict(json.loads(path.read_text(encoding="utf-8")))
        logger.info("settings loaded")
    except Exception as exc:
        logger.error("settings load failed: %s", exc)


def save_settings(path: Path | None = None) -> None:
    (path or SETTINGS_PATH).write_text(json.dumps(to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")


def reload_settings() -> None:
    load_settings()


def set_app_paths(enabled: dict[str, str], manual: dict[str, str] | None = None) -> None:
    """Обновить список активных приложений и сохранить."""
    global APP_PATHS, APP_PATHS_MANUAL
    APP_PATHS = {k.lower(): v for k, v in enabled.items()}
    if manual is not None:
        APP_PATHS_MANUAL = {k.lower(): v for k, v in manual.items()}
    invalidate_app_cache()
    save_settings()


load_settings()
ensure_app_paths()