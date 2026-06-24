"""
Голосовой модуль Windy AI Assistant.

Возможности:
  - Wake-word detection (faster-whisper)
  - Continuous VAD: RMS + energy, pre-roll, hangover
  - TTS: edge-tts + SAPI fallback
  - Callbacks для GUI (уровень микрофона, статус VAD)
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import re
import subprocess
import tempfile
import time
from collections import deque
from enum import Enum, auto
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np
import sounddevice as sd
from scipy.signal import butter, filtfilt

import bootstrap  # noqa: F401
import config

logger = logging.getLogger(__name__)

# ── Глобальное состояние ──────────────────────────────────────────────────────
_whisper_model = None
_whisper_key: tuple | None = None
_on_wake: Callable[[], None] | None = None
_on_mic_level: Callable[[float], None] | None = None
_on_vad_state: Callable[[str], None] | None = None
_force_wake = False
_FILLER = re.compile(r"\b(э+|мм+|ну+|ага|угу|типа|как бы)\b", re.IGNORECASE)


class VADPhase(Enum):
    """Фазы continuous listening."""
    CALIBRATING = auto()
    WAITING = auto()
    RECORDING = auto()
    DONE = auto()


# ── Callbacks ─────────────────────────────────────────────────────────────────

def set_wake_callback(cb: Callable[[], None] | None) -> None:
    global _on_wake
    _on_wake = cb


def set_mic_level_callback(cb: Callable[[float], None] | None) -> None:
    global _on_mic_level
    _on_mic_level = cb


def set_vad_state_callback(cb: Callable[[str], None] | None) -> None:
    global _on_vad_state
    _on_vad_state = cb


def trigger_force_wake() -> None:
    """Принудительный wake без произнесения wake-word (кнопка GUI)."""
    global _force_wake
    _force_wake = True


def consume_force_wake() -> bool:
    global _force_wake
    if _force_wake:
        _force_wake = False
        return True
    return False


def reset_whisper_model() -> None:
    global _whisper_model, _whisper_key
    _whisper_model = _whisper_key = None


# ── Whisper ───────────────────────────────────────────────────────────────────

def _load_whisper():
    from faster_whisper import WhisperModel
    err: Exception | None = None
    for dev, comp in config.resolve_whisper_backend():
        try:
            logger.info("Whisper loading: device=%s compute=%s", dev, comp)
            model = WhisperModel(config.WHISPER_MODEL, device=dev, compute_type=comp)
            return model, dev, comp
        except Exception as exc:
            err = exc
            logger.warning("Whisper %s/%s failed: %s", dev, comp, exc)
    raise RuntimeError(f"Whisper load failed: {err}")


def _get_whisper():
    global _whisper_model, _whisper_key
    key = (config.WHISPER_MODEL, config.WHISPER_DEVICE, config.WHISPER_COMPUTE_TYPE)
    if _whisper_model and _whisper_key == key:
        return _whisper_model
    model, dev, comp = _load_whisper()
    _whisper_model = model
    _whisper_key = (config.WHISPER_MODEL, dev, comp)
    logger.info("Whisper ready: %s on %s/%s", config.WHISPER_MODEL, dev, comp)
    return _whisper_model


# ── Аудио-утилиты ─────────────────────────────────────────────────────────────

def _rms(audio: np.ndarray) -> float:
    if audio.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(audio, dtype=np.float64))))


def _energy(audio: np.ndarray) -> float:
    if audio.size == 0:
        return 0.0
    return float(np.mean(np.square(audio, dtype=np.float64)))


def _voice_level(audio: np.ndarray) -> float:
    """Комбинированный уровень: RMS + sqrt(energy) — устойчивее к фоновому шуму."""
    r, e = _rms(audio), _energy(audio)
    w = config.VAD_ENERGY_WEIGHT
    return (1.0 - w) * r + w * float(np.sqrt(e))


def _smooth(value: float, buf: deque[float]) -> float:
    buf.append(value)
    return float(np.mean(buf))


def _highpass(audio: np.ndarray) -> np.ndarray:
    """Фильтр НЧ-шума (~80 Hz)."""
    try:
        if audio.size < 64:
            return audio
        nyq = config.SAMPLE_RATE / 2.0
        b, a = butter(2, max(80.0 / nyq, 0.001), btype="high")
        return filtfilt(b, a, audio).astype(np.float32)
    except Exception as exc:
        logger.debug("highpass skip: %s", exc)
        return audio


def _normalize(audio: np.ndarray, target: float = 0.05) -> np.ndarray:
    r = _rms(audio)
    if r <= 1e-6:
        return audio
    return np.clip(audio * min(target / r, 10.0), -1.0, 1.0).astype(np.float32)


def _emit_mic(level: float) -> None:
    if _on_mic_level:
        try:
            _on_mic_level(min(1.0, level * 25.0))
        except Exception:
            pass


def _emit_vad(state: str) -> None:
    if _on_vad_state:
        try:
            _on_vad_state(state)
        except Exception:
            pass


def _sd_kwargs() -> dict:
    kw: dict = {
        "samplerate": config.SAMPLE_RATE,
        "channels": config.CHANNELS,
        "dtype": config.DTYPE,
    }
    if config.MIC_DEVICE_ID is not None:
        kw["device"] = config.MIC_DEVICE_ID
    return kw


def _record_fixed(sec: float, *, emit_levels: bool = False) -> np.ndarray:
    """Фиксированная запись; при emit_levels — live-уровень для GUI."""
    n = int(sec * config.SAMPLE_RATE)
    if n <= 0:
        return np.array([], dtype=np.float32)

    if not emit_levels:
        audio = sd.rec(
            n, **{k: v for k, v in _sd_kwargs().items() if k != "dtype"}, dtype=config.DTYPE
        )
        sd.wait()
        return audio.flatten().astype(np.float32)

    chunk_n = int(config.SAMPLE_RATE * 0.12)
    chunks: list[np.ndarray] = []
    remaining = n
    try:
        with sd.InputStream(blocksize=chunk_n, **_sd_kwargs()) as stream:
            while remaining > 0:
                take = min(chunk_n, remaining)
                data, _ = stream.read(take)
                flat = np.asarray(data, dtype=np.float32).flatten()
                chunks.append(flat)
                _emit_mic(_voice_level(flat))
                remaining -= take
    except Exception as exc:
        logger.error("stream record failed: %s", exc)
        return np.array([], dtype=np.float32)
    return np.concatenate(chunks) if chunks else np.array([], dtype=np.float32)


# ── Continuous VAD ────────────────────────────────────────────────────────────

class ContinuousVAD:
    """
    Voice Activity Detection для непрерывной записи после wake-word.

    Алгоритм:
      1. Калибровка шума (короткая пауза перед записью)
      2. Pre-roll ring buffer — не обрезает начало фразы
      3. Пороги on/off с гистерезисом (адаптивно к шуму + sensitivity)
      4. Hangover — короткие паузы внутри фразы не завершают запись
      5. N тихих чанков подряд → конец записи
    """

    def __init__(self) -> None:
        self.chunk_n = int(config.SAMPLE_RATE * config.VAD_CHUNK_MS / 1000)
        self.dt = config.VAD_CHUNK_MS / 1000.0
        self.pre_max = max(1, int(config.VAD_PRE_ROLL_SEC / self.dt))
        speech_m, silence_m, silence_sec_m = config.vad_sensitivity_scale()
        self.silence_sec = config.VAD_SILENCE_SEC * silence_sec_m
        self.silent_need = max(5, int(self.silence_sec / self.dt))
        self.hang_extra = max(2, int(config.VAD_HANGOVER_SEC / self.dt))

        self.noise_floor = 0.0
        self.thr_on = config.VAD_SPEECH_THRESHOLD * speech_m
        self.thr_off = config.VAD_SILENCE_THRESHOLD * silence_m

        self.recorded: list[np.ndarray] = []
        self.pre_roll: deque[np.ndarray] = deque(maxlen=self.pre_max)
        self.smooth_buf: deque[float] = deque(maxlen=config.VAD_RMS_SMOOTH_WINDOW)

        self.phase = VADPhase.CALIBRATING
        self.started = False
        self.speech_t = 0.0
        self.wait_t = 0.0
        self.total_t = 0.0
        self.silent_run = 0
        self.last_voice_t = 0.0
        self.calib_chunks = max(1, int(config.VAD_NOISE_CALIBRATION_SEC / self.dt))
        self.calib_done = 0
        self.calib_levels: list[float] = []

    def _update_thresholds(self) -> None:
        speech_m, silence_m, _ = config.vad_sensitivity_scale()
        base = max(self.noise_floor, 1e-5)
        self.thr_on = max(
            config.VAD_SPEECH_THRESHOLD * speech_m,
            base * config.VAD_NOISE_MULT_ON,
        )
        self.thr_off = max(
            config.VAD_SILENCE_THRESHOLD * silence_m,
            base * config.VAD_NOISE_MULT_OFF,
        )
        logger.debug("VAD thresholds: noise=%.5f on=%.5f off=%.5f", self.noise_floor, self.thr_on, self.thr_off)

    def _calibrate(self, level: float) -> bool:
        """Собираем уровень фонового шума перед ожиданием речи."""
        self.calib_levels.append(level)
        self.calib_done += 1
        if self.calib_done < self.calib_chunks:
            return False
        self.noise_floor = float(np.median(self.calib_levels)) if self.calib_levels else level
        self._update_thresholds()
        self.phase = VADPhase.WAITING
        _emit_vad("waiting")
        return True

    def _in_hangover(self) -> bool:
        """Hangover: недавно была речь — требуем больше тихих чанков для стопа."""
        return (self.total_t - self.last_voice_t) < config.VAD_HANGOVER_SEC

    def process_chunk(self, data: np.ndarray) -> bool:
        """
        Обработать один аудио-чанк.
        Возвращает True, если запись завершена.
        """
        level = _smooth(_voice_level(data), self.smooth_buf)
        _emit_mic(level)
        self.total_t += self.dt

        if self.phase == VADPhase.CALIBRATING:
            self.pre_roll.append(data)  # pre-roll с калибровки — не теряем начало фразы
            if self._calibrate(level):
                pass
            return False

        if not self.started:
            self.wait_t += self.dt
            self.pre_roll.append(data)  # pre-roll: сохраняем аудио до детекта речи
            if level >= self.thr_on:
                self.started = True
                self.phase = VADPhase.RECORDING
                self.recorded.extend(self.pre_roll)
                self.pre_roll.clear()
                self.speech_t = 0.0
                self.silent_run = 0
                self.last_voice_t = self.total_t
                _emit_vad("recording")
                logger.debug("VAD speech start level=%.5f", level)
            elif self.wait_t >= config.VAD_WAIT_SPEECH_SEC:
                self.phase = VADPhase.DONE
                _emit_vad("timeout")
                return True
            return False

        # Активная запись
        self.recorded.append(data)
        self.speech_t += self.dt

        if level < self.thr_off:
            self.silent_run += 1
        else:
            self.silent_run = 0
            self.last_voice_t = self.total_t

        need_silent = self.silent_need + (self.hang_extra if self._in_hangover() else 0)
        if self.silent_run >= need_silent and self.speech_t >= config.VAD_MIN_SPEECH_SEC:
            self.phase = VADPhase.DONE
            _emit_vad("done")
            logger.debug("VAD end speech=%.1fs silent_chunks=%d", self.speech_t, self.silent_run)
            return True

        if self.total_t >= config.VAD_MAX_RECORD_SEC:
            self.phase = VADPhase.DONE
            _emit_vad("max_duration")
            return True

        return False

    def _trim_trailing_silence(self, audio: np.ndarray) -> np.ndarray:
        """Убираем хвостовую тишину, оставляя hangover-паузу внутри фразы."""
        if audio.size < self.chunk_n * 2:
            return audio
        tail_allow = max(1, int(0.35 / self.dt))  # ~350 мс тишины в конце
        levels = []
        for i in range(0, len(audio) - self.chunk_n, self.chunk_n):
            levels.append(_voice_level(audio[i : i + self.chunk_n]))
        cut_chunks = 0
        for lvl in reversed(levels):
            if lvl < self.thr_off:
                cut_chunks += 1
            else:
                break
        cut_chunks = max(0, cut_chunks - tail_allow)
        if cut_chunks <= 0:
            return audio
        cut_samples = cut_chunks * self.chunk_n
        return audio[: max(self.chunk_n, len(audio) - cut_samples)]

    def get_audio(self) -> np.ndarray:
        if not self.recorded:
            return np.array([], dtype=np.float32)
        audio = self._trim_trailing_silence(np.concatenate(self.recorded))
        return _normalize(_highpass(audio))


def record_continuous() -> np.ndarray:
    """Запись команды после wake-word с continuous VAD."""
    vad = ContinuousVAD()
    _emit_vad("calibrating")
    chunk_n = vad.chunk_n

    try:
        with sd.InputStream(blocksize=chunk_n, **_sd_kwargs()) as stream:
            while vad.phase != VADPhase.DONE:
                chunk, overflowed = stream.read(chunk_n)
                if overflowed:
                    logger.warning("audio buffer overflow")
                data = np.asarray(chunk, dtype=np.float32).flatten()
                if vad.process_chunk(data):
                    break
    except Exception as exc:
        logger.error("record_continuous failed: %s", exc)
        _emit_vad("error")
        return np.array([], dtype=np.float32)

    return vad.get_audio()


# ── STT ───────────────────────────────────────────────────────────────────────

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


def _transcribe_once(audio: np.ndarray, *, vad_filter: bool) -> str:
    segments, info = _get_whisper().transcribe(
        audio,
        language=config.WHISPER_LANGUAGE,
        beam_size=config.WHISPER_BEAM_SIZE,
        best_of=config.WHISPER_BEST_OF,
        vad_filter=vad_filter,
        vad_parameters={"min_silence_duration_ms": 350, "speech_pad_ms": 450},
        no_speech_threshold=min(config.WHISPER_NO_SPEECH_THRESHOLD, 0.55),
        condition_on_previous_text=False,
        temperature=0.0,
    )
    text = _clean_text(" ".join(s.text.strip() for s in segments))
    dur = getattr(info, "duration", 0) or 0
    logger.info("STT [%.1fs vad=%s]: %r", dur, vad_filter, text)
    return text


def transcribe(audio: np.ndarray) -> str:
    if audio is None or audio.size < int(config.SAMPLE_RATE * 0.15):
        logger.debug("STT: audio too short")
        return ""

    audio = _normalize(_highpass(audio))
    rms = _rms(audio)
    if rms < config.VAD_SILENCE_THRESHOLD * 0.08:
        logger.debug("STT: audio too quiet rms=%.5f", rms)
        return ""

    # Padding — Whisper лучше распознаёт с небольшими паузами по краям
    pad = int(config.SAMPLE_RATE * 0.25)
    audio_padded = np.pad(audio, (pad, pad), mode="constant")

    try:
        for attempt, (data, use_vad) in enumerate([
            (audio_padded, True),
            (audio_padded, False),
            (audio, False),
        ]):
            try:
                text = _transcribe_once(data, vad_filter=use_vad)
                if text:
                    return text
                logger.warning("STT empty attempt %d (vad=%s)", attempt, use_vad)
            except Exception as exc:
                logger.warning("STT attempt %d failed: %s", attempt, exc)
                reset_whisper_model()
        return ""
    except Exception as exc:
        logger.error("STT error: %s", exc)
        reset_whisper_model()
        return ""


# ── Wake-word ─────────────────────────────────────────────────────────────────

def _norm_wake(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^\w\sа-яё]", " ", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip()


def _has_wake(text: str) -> bool:
    norm = _norm_wake(text)
    if not norm:
        return False
    return any(alias in norm for alias in sorted(config.WAKE_WORD_ALIASES, key=len, reverse=True))


def listen_chunk(sec: float | None = None) -> str:
    return transcribe(_record_fixed(sec or config.WAKE_CHUNK_SEC, emit_levels=True))


def listen_command() -> str:
    """Пауза после TTS «Слушаю», затем continuous VAD + STT."""
    time.sleep(config.POST_TTS_DELAY_SEC)
    return transcribe(record_continuous())


def wait_for_wake_word() -> bool:
    """Ожидание wake-word или force wake (GUI)."""
    if consume_force_wake():
        logger.info("force wake triggered")
        if _on_wake:
            try:
                _on_wake()
            except Exception:
                pass
        return True

    try:
        text = listen_chunk(config.WAKE_CHUNK_SEC)
        if _has_wake(text):
            logger.info("wake-word detected: %r", text)
            if _on_wake:
                try:
                    _on_wake()
                except Exception:
                    pass
            return True
        return False
    except Exception as exc:
        logger.error("wake-word error: %s", exc)
        time.sleep(config.WAKE_POLL_INTERVAL)
        return False


# ── TTS ───────────────────────────────────────────────────────────────────────

async def _edge_tts_save(text: str, path: Path) -> bool:
    import edge_tts
    comm = edge_tts.Communicate(
        text, voice=config.TTS_VOICE, rate=config.TTS_RATE, volume=config.TTS_VOLUME
    )
    await comm.save(str(path))
    return path.exists() and path.stat().st_size > 512


def _run_async_safe(coro) -> Any:
    """Запуск coroutine в отдельном потоке — обход конфликтов event loop."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result(timeout=90)


