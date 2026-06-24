"""
Голосовой модуль Windy AI Assistant.

Возможности:
  - Wake-word detection (faster-whisper)
  - Continuous VAD v2: RMS + energy + peak, pre-roll, attack/release, hangover
  - Параллельная запись во время TTS «Слушаю» — не теряется начало фразы
  - TTS: edge-tts → SAPI (Unicode) → winsound
  - Callbacks для GUI (уровень микрофона, статус VAD)
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import re
import subprocess
import tempfile
import threading
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
    LEAD_IN = auto()       # микрофон открыт во время TTS
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


def _peak(audio: np.ndarray) -> float:
    if audio.size == 0:
        return 0.0
    return float(np.max(np.abs(audio)))


def _voice_level(audio: np.ndarray) -> float:
    """
    Комбинированный уровень громкости: RMS + sqrt(energy) + peak.
    Устойчив к фону, но чувствителен к тихим согласным и началу слов.
    """
    r, e, p = _rms(audio), _energy(audio), _peak(audio)
    w_e = config.VAD_ENERGY_WEIGHT
    w_p = config.VAD_PEAK_WEIGHT
    w_r = max(0.0, 1.0 - w_e - w_p)
    return w_r * r + w_e * float(np.sqrt(e)) + w_p * p


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
            _on_mic_level(min(1.0, level * 22.0))
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


# ── Continuous VAD v2 ─────────────────────────────────────────────────────────

class ContinuousVAD:
    """
    Voice Activity Detection для непрерывной записи после wake-word.

    Алгоритм:
      1. Lead-in — микрофон уже пишет во время TTS «Слушаю»
      2. Калибровка шума (медиана уровня фона)
      3. Pre-roll ring buffer — не обрезает начало фразы
      4. Attack — N чанков подряд выше порога → старт записи
      5. Release + hangover — короткие паузы внутри фразы не завершают запись
      6. Адаптивный noise floor во время ожидания/записи
      7. Мягкая обрезка хвостовой тишины + end padding
    """

    def __init__(self, *, lead_in_sec: float = 0.0) -> None:
        self.chunk_n = max(32, int(config.SAMPLE_RATE * config.VAD_CHUNK_MS / 1000))
        self.dt = self.chunk_n / config.SAMPLE_RATE

        speech_m, silence_m, release_m, attack_m = config.vad_sensitivity_scale()
        self.release_sec = config.vad_release_sec()
        self.release_chunks = max(6, int(self.release_sec / self.dt))
        self.hang_extra = max(3, int(config.VAD_HANGOVER_SEC / self.dt))
        self.long_speech_bonus_chunks = max(0, int(config.VAD_LONG_SPEECH_BONUS_SEC / self.dt))

        attack_sec = max(0.08, config.VAD_ATTACK_SEC * attack_m)
        self.attack_need = max(2, int(attack_sec / self.dt))

        pre_sec = config.VAD_PRE_ROLL_SEC + max(0.0, lead_in_sec)
        self.pre_max = max(2, int(pre_sec / self.dt))

        self.noise_floor = 0.0
        self.thr_on = config.VAD_SPEECH_THRESHOLD * speech_m
        self.thr_off = config.VAD_SILENCE_THRESHOLD * silence_m

        self.recorded: list[np.ndarray] = []
        self.pre_roll: deque[np.ndarray] = deque(maxlen=self.pre_max)
        self.smooth_buf: deque[float] = deque(maxlen=config.VAD_RMS_SMOOTH_WINDOW)

        self.phase = VADPhase.LEAD_IN if lead_in_sec > 0 else VADPhase.CALIBRATING
        self.lead_in_left = max(0.0, lead_in_sec)
        self.started = False
        self.speech_streak = 0
        self.speech_t = 0.0
        self.wait_t = 0.0
        self.total_t = 0.0
        self.silent_run = 0
        self.last_voice_t = 0.0
        self.calib_chunks = max(2, int(config.VAD_NOISE_CALIBRATION_SEC / self.dt))
        self.calib_done = 0
        self.calib_levels: list[float] = []
        self._last_debug_t = 0.0
        self._end_reason = ""

    def _update_thresholds(self) -> None:
        speech_m, silence_m, _, _ = config.vad_sensitivity_scale()
        base = max(self.noise_floor, 1e-5)
        self.thr_on = max(config.VAD_SPEECH_THRESHOLD * speech_m, base * config.VAD_NOISE_MULT_ON)
        self.thr_off = max(
            config.VAD_SILENCE_THRESHOLD * silence_m,
            base * config.VAD_NOISE_MULT_OFF,
            self.thr_on * 0.55,  # гистерезис: off < on
        )
        logger.info(
            "VAD calibrated: noise=%.5f thr_on=%.5f thr_off=%.5f release=%.1fs hang=%.1fs",
            self.noise_floor, self.thr_on, self.thr_off, self.release_sec, config.VAD_HANGOVER_SEC,
        )

    def _adapt_noise(self, level: float) -> None:
        """Медленная подстройка порога к изменению фонового шума."""
        alpha = config.VAD_ADAPTIVE_NOISE_ALPHA
        if alpha <= 0:
            return
        self.noise_floor = (1.0 - alpha) * self.noise_floor + alpha * level
        self._update_thresholds()

    def _calibrate_sample(self, level: float, *, defer_waiting: bool = False) -> bool:
        """Собрать фоновый шум. defer_waiting=True — не выходить из lead-in раньше TTS."""
        self.calib_levels.append(level)
        self.calib_done += 1
        if self.calib_done < self.calib_chunks:
            return False
        self.noise_floor = float(np.percentile(self.calib_levels, 30)) if self.calib_levels else level
        self._update_thresholds()
        if not defer_waiting:
            self.phase = VADPhase.WAITING
            _emit_vad("waiting")
        return True

    def _debug_log(self, level: float) -> None:
        if not config.VAD_DEBUG_LOG:
            return
        if self.total_t - self._last_debug_t < config.VAD_DEBUG_LOG_INTERVAL_SEC:
            return
        self._last_debug_t = self.total_t
        logger.info(
            "VAD [%s] lvl=%.5f on=%.5f off=%.5f streak=%d silent=%d speech=%.1fs",
            self.phase.name, level, self.thr_on, self.thr_off,
            self.speech_streak, self.silent_run, self.speech_t,
        )

    def _in_hangover(self) -> bool:
        return (self.total_t - self.last_voice_t) < config.VAD_HANGOVER_SEC

    def _release_threshold_chunks(self) -> int:
        """Сколько тихих чанков нужно для завершения записи."""
        extra = self.hang_extra if self._in_hangover() else 0
        if self.speech_t >= 5.0:
            extra += self.long_speech_bonus_chunks
        return self.release_chunks + extra

    def _begin_recording(self, level: float) -> None:
        self.started = True
        self.phase = VADPhase.RECORDING
        self.recorded.extend(self.pre_roll)
        self.pre_roll.clear()
        self.speech_t = 0.0
        self.silent_run = 0
        self.last_voice_t = self.total_t
        self.speech_streak = 0
        _emit_vad("recording")
        logger.info("VAD speech start level=%.5f pre_roll=%.2fs", level, len(self.recorded) * self.dt)

    def _finish(self, reason: str) -> bool:
        self.phase = VADPhase.DONE
        self._end_reason = reason
        _emit_vad(reason)
        dur = len(self.recorded) * self.dt if self.recorded else 0.0
        logger.info(
            "VAD end [%s] speech=%.1fs recorded=%.1fs silent_run=%d",
            reason, self.speech_t, dur, self.silent_run,
        )
        return True

    def process_chunk(self, data: np.ndarray) -> bool:
        """Обработать один аудио-чанк. True = запись завершена."""
        level = _smooth(_voice_level(data), self.smooth_buf)
        _emit_mic(level)
        self.total_t += self.dt
        self._debug_log(level)

        # ── Lead-in: пишем pre-roll пока играет TTS ──
        if self.phase == VADPhase.LEAD_IN:
            self.pre_roll.append(data)
            self.lead_in_left -= self.dt
            self._calibrate_sample(level, defer_waiting=True)
            if self.lead_in_left <= 0:
                if self.calib_done < self.calib_chunks:
                    self.phase = VADPhase.CALIBRATING
                    _emit_vad("calibrating")
                else:
                    self.phase = VADPhase.WAITING
                    _emit_vad("waiting")
            return False

        # ── Калибровка шума ──
        if self.phase == VADPhase.CALIBRATING:
            self.pre_roll.append(data)
            if self._calibrate_sample(level):
                pass
            return False

        # ── Ожидание начала речи (attack) ──
        if not self.started:
            self.wait_t += self.dt
            self.pre_roll.append(data)

            if level >= self.thr_on:
                self.speech_streak += 1
            else:
                self.speech_streak = max(0, self.speech_streak - 1)
                self._adapt_noise(level)

            if self.speech_streak >= self.attack_need:
                self._begin_recording(level)
            elif self.wait_t >= config.VAD_WAIT_SPEECH_SEC:
                return self._finish("timeout")
            return False

        # ── Активная запись (release + hangover) ──
        self.recorded.append(data)
        self.speech_t += self.dt

        if level >= self.thr_off:
            self.silent_run = 0
            self.last_voice_t = self.total_t
        else:
            self.silent_run += 1
            if self.silent_run % 8 == 0:
                self._adapt_noise(level)

        if self.silent_run >= self._release_threshold_chunks() and self.speech_t >= config.VAD_MIN_SPEECH_SEC:
            return self._finish("done")

        if self.total_t >= config.VAD_MAX_RECORD_SEC:
            return self._finish("max_duration")

        return False

    def _trim_trailing_silence(self, audio: np.ndarray) -> np.ndarray:
        """Мягкая обрезка хвоста — не срезаем конец слов."""
        if not config.VAD_TRIM_TRAILING or audio.size < self.chunk_n * 3:
            return audio

        levels: list[float] = []
        for i in range(0, len(audio) - self.chunk_n, self.chunk_n):
            levels.append(_voice_level(audio[i : i + self.chunk_n]))

        cut_chunks = 0
        for lvl in reversed(levels):
            if lvl < self.thr_off:
                cut_chunks += 1
            else:
                break

        # Оставляем 40% хвостовой тишины + end padding
        keep_tail = max(1, int(0.4 / self.dt))
        cut_chunks = max(0, cut_chunks - keep_tail)
        if cut_chunks <= 0:
            return audio

        cut_samples = cut_chunks * self.chunk_n
        pad = int(config.VAD_END_PADDING_SEC * config.SAMPLE_RATE)
        end = max(self.chunk_n * 2, len(audio) - cut_samples + pad)
        return audio[: min(len(audio), end)]

    def get_audio(self) -> np.ndarray:
        if not self.recorded:
            return np.array([], dtype=np.float32)
        audio = np.concatenate(self.recorded)
        audio = self._trim_trailing_silence(audio)
        out = _normalize(_highpass(audio))
        logger.info(
            "VAD audio ready: %.2fs rms=%.5f reason=%s",
            out.size / config.SAMPLE_RATE, _rms(out), self._end_reason or "—",
        )
        return out


def record_continuous(*, lead_in_sec: float = 0.0) -> np.ndarray:
    """Запись команды с continuous VAD. lead_in_sec — буфер во время TTS."""
    vad = ContinuousVAD(lead_in_sec=lead_in_sec)
    if vad.phase == VADPhase.CALIBRATING:
        _emit_vad("calibrating")
    elif vad.phase == VADPhase.LEAD_IN:
        _emit_vad("lead_in")

    chunk_n = vad.chunk_n
    try:
        time.sleep(config.VAD_MIC_WARMUP_SEC)
        with sd.InputStream(blocksize=chunk_n, **_sd_kwargs()) as stream:
            while vad.phase != VADPhase.DONE:
                chunk, overflowed = stream.read(chunk_n)
                if overflowed:
                    logger.warning("audio buffer overflow — увеличь buffer в ОС")
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
        vad_parameters={
            "min_silence_duration_ms": 500,
            "speech_pad_ms": 600,
            "threshold": 0.35,
        },
        no_speech_threshold=min(config.WHISPER_NO_SPEECH_THRESHOLD, 0.55),
        condition_on_previous_text=False,
        temperature=0.0,
    )
    text = _clean_text(" ".join(s.text.strip() for s in segments))
    dur = getattr(info, "duration", 0) or 0
    logger.info("STT [%.1fs vad=%s]: %r", dur, vad_filter, text)
    return text


def transcribe(audio: np.ndarray) -> str:
    if audio is None or audio.size < int(config.SAMPLE_RATE * 0.12):
        logger.debug("STT: audio too short (%.2fs)", (audio.size if audio is not None else 0) / config.SAMPLE_RATE)
        return ""

    audio = _normalize(_highpass(audio))
    rms = _rms(audio)
    if rms < config.VAD_SILENCE_THRESHOLD * 0.06:
        logger.debug("STT: audio too quiet rms=%.5f", rms)
        return ""

    # Padding — Whisper лучше распознаёт длинные фразы с паузами по краям
    pad = int(config.SAMPLE_RATE * 0.35)
    audio_padded = np.pad(audio, (pad, pad), mode="constant")

    try:
        # Сначала без whisper-vad — наш VAD уже выделил речь
        for attempt, (data, use_vad) in enumerate([
            (audio_padded, False),
            (audio, False),
            (audio_padded, True),
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
    """Continuous VAD без TTS (если «Слушаю» уже произнесено)."""
    delay = max(0.0, config.POST_TTS_DELAY_SEC)
    if delay > 0:
        time.sleep(delay)
    return transcribe(record_continuous())


def listen_after_wake() -> str:
    """
    TTS «Слушаю» + continuous VAD параллельно.
    Микрофон открывается сразу — начало длинной команды не теряется.
    """
    audio_box: list[np.ndarray] = []
    err_box: list[Exception] = []

    def _record_worker() -> None:
        try:
            audio_box.append(record_continuous(lead_in_sec=config.VAD_TTS_OVERLAP_SEC))
        except Exception as exc:
            err_box.append(exc)

    rec = threading.Thread(target=_record_worker, daemon=True, name="vad-parallel")
    rec.start()
    time.sleep(config.VAD_MIC_WARMUP_SEC)
    speak(config.CONFIRM_WAKE)
    rec.join(timeout=config.VAD_MAX_RECORD_SEC + 15.0)

    if err_box:
        logger.error("listen_after_wake record: %s", err_box[0])
        return ""

    if not audio_box:
        logger.warning("listen_after_wake: no audio captured")
        return ""

    return transcribe(audio_box[0])


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
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result(timeout=config.TTS_EDGE_TIMEOUT_SEC)


def _edge_tts_to_file(text: str, path: Path) -> bool:
    for attempt in range(config.TTS_EDGE_RETRIES):
        try:
            if _run_async_safe(_edge_tts_save(text, path)):
                logger.debug("edge-tts OK attempt %d", attempt + 1)
                return True
        except Exception as exc:
            logger.warning("edge-tts attempt %d/%d: %s", attempt + 1, config.TTS_EDGE_RETRIES, exc)
            time.sleep(0.5 * (attempt + 1))
    return False


def _sapi_speak(text: str) -> bool:
    """SAPI fallback с поддержкой кириллицы через временный UTF-8 файл."""
    if not config.TTS_USE_SAPI_FALLBACK:
        return False

    txt_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8", dir=config.TEMP_DIR
        ) as f:
            f.write(text)
            txt_path = Path(f.name)

        rate = config.TTS_SAPI_RATE
        ps = (
            "Add-Type -AssemblyName System.Speech; "
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            f"$s.Rate = {rate}; "
            f"$t = Get-Content -LiteralPath '{txt_path}' -Encoding UTF8 -Raw; "
            "$s.Speak($t);"
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            check=True, capture_output=True, timeout=120,
        )
        logger.info("TTS via SAPI (Unicode)")
        return True
    except Exception as exc:
        logger.warning("SAPI TTS failed: %s", exc)
        return False
    finally:
        if txt_path and txt_path.exists():
            try:
                txt_path.unlink()
            except OSError:
                pass


def _play_mp3(path: Path) -> bool:
    try:
        from playsound3 import playsound
        playsound(str(path), block=True)
        return True
    except Exception as exc:
        logger.warning("playsound failed: %s — trying winsound", exc)
    try:
        import winsound
        winsound.PlaySound(str(path), winsound.SND_FILENAME)
        return True
    except Exception as exc:
        logger.warning("winsound failed: %s", exc)
        return False


def speak(text: str) -> None:
    if not text or not text.strip():
        return
    text = text.strip()
    logger.info("TTS: %r", text[:80])
    tmp: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False, dir=config.TEMP_DIR) as f:
            tmp = Path(f.name)
        if _edge_tts_to_file(text, tmp) and _play_mp3(tmp):
            return
        logger.info("TTS fallback → SAPI")
        if _sapi_speak(text):
            return
        logger.error("TTS: all methods failed for %r", text[:60])
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
    return _edge_tts_to_file(text, path)


# ── Публичный API ─────────────────────────────────────────────────────────────

class VoiceEngine:
    def wait_for_wake_word(self) -> bool:
        return wait_for_wake_word()

    def listen_command(self) -> str:
        return listen_command()

    def listen_after_wake(self) -> str:
        return listen_after_wake()

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