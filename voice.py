"""
Голосовой модуль Windy AI Assistant.

- STT: faster-whisper (small/base, cuda/cpu, int8)
- TTS: edge-tts + SAPI fallback
- Wake-word: «Эй Винди», «Hey Винди», «Винди»
- VAD: RMS + hangover + pre-roll buffer
"""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess
import tempfile
import time
from collections import deque
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import sounddevice as sd
from scipy.signal import butter, filtfilt

import bootstrap  # noqa: F401
import config

logger = logging.getLogger(__name__)

_whisper_model = None
_whisper_key: tuple | None = None
_on_wake_callback: Callable[[], None] | None = None

_FILLER = re.compile(r"\b(э+|мм+|ну+|ага|угу|типа|как бы)\b", re.IGNORECASE)


def set_wake_callback(cb: Callable[[], None] | None) -> None:
    """GUI может подписаться на событие wake-word."""
    global _on_wake_callback
    _on_wake_callback = cb


def reset_whisper_model() -> None:
    global _whisper_model, _whisper_key
    _whisper_model = None
    _whisper_key = None


def _load_whisper():
    from faster_whisper import WhisperModel

    last_err: Exception | None = None
    for device, compute in config.resolve_whisper_backend():
        try:
            logger.info("Whisper: model=%s device=%s compute=%s", config.WHISPER_MODEL, device, compute)
            return WhisperModel(config.WHISPER_MODEL, device=device, compute_type=compute), device, compute
        except Exception as exc:
            last_err = exc
            logger.warning("Whisper fail %s/%s: %s", device, compute, exc)
    raise RuntimeError(f"Whisper не загружен: {last_err}")


def _get_whisper():
    global _whisper_model, _whisper_key
    key = (config.WHISPER_MODEL, config.WHISPER_DEVICE, config.WHISPER_COMPUTE_TYPE)
    if _whisper_model and _whisper_key == key:
        return _whisper_model
    model, dev, comp = _load_whisper()
    _whisper_model = model
    _whisper_key = (config.WHISPER_MODEL, dev, comp)
    return _whisper_model


def _rms(audio: np.ndarray) -> float:
    if audio.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(audio, dtype=np.float64))))


def _smooth(level: float, buf: deque[float]) -> float:
    buf.append(level)
    return float(np.mean(buf))


def _highpass(audio: np.ndarray) -> np.ndarray:
    try:
        if audio.size < 64:
            return audio
        nyq = config.SAMPLE_RATE / 2
        b, a = butter(2, max(80 / nyq, 0.001), btype="high")
        return filtfilt(b, a, audio).astype(np.float32)
    except Exception:
        return audio


def _normalize(audio: np.ndarray, target: float = 0.05) -> np.ndarray:
    r = _rms(audio)
    if r < 1e-6:
        return audio
    return np.clip(audio * min(target / r, 10.0), -1.0, 1.0).astype(np.float32)


def _clean_text(text: str) -> str:
    if not text:
        return ""
    text = _FILLER.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    words: list[str] = []
    for w in text.split():
        if not words or words[-1].lower() != w.lower():
            words.append(w)
    return " ".join(words)


def _record_seconds(sec: float) -> np.ndarray:
    n = int(sec * config.SAMPLE_RATE)
    if n <= 0:
        return np.array([], dtype=np.float32)
    rec = sd.rec(n, samplerate=config.SAMPLE_RATE, channels=config.CHANNELS, dtype=config.DTYPE)
    sd.wait()
    return rec.flatten().astype(np.float32)


def _ambient_noise(sec: float = 0.5) -> float:
    try:
        return _rms(_record_seconds(sec))
    except Exception:
        return 0.0


def record_continuous() -> np.ndarray:
    """
    Непрерывная запись после wake-word.

    Алгоритм:
    1. Pre-roll buffer — сохраняет начало фразы.
    2. Сглаженный RMS — меньше ложных срабатываний.
    3. Hangover — короткие паузы в речи не обрезают запись.
    4. N тихих чанков подряд → конец записи.
    """
    chunk_n = int(config.SAMPLE_RATE * config.VAD_CHUNK_MS / 1000)
    chunk_dt = config.VAD_CHUNK_MS / 1000.0
    pre_max = max(1, int(config.VAD_PRE_ROLL_SEC / chunk_dt))
    silent_need = max(4, int(config.VAD_SILENCE_SEC / chunk_dt))
    hang_extra = max(2, int(config.VAD_HANGOVER_SEC / chunk_dt))

    noise = _ambient_noise(0.4)
    thr_speech = max(config.VAD_SPEECH_THRESHOLD, noise * 3.0)
    thr_silence = max(config.VAD_SILENCE_THRESHOLD, noise * 1.8)
    logger.debug("VAD noise=%.4f speech=%.4f silence=%.4f", noise, thr_speech, thr_silence)

    recorded: list[np.ndarray] = []
    pre: deque[np.ndarray] = deque(maxlen=pre_max)
    rms_buf: deque[float] = deque(maxlen=config.VAD_RMS_SMOOTH_WINDOW)

    started = False
    speech_t = wait_t = total_t = 0.0
    silent_run = 0
    last_voice_t = 0.0

    with sd.InputStream(
        samplerate=config.SAMPLE_RATE,
        channels=config.CHANNELS,
        dtype=config.DTYPE,
        blocksize=chunk_n,
    ) as stream:
        while total_t < config.VAD_MAX_RECORD_SEC:
            chunk, ov = stream.read(chunk_n)
            if ov:
                logger.warning("audio overflow")
            data = np.asarray(chunk, dtype=np.float32).flatten()
            lvl = _smooth(_rms(data), rms_buf)
            total_t += chunk_dt

            if not started:
                wait_t += chunk_dt
                pre.append(data)
                if lvl >= thr_speech:
                    started = True
                    recorded.extend(pre)
                    pre.clear()
                    speech_t = silent_run = 0.0
                    last_voice_t = total_t
                    logger.debug("VAD: speech start")
                elif wait_t >= config.VAD_WAIT_SPEECH_SEC:
                    logger.debug("VAD: wait timeout")
                    break
                continue

            recorded.append(data)
            speech_t += chunk_dt

            if lvl < thr_silence:
                silent_run += 1
            else:
                silent_run = 0
                last_voice_t = total_t

            need = silent_need
            if (total_t - last_voice_t) < config.VAD_HANGOVER_SEC:
                need += hang_extra

            if silent_run >= need and speech_t >= config.VAD_MIN_SPEECH_SEC:
                logger.debug("VAD: end speech=%.1fs silent_chunks=%d", speech_t, silent_run)
                break

    if not recorded:
        return np.array([], dtype=np.float32)
    return _normalize(_highpass(np.concatenate(recorded)))