def _edge_tts_to_file(text: str, path: Path) -> bool:
    for attempt in range(config.TTS_EDGE_RETRIES):
        try:
            if _run_async_safe(_edge_tts_save(text, path)):
                return True
        except Exception as exc:
            logger.warning("edge-tts attempt %d: %s", attempt + 1, exc)
            time.sleep(0.4 * (attempt + 1))
    return False


def _sapi_speak(text: str) -> bool:
    if not config.TTS_USE_SAPI_FALLBACK:
        return False
    safe = text.replace("'", "''")
    try:
        subprocess.run(
            [
                "powershell", "-NoProfile", "-Command",
                f"Add-Type -AssemblyName System.Speech; "
                f"(New-Object System.Speech.Synthesis.SpeechSynthesizer).Speak('{safe}')",
            ],
            check=True,
            capture_output=True,
            timeout=90,
        )
        return True
    except Exception as exc:
        logger.warning("SAPI TTS failed: %s", exc)
        return False


def _play_mp3(path: Path) -> bool:
    try:
        from playsound3 import playsound
        playsound(str(path), block=True)
        return True
    except Exception as exc:
        logger.warning("playsound failed: %s", exc)
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
        if _edge_tts_to_file(text, tmp) and _play_mp3(tmp):
            return
        logger.info("TTS fallback → SAPI")
        if _sapi_speak(text):
            return
        logger.error("TTS: all methods failed")
    except Exception as exc:
        logger.error("TTS error: %s", exc)
        _sapi_speak(text)
    finally:
        if tmp and tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def synthesize_to_file(text: str, path: Path) -> bool:
    """Синтез речи в файл (для telegram_send_voice)."""
    return _edge_tts_to_file(text, path)


# ── Публичный API ─────────────────────────────────────────────────────────────

class VoiceEngine:
    def wait_for_wake_word(self) -> bool:
        return wait_for_wake_word()

    def listen_command(self) -> str:
        return listen_command()

    def speak(self, text: str) -> None:
        speak(text)

    def record_continuous(self) -> np.ndarray:
        return record_continuous()

    def trigger_force_wake(self) -> None:
        trigger_force_wake()

    def set_mic_callback(self, cb: Callable[[float], None] | None) -> None:
        set_mic_level_callback(cb)

    def set_vad_callback(self, cb: Callable[[str], None] | None) -> None:
        set_vad_state_callback(cb)

    def get_whisper_status(self) -> str:
        try:
            _get_whisper()
            return f"{config.WHISPER_MODEL} ({config.WHISPER_DEVICE}/int8)"
        except Exception as exc:
            return f"ошибка: {exc}"

    def reload(self) -> None:
        config.reload_settings()
        reset_whisper_model()