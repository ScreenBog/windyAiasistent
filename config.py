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

# ── VAD defaults (continuous listening) ───────────────────────────────────────
# Чувствительность 0.0–1.0: выше → легче начать запись, дольше ждёт паузу.
VAD_SENSITIVITY = 0.55
VAD_CHUNK_MS = 80
VAD_RMS_SMOOTH_WINDOW = 6
VAD_ENERGY_WEIGHT = 0.35
VAD_NOISE_CALIBRATION_SEC = 0.5
VAD_NOISE_MULT_ON = 3.0
VAD_NOISE_MULT_OFF = 1.6
VAD_SPEECH_THRESHOLD = 0.010
VAD_SILENCE_THRESHOLD = 0.005
VAD_SILENCE_SEC = 2.5
VAD_HANGOVER_SEC = 1.2
VAD_MAX_RECORD_SEC = 40.0
VAD_MIN_SPEECH_SEC = 0.45
VAD_WAIT_SPEECH_SEC = 14.0
VAD_PRE_ROLL_SEC = 0.9
POST_TTS_DELAY_SEC = 1.25
MIC_DEVICE_ID: int | None = None  # None = системный микрофон по умолчанию

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
TELEGRAM_BOT_TOKEN = ""
TELEGRAM_DEFAULT_CHAT_ID = ""
TELEGRAM_CHATS: dict[str, str] = {}
FILE_SEARCH_ROOTS: list[str] = []
LOG_LEVEL = "INFO"
PLUGINS_ENABLED = True
GUI_THEME = "dark"
GUI_ACCENT = "#3b82f6"
GUI_ACCENT_HOVER = "#2563eb"
GUI_SUCCESS = "#22c55e"
GUI_WARNING = "#f59e0b"
GUI_DANGER = "#ef4444"
WHISPER_MODELS = ("tiny", "base", "small", "medium", "large-v3")

WHISPER_BEAM_SIZE = 5
WHISPER_BEST_OF = 3
WHISPER_NO_SPEECH_THRESHOLD = 0.45
WHISPER_COMPRESSION_RATIO_THRESHOLD = 2.4
WHISPER_LOG_PROB_THRESHOLD = -1.0


def vad_sensitivity_scale() -> tuple[float, float, float]:
    """
    Масштабирование порогов VAD по чувствительности (0..1).
    Возвращает (speech_mult, silence_mult, silence_sec_mult).
    """
    s = max(0.0, min(1.0, VAD_SENSITIVITY))
    # Высокая чувствительность → ниже пороги, дольше ждём паузу
    speech_mult = 1.4 - s * 0.8
    silence_mult = 1.3 - s * 0.7
    silence_sec_mult = 0.75 + s * 0.55
    return speech_mult, silence_mult, silence_sec_mult


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
    global OLLAMA_REPEAT_PENALTY, TTS_VOICE, TTS_RATE, TTS_VOLUME
    global VAD_SENSITIVITY, VAD_SPEECH_THRESHOLD, VAD_SILENCE_THRESHOLD
    global VAD_SILENCE_SEC, VAD_HANGOVER_SEC, VAD_MAX_RECORD_SEC
    global VAD_MIN_SPEECH_SEC, VAD_WAIT_SPEECH_SEC, VAD_PRE_ROLL_SEC
    global VAD_NOISE_MULT_ON, VAD_NOISE_MULT_OFF, VAD_NOISE_CALIBRATION_SEC
    global POST_TTS_DELAY_SEC, MIC_DEVICE_ID
    global APP_PATHS, APP_PATHS_MANUAL, STEAM_GAMES, VPN_TOGGLE_BAT, TELEGRAM_API_ID
    global TELEGRAM_API_HASH, TELEGRAM_BOT_TOKEN, TELEGRAM_DEFAULT_CHAT_ID
    global TELEGRAM_CHATS, FILE_SEARCH_ROOTS, LOG_LEVEL, PLUGINS_ENABLED, GUI_THEME, GUI_ACCENT

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
    VAD_HANGOVER_SEC = float(data.get("vad_hangover_sec", VAD_HANGOVER_SEC))
    VAD_MAX_RECORD_SEC = float(data.get("vad_max_record_sec", VAD_MAX_RECORD_SEC))
    VAD_MIN_SPEECH_SEC = float(data.get("vad_min_speech_sec", VAD_MIN_SPEECH_SEC))
    VAD_WAIT_SPEECH_SEC = float(data.get("vad_wait_speech_sec", VAD_WAIT_SPEECH_SEC))
    VAD_PRE_ROLL_SEC = float(data.get("vad_pre_roll_sec", VAD_PRE_ROLL_SEC))
    VAD_NOISE_MULT_ON = float(data.get("vad_noise_mult_on", VAD_NOISE_MULT_ON))
    VAD_NOISE_MULT_OFF = float(data.get("vad_noise_mult_off", VAD_NOISE_MULT_OFF))
    VAD_NOISE_CALIBRATION_SEC = float(data.get("vad_noise_calibration_sec", VAD_NOISE_CALIBRATION_SEC))
    POST_TTS_DELAY_SEC = float(data.get("post_tts_delay_sec", POST_TTS_DELAY_SEC))

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
    TELEGRAM_BOT_TOKEN = str(data.get("telegram_bot_token", TELEGRAM_BOT_TOKEN))
    TELEGRAM_DEFAULT_CHAT_ID = str(data.get("telegram_default_chat_id", TELEGRAM_DEFAULT_CHAT_ID))
    TELEGRAM_CHATS = {str(k).lower(): str(v) for k, v in (data.get("telegram_chats") or {}).items()}
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
        "vad_hangover_sec": VAD_HANGOVER_SEC,
        "vad_max_record_sec": VAD_MAX_RECORD_SEC,
        "vad_min_speech_sec": VAD_MIN_SPEECH_SEC,
        "vad_wait_speech_sec": VAD_WAIT_SPEECH_SEC,
        "vad_pre_roll_sec": VAD_PRE_ROLL_SEC,
        "vad_noise_mult_on": VAD_NOISE_MULT_ON,
        "vad_noise_mult_off": VAD_NOISE_MULT_OFF,
        "vad_noise_calibration_sec": VAD_NOISE_CALIBRATION_SEC,
        "post_tts_delay_sec": POST_TTS_DELAY_SEC,
        "mic_device_id": MIC_DEVICE_ID,
        "app_paths": APP_PATHS,
        "app_paths_manual": APP_PATHS_MANUAL,
        "steam_games": STEAM_GAMES,
        "vpn_toggle_bat": VPN_TOGGLE_BAT,
        "telegram_api_id": TELEGRAM_API_ID,
        "telegram_api_hash": TELEGRAM_API_HASH,
        "telegram_bot_token": TELEGRAM_BOT_TOKEN,
        "telegram_default_chat_id": TELEGRAM_DEFAULT_CHAT_ID,
        "telegram_chats": TELEGRAM_CHATS,
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
    save_settings()


load_settings()