def transcribe(audio: np.ndarray) -> str:
    if audio is None or audio.size < config.SAMPLE_RATE * 0.2:
        return ""
    audio = _normalize(_highpass(audio))
    if _rms(audio) < config.VAD_SILENCE_THRESHOLD * 0.25:
        return ""
    try:
        model = _get_whisper()
        segs, info = model.transcribe(
            audio,
            language=config.WHISPER_LANGUAGE,
            beam_size=config.WHISPER_BEAM_SIZE,
            best_of=config.WHISPER_BEST_OF,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 400, "speech_pad_ms": 350},
            no_speech_threshold=config.WHISPER_NO_SPEECH_THRESHOLD,
            compression_ratio_threshold=config.WHISPER_COMPRESSION_RATIO_THRESHOLD,
            log_prob_threshold=config.WHISPER_LOG_PROB_THRESHOLD,
            condition_on_previous_text=False,
            temperature=0.0,
        )
        text = _clean_text(" ".join(s.text.strip() for s in segs))
        logger.info("STT [%.1fs]: %r", getattr(info, "duration", 0) or 0, text)
        return text
    except Exception as exc:
        logger.error("STT: %s", exc)
        reset_whisper_model()
        return ""


def _normalize_wake(text: str) -> str:
    t = text.lower().strip()
    t = re.sub(r"[^\w\sа-яё]", " ", t, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", t).strip()


def _contains_wake_word(text: str) -> bool:
    norm = _normalize_wake(text)
    if not norm:
        return False
    # Сначала длинные фразы (точнее)
    for alias in sorted(config.WAKE_WORD_ALIASES, key=len, reverse=True):
        if alias in norm:
            return True
    return False


def listen_chunk(sec: float | None = None) -> str:
    return transcribe(_record_seconds(sec or config.WAKE_CHUNK_SEC))


def listen_command() -> str:
    time.sleep(config.POST_TTS_DELAY_SEC)
    return transcribe(record_continuous())


def wait_for_wake_word() -> bool:
    try:
        text = listen_chunk(config.WAKE_CHUNK_SEC)
        if _contains_wake_word(text):
            logger.info("Wake-word: %r", text)
            if _on_wake_callback:
                try:
                    _on_wake_callback()
                except Exception:
                    pass
            return True
        return False
    except Exception as exc:
        logger.error("wake-word: %s", exc)
        time.sleep(config.WAKE_POLL_INTERVAL)
        return False


# --- TTS ---

async def _edge_tts(text: str, path: Path) -> bool:
    try:
        import edge_tts
        await edge_tts.Communicate(text, voice=config.TTS_VOICE, rate=config.TTS_RATE, volume=config.TTS_VOLUME).save(str(path))
        return path.exists() and path.stat().st_size > 0
    except Exception as exc:
        logger.warning("edge-tts: %s", exc)
        return False


def _sapi(text: str) -> bool:
    if not config.TTS_USE_SAPI_FALLBACK:
        return False
    safe = text.replace("'", "''")
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"Add-Type -AssemblyName System.Speech; (New-Object System.Speech.Synthesis.SpeechSynthesizer).Speak('{safe}')"],
            check=True, capture_output=True, timeout=90,
        )
        return True
    except Exception as exc:
        logger.warning("SAPI: %s", exc)
        return False


def speak(text: str) -> None:
    if not text or not text.strip():
        return
    text = text.strip()
    logger.info("TTS: %r", text)
    tmp: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False, dir=config.TEMP_DIR) as f:
            tmp = Path(f.name)
        if asyncio.run(_edge_tts(text, tmp)):
            from playsound3 import playsound
            playsound(str(tmp), block=True)
            return
        _sapi(text)
    except Exception as exc:
        logger.error("TTS: %s", exc)
        _sapi(text)
    finally:
        if tmp and tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


class VoiceEngine:
    def wait_for_wake_word(self) -> bool:
        return wait_for_wake_word()

    def listen_command(self) -> str:
        return listen_command()

    def speak(self, text: str) -> None:
        speak(text)

    def record_continuous(self) -> np.ndarray:
        return record_continuous()

    def reload(self) -> None:
        config.reload_settings()
        reset_whisper_model()