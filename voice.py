"""
Голосовой модуль: STT (faster-whisper), TTS (edge-tts + SAPI), VAD continuous listening.
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
_on_wake: Callable[[], None] | None = None
_FILLER = re.compile(r"\b(э+|мм+|ну+|ага|угу|типа|как бы)\b", re.IGNORECASE)


def set_wake_callback(cb: Callable[[], None] | None) -> None:
    global _on_wake
    _on_wake = cb


def reset_whisper_model() -> None:
    global _whisper_model, _whisper_key
    _whisper_model = _whisper_key = None


def _load_whisper():
    from faster_whisper import WhisperModel
    err: Exception | None = None
    for dev, comp in config.resolve_whisper_backend():
        try:
            logger.info("Whisper try: %s/%s", dev, comp)
            return WhisperModel(config.WHISPER_MODEL, device=dev, compute_type=comp), dev, comp
        except Exception as exc:
            err = exc
            logger.warning("Whisper %s/%s: %s", dev, comp, exc)
    raise RuntimeError(f"Whisper load failed: {err}")


def _get_whisper():
    global _whisper_model, _whisper_key
    key = (config.WHISPER_MODEL, config.WHISPER_DEVICE, config.WHISPER_COMPUTE_TYPE)
    if _whisper_model and _whisper_key == key:
        return _whisper_model
    m, d, c = _load_whisper()
    _whisper_model, _whisper_key = m, (config.WHISPER_MODEL, d, c)
    return _whisper_model


def _rms(audio: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(audio, dtype=np.float64)))) if audio.size else 0.0


def _energy(audio: np.ndarray) -> float:
    return float(np.mean(np.square(audio, dtype=np.float64))) if audio.size else 0.0


def _voice_level(audio: np.ndarray) -> float:
    """Комбинированный уровень: RMS + energy (устойчивее к шуму)."""
    r, e = _rms(audio), _energy(audio)
    w = config.VAD_ENERGY_WEIGHT
    return (1 - w) * r + w * float(np.sqrt(e))


def _smooth(v: float, buf: deque[float]) -> float:
    buf.append(v)
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
    return np.clip(audio * min(target / r, 10.0), -1, 1).astype(np.float32) if r > 1e-6 else audio


def _clean(text: str) -> str:
    if not text:
        return ""
    text = _FILLER.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    words: list[str] = []
    for w in text.split():
        if not words or words[-1].lower() != w.lower():
            words.append(w)
    return " ".join(words)


def _record(sec: float) -> np.ndarray:
    n = int(sec * config.SAMPLE_RATE)
    if n <= 0:
        return np.array([], np.float32)
    r = sd.rec(n, samplerate=config.SAMPLE_RATE, channels=config.CHANNELS, dtype=config.DTYPE)
    sd.wait()
    return r.flatten().astype(np.float32)


def record_continuous() -> np.ndarray:
    """
    VAD continuous recording:
    - pre-roll buffer (не обрезает начало)
    - RMS + energy threshold
    - hangover (паузы внутри фразы)
    - N тихих чанков подряд → стоп
    """
    chunk_n = int(config.SAMPLE_RATE * config.VAD_CHUNK_MS / 1000)
    dt = config.VAD_CHUNK_MS / 1000.0
    pre_max = max(1, int(config.VAD_PRE_ROLL_SEC / dt))
    silent_need = max(4, int(config.VAD_SILENCE_SEC / dt))
    hang_extra = max(2, int(config.VAD_HANGOVER_SEC / dt))

    noise = _voice_level(_record(0.45))
    thr_on = max(config.VAD_SPEECH_THRESHOLD, noise * 3.2)
    thr_off = max(config.VAD_SILENCE_THRESHOLD, noise * 1.7)
    logger.debug("VAD noise=%.5f on=%.5f off=%.5f", noise, thr_on, thr_off)

    recorded: list[np.ndarray] = []
    pre: deque[np.ndarray] = deque(maxlen=pre_max)
    smooth: deque[float] = deque(maxlen=config.VAD_RMS_SMOOTH_WINDOW)

    started = speech_t = wait_t = total_t = 0.0
    silent_run = 0
    last_voice = 0.0

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
            data = np.asarray(chunk, np.float32).flatten()
            lvl = _smooth(_voice_level(data), smooth)
            total_t += dt

            if not started:
                wait_t += dt
                pre.append(data)
                if lvl >= thr_on:
                    started = True
                    recorded.extend(pre)
                    pre.clear()
                    speech_t = silent_run = 0.0
                    last_voice = total_t
                    logger.debug("VAD start lvl=%.5f", lvl)
                elif wait_t >= config.VAD_WAIT_SPEECH_SEC:
                    break
                continue

            recorded.append(data)
            speech_t += dt
            if lvl < thr_off:
                silent_run += 1
            else:
                silent_run = 0
                last_voice = total_t

            need = silent_need + (hang_extra if (total_t - last_voice) < config.VAD_HANGOVER_SEC else 0)
            if silent_run >= need and speech_t >= config.VAD_MIN_SPEECH_SEC:
                logger.debug("VAD end speech=%.1fs silent=%d", speech_t, silent_run)
                break

    return _normalize(_highpass(np.concatenate(recorded))) if recorded else np.array([], np.float32)


def transcribe(audio: np.ndarray) -> str:
    if audio is None or audio.size < config.SAMPLE_RATE * 0.2:
        return ""
    audio = _normalize(_highpass(audio))
    if _rms(audio) < config.VAD_SILENCE_THRESHOLD * 0.2:
        return ""
    try:
        segs, info = _get_whisper().transcribe(
            audio,
            language=config.WHISPER_LANGUAGE,
            beam_size=config.WHISPER_BEAM_SIZE,
            best_of=config.WHISPER_BEST_OF,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 450, "speech_pad_ms": 350},
            no_speech_threshold=config.WHISPER_NO_SPEECH_THRESHOLD,
            condition_on_previous_text=False,
            temperature=0.0,
        )
        text = _clean(" ".join(s.text.strip() for s in segs))
        logger.info("STT [%.1fs]: %r", getattr(info, "duration", 0) or 0, text)
        return text
    except Exception as exc:
        logger.error("STT: %s", exc)
        reset_whisper_model()
        return ""


def _norm_wake(t: str) -> str:
    t = t.lower()
    t = re.sub(r"[^\w\sа-яё]", " ", t, flags=re.I)
    return re.sub(r"\s+", " ", t).strip()


def _has_wake(text: str) -> bool:
    n = _norm_wake(text)
    return bool(n) and any(a in n for a in sorted(config.WAKE_WORD_ALIASES, key=len, reverse=True))


def listen_chunk(sec: float | None = None) -> str:
    return transcribe(_record(sec or config.WAKE_CHUNK_SEC))


def listen_command() -> str:
    time.sleep(config.POST_TTS_DELAY_SEC)
    return transcribe(record_continuous())


def wait_for_wake_word() -> bool:
    try:
        t = listen_chunk(config.WAKE_CHUNK_SEC)
        if _has_wake(t):
            logger.info("wake: %r", t)
            if _on_wake:
                try:
                    _on_wake()
                except Exception:
                    pass
            return True
        return False
    except Exception as exc:
        logger.error("wake: %s", exc)
        time.sleep(config.WAKE_POLL_INTERVAL)
        return False


async def _edge(text: str, path: Path) -> bool:
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
    s = text.replace("'", "''")
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command",
            f"Add-Type -AssemblyName System.Speech; (New-Object System.Speech.Synthesis.SpeechSynthesizer).Speak('{s}')"],
            check=True, capture_output=True, timeout=90)
        return True
    except Exception:
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
        if asyncio.run(_edge(text, tmp)):
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