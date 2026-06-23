"""
Настройки Windy AI Assistant.
Загружает settings.json, hot-reload, сохранение из GUI.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

# Bootstrap path до любых локальных импортов
import bootstrap  # noqa: F401
from bootstrap import PROJECT_DIR as BASE_DIR

logger = logging.getLogger(__name__)

# --- Пути проекта ---
PROMPTS_DIR = BASE_DIR / "prompts"
PLUGINS_DIR = BASE_DIR / "plugins"
DATA_DIR = BASE_DIR / "data"
SYSTEM_PROMPT_PATH = PROMPTS_DIR / "system.txt"
SETTINGS_PATH = BASE_DIR / "settings.json"
TEMP_DIR = BASE_DIR / "temp"
LOG_DIR = BASE_DIR / "logs"
MODELFILE_PATH = BASE_DIR / "Modelfile"
TELEGRAM_STATE_PATH = DATA_DIR / "telegram_offset.json"

for _dir in (TEMP_DIR, LOG_DIR, PLUGINS_DIR, DATA_DIR):
    _dir.mkdir(exist_ok=True)

# --- Аудио (константы) ---
SAMPLE_RATE = 16_000
CHANNELS = 1
DTYPE = "float32"

# --- Поведение по умолчанию ---
ASSISTANT_NAME = "Винди"
STARTUP_GREETING = "Винди на связи. Скажи «Винди», когда понадоблюсь."
ERROR_GENERIC = "Произошла ошибка. Попробуй ещё раз."
NO_SPEECH = "Я тебя не расслышал."
CONFIRM_WAKE = "Слушаю."

WAKE_CHUNK_SEC = 2.5
WAKE_POLL_INTERVAL = 0.3
VAD_CHUNK_MS = 100
VAD_PRE_ROLL_SEC = 0.6
VAD_RMS_SMOOTH_WINDOW = 4
TTS_RATE = "+0%"
TTS_VOLUME = "+0%"
TTS_USE_SAPI_FALLBACK = True
OLLAMA_TIMEOUT = 90

# Mutable settings (перезаписываются из settings.json)
WAKE_WORD = "винди"
WAKE_WORD_ALIASES: tuple[str, ...] = ("windy", "винди", "уинди")
WHISPER_MODEL = "small"
WHISPER_DEVICE = "cpu"
WHISPER_COMPUTE_TYPE = "int8"
WHISPER_LANGUAGE = "ru"
OLLAMA_HOST = "http://127.0.0.1:11434"
OLLAMA_MODEL = "qwen2.5:3b-windy"
OLLAMA_TEMPERATURE = 0.3
OLLAMA_NUM_CTX = 4096
OLLAMA_NUM_GPU = -1
OLLAMA_TOP_P = 0.9
OLLAMA_REPEAT_PENALTY = 1.1
TTS_VOICE = "ru-RU-DmitryNeural"
VAD_SPEECH_THRESHOLD = 0.012
VAD_SILENCE_THRESHOLD = 0.006
VAD_SILENCE_SEC = 2.5
VAD_HANGOVER_SEC = 0.8
VAD_MAX_RECORD_SEC = 30.0
VAD_MIN_SPEECH_SEC = 0.5
VAD_WAIT_SPEECH_SEC = 10.0
POST_TTS_DELAY_SEC = 1.2
APP_PATHS: dict[str, str] = {}
STEAM_GAMES: dict[str, str] = {}
VPN_TOGGLE_BAT = ""
TELEGRAM_BOT_TOKEN = ""
TELEGRAM_DEFAULT_CHAT_ID = ""
TELEGRAM_CHATS: dict[str, str] = {}
LOG_LEVEL = "INFO"
PLUGINS_ENABLED = True

WHISPER_BEAM_SIZE = 5
WHISPER_BEST_OF = 3
WHISPER_NO_SPEECH_THRESHOLD = 0.5
WHISPER_COMPRESSION_RATIO_THRESHOLD = 2.4
WHISPER_LOG_PROB_THRESHOLD = -1.0


def _detect_gpu() -> tuple[str, str]:
    """
    Автоопределение Whisper backend.
    GTX 10xx (Pascal): int8 на CUDA — стабильно; float16 часто падает.
    """
    try:
        import ctranslate2

        if ctranslate2.get_cuda_device_count() > 0:
            return "cuda", "int8"
    except Exception as exc:
        logger.debug("CUDA недоступна: %s", exc)
    return "cpu", "int8"


def resolve_whisper_backend(
    device: str | None = None,
    compute_type: str | None = None,
) -> list[tuple[str, str]]:
    """
    Цепочка fallback для Whisper.
    Порядок: запрошенный → cuda/int8 → cpu/int8.
    """
    device = (device or WHISPER_DEVICE).lower()
    compute = (compute_type or WHISPER_COMPUTE_TYPE).lower()

    if device == "auto":
        device, compute = _detect_gpu()
    if compute == "auto":
        compute = "int8" if device == "cuda" else "int8"
    # float16 на старых GPU не ставим в приоритет
    if compute == "float16" and device == "cuda":
        compute = "int8"

    chain: list[tuple[str, str]] = [(device, compute)]
    if (device, compute) != ("cuda", "int8"):
        chain.append(("cuda", "int8"))
    if (device, compute) != ("cpu", "int8"):
        chain.append(("cpu", "int8"))

    # Убираем дубликаты, сохраняя порядок
    seen: set[tuple[str, str]] = set()
    unique: list[tuple[str, str]] = []
    for item in chain:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return unique


def _apply_dict(data: dict[str, Any]) -> None:
    global WAKE_WORD, WAKE_WORD_ALIASES, WHISPER_MODEL, WHISPER_DEVICE
    global WHISPER_COMPUTE_TYPE, WHISPER_LANGUAGE, OLLAMA_HOST, OLLAMA_MODEL
    global OLLAMA_TEMPERATURE, OLLAMA_NUM_CTX, OLLAMA_NUM_GPU, OLLAMA_TOP_P
    global OLLAMA_REPEAT_PENALTY, TTS_VOICE, VAD_SPEECH_THRESHOLD
    global VAD_SILENCE_THRESHOLD, VAD_SILENCE_SEC, VAD_HANGOVER_SEC
    global VAD_MAX_RECORD_SEC, VAD_MIN_SPEECH_SEC, VAD_WAIT_SPEECH_SEC
    global POST_TTS_DELAY_SEC, APP_PATHS, STEAM_GAMES, VPN_TOGGLE_BAT
    global TELEGRAM_BOT_TOKEN, TELEGRAM_DEFAULT_CHAT_ID, TELEGRAM_CHATS
    global LOG_LEVEL, PLUGINS_ENABLED

    WAKE_WORD = str(data.get("wake_word", WAKE_WORD))
    aliases = data.get("wake_word_aliases", list(WAKE_WORD_ALIASES))
    WAKE_WORD_ALIASES = tuple(str(a).lower() for a in aliases)

    WHISPER_MODEL = str(data.get("whisper_model", WHISPER_MODEL))
    device = str(data.get("whisper_device", WHISPER_DEVICE)).lower()
    compute = str(data.get("whisper_compute_type", WHISPER_COMPUTE_TYPE)).lower()

    if device == "auto":
        WHISPER_DEVICE, auto_compute = _detect_gpu()
        WHISPER_COMPUTE_TYPE = auto_compute if compute == "auto" else compute
    else:
        WHISPER_DEVICE = device
        if compute == "auto":
            WHISPER_COMPUTE_TYPE = "int8"
        else:
            WHISPER_COMPUTE_TYPE = compute

    # Защита от float16 на Pascal
    if WHISPER_COMPUTE_TYPE == "float16":
        logger.warning("float16 отключён (нестабилен на GTX 10xx) → int8")
        WHISPER_COMPUTE_TYPE = "int8"

    WHISPER_LANGUAGE = str(data.get("whisper_language", WHISPER_LANGUAGE))

    OLLAMA_HOST = str(data.get("ollama_host", OLLAMA_HOST))
    OLLAMA_MODEL = str(data.get("ollama_model", OLLAMA_MODEL))
    OLLAMA_TEMPERATURE = float(data.get("ollama_temperature", OLLAMA_TEMPERATURE))
    OLLAMA_NUM_CTX = int(data.get("ollama_num_ctx", OLLAMA_NUM_CTX))
    OLLAMA_NUM_GPU = int(data.get("ollama_num_gpu", OLLAMA_NUM_GPU))
    OLLAMA_TOP_P = float(data.get("ollama_top_p", OLLAMA_TOP_P))
    OLLAMA_REPEAT_PENALTY = float(data.get("ollama_repeat_penalty", OLLAMA_REPEAT_PENALTY))

    TTS_VOICE = str(data.get("tts_voice", TTS_VOICE))

    VAD_SPEECH_THRESHOLD = float(data.get("vad_speech_threshold", VAD_SPEECH_THRESHOLD))
    VAD_SILENCE_THRESHOLD = float(data.get("vad_silence_threshold", VAD_SILENCE_THRESHOLD))
    VAD_SILENCE_SEC = float(data.get("vad_silence_sec", VAD_SILENCE_SEC))
    VAD_HANGOVER_SEC = float(data.get("vad_hangover_sec", VAD_HANGOVER_SEC))
    VAD_MAX_RECORD_SEC = float(data.get("vad_max_record_sec", VAD_MAX_RECORD_SEC))
    VAD_MIN_SPEECH_SEC = float(data.get("vad_min_speech_sec", VAD_MIN_SPEECH_SEC))
    VAD_WAIT_SPEECH_SEC = float(data.get("vad_wait_speech_sec", VAD_WAIT_SPEECH_SEC))
    POST_TTS_DELAY_SEC = float(data.get("post_tts_delay_sec", POST_TTS_DELAY_SEC))

    APP_PATHS = {str(k).lower(): str(v) for k, v in (data.get("app_paths") or {}).items()}
    STEAM_GAMES = {str(k).lower(): str(v) for k, v in (data.get("steam_games") or {}).items()}
    VPN_TOGGLE_BAT = str(data.get("vpn_toggle_bat", VPN_TOGGLE_BAT))
    TELEGRAM_BOT_TOKEN = str(data.get("telegram_bot_token", TELEGRAM_BOT_TOKEN))
    TELEGRAM_DEFAULT_CHAT_ID = str(data.get("telegram_default_chat_id", TELEGRAM_DEFAULT_CHAT_ID))
    TELEGRAM_CHATS = {
        str(k).lower(): str(v) for k, v in (data.get("telegram_chats") or {}).items()
    }
    LOG_LEVEL = str(data.get("log_level", LOG_LEVEL))
    PLUGINS_ENABLED = bool(data.get("plugins_enabled", PLUGINS_ENABLED))


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
        "vad_speech_threshold": VAD_SPEECH_THRESHOLD,
        "vad_silence_threshold": VAD_SILENCE_THRESHOLD,
        "vad_silence_sec": VAD_SILENCE_SEC,
        "vad_hangover_sec": VAD_HANGOVER_SEC,
        "vad_max_record_sec": VAD_MAX_RECORD_SEC,
        "vad_min_speech_sec": VAD_MIN_SPEECH_SEC,
        "vad_wait_speech_sec": VAD_WAIT_SPEECH_SEC,
        "post_tts_delay_sec": POST_TTS_DELAY_SEC,
        "app_paths": APP_PATHS,
        "steam_games": STEAM_GAMES,
        "vpn_toggle_bat": VPN_TOGGLE_BAT,
        "telegram_bot_token": TELEGRAM_BOT_TOKEN,
        "telegram_default_chat_id": TELEGRAM_DEFAULT_CHAT_ID,
        "telegram_chats": TELEGRAM_CHATS,
        "log_level": LOG_LEVEL,
        "plugins_enabled": PLUGINS_ENABLED,
    }


def load_settings(path: Path | None = None) -> None:
    path = path or SETTINGS_PATH
    if not path.exists():
        logger.warning("settings.json не найден — значения по умолчанию")
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        _apply_dict(data)
        logger.info("Настройки загружены: %s", path)
    except Exception as exc:
        logger.error("Ошибка settings.json: %s", exc)


def save_settings(path: Path | None = None) -> None:
    path = path or SETTINGS_PATH
    try:
        path.write_text(json.dumps(to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("Настройки сохранены: %s", path)
    except Exception as exc:
        logger.error("Ошибка сохранения: %s", exc)
        raise


def reload_settings() -> None:
    load_settings()


load_settings()