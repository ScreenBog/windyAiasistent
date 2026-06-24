"""
Голосовой модуль Windy AI Assistant v10 — архитектура «умной колонки».

Режимы:
  IDLE (low-power)  — маленькие чанки, energy gate + WebRTC, STT только при речи
  ACTIVE (post-wake)— полный hybrid VAD + end-of-speech + шумоподавление → Whisper

Backends (опционально, с fallback):
  - webrtcvad      — детекция речи в idle и active
  - noisereduce    — спектральное шумоподавление
  - openwakeword   — опциональный wake (нужна своя модель)
  - Silero VAD     — опционально (torch)
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

# ── Опциональные backends ─────────────────────────────────────────────────────

def _try_import(name: str):
    try:
        return __import__(name)
    except ImportError:
        return None


_HAVE_WEBRTC = _try_import("webrtcvad") is not None
_HAVE_NR = _try_import("noisereduce") is not None
_HAVE_OWW = _try_import("openwakeword") is not None

_whisper_model = None
_whisper_key: tuple | None = None
_on_wake: Callable[[], None] | None = None
_on_mic_level: Callable[[float], None] | None = None
_on_vad_state: Callable[[str], None] | None = None
_force_wake = False
_silero_model: Any = None
_silero_utils: Any = None

_FILLER = re.compile(r"\b(э+|мм+|ну+|ага|угу|типа|как бы)\b", re.IGNORECASE)


class ListeningMode(Enum):
    IDLE = "idle"       # low-power: только wake-word
    ACTIVE = "active"   # полный VAD после wake


class VADPhase(Enum):
    LEAD_IN = auto()
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


def get_voice_backends() -> dict[str, bool]:
    return {
        "webrtcvad": _HAVE_WEBRTC,
        "noisereduce": _HAVE_NR,
        "openwakeword": _HAVE_OWW,
        "silero": config.SILERO_VAD_ENABLED,
    }


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
    r, e, p = _rms(audio), _energy(audio), _peak(audio)
    w_e = config.VAD_ENERGY_WEIGHT
    w_p = config.VAD_PEAK_WEIGHT
    w_r = max(0.0, 1.0 - w_e - w_p)
    return w_r * r + w_e * float(np.sqrt(e)) + w_p * p


def _smooth(value: float, buf: deque[float]) -> float:
    buf.append(value)
    return float(np.mean(buf))


def _highpass(audio: np.ndarray) -> np.ndarray:
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


def _to_pcm16(audio: np.ndarray) -> bytes:
    return (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16).tobytes()


def _emit_mic(level: float, *, idle: bool = False) -> None:
    if _on_mic_level:
        try:
            scale = 14.0 if idle else 22.0
            _on_mic_level(min(1.0, level * scale))
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


# ── Шумоподавление ────────────────────────────────────────────────────────────

class NoiseReducer:
    """Спектральное шумоподавление (noisereduce) с профилем из калибровки."""

    def __init__(self) -> None:
        self.noise_profile: np.ndarray | None = None

    def set_noise_profile(self, audio: np.ndarray) -> None:
        if audio is not None and audio.size > int(config.SAMPLE_RATE * 0.2):
            self.noise_profile = audio.copy()

    def process(self, audio: np.ndarray) -> np.ndarray:
        if not config.NOISE_REDUCE_ENABLED or not _HAVE_NR or audio.size == 0:
            return audio
        try:
            import noisereduce as nr
            y_noise = self.noise_profile
            if y_noise is None or y_noise.size < 100:
                y_noise = audio[: min(len(audio), int(config.SAMPLE_RATE * 0.4))]
            out = nr.reduce_noise(
                y=audio,
                sr=config.SAMPLE_RATE,
                y_noise=y_noise,
                stationary=config.NOISE_REDUCE_STATIONARY,
                prop_decrease=max(0.1, min(1.0, config.NOISE_REDUCE_PROP)),
            )
            return np.asarray(out, dtype=np.float32)
        except Exception as exc:
            logger.debug("noisereduce skip: %s", exc)
            return audio


# ── WebRTC VAD ────────────────────────────────────────────────────────────────

class WebRTCVADHelper:
    """webrtcvad: 30ms фреймы @ 16kHz."""

    FRAME_SAMPLES = 480  # 30ms @ 16kHz

    def __init__(self) -> None:
        self._vad = None
        if _HAVE_WEBRTC:
            import webrtcvad
            agg = max(0, min(3, config.WEBRTC_VAD_AGGRESSIVENESS))
            self._vad = webrtcvad.Vad(agg)

    @property
    def available(self) -> bool:
        return self._vad is not None

    def speech_ratio(self, audio: np.ndarray) -> float:
        if not self.available or audio.size < self.FRAME_SAMPLES:
            return 0.0
        pcm = _to_pcm16(audio)
        speech_frames = 0
        total_frames = 0
        frame_bytes = self.FRAME_SAMPLES * 2
        for i in range(0, len(pcm) - frame_bytes + 1, frame_bytes):
            frame = pcm[i : i + frame_bytes]
            try:
                if self._vad.is_speech(frame, config.SAMPLE_RATE):
                    speech_frames += 1
            except Exception:
                pass
            total_frames += 1
        return speech_frames / max(1, total_frames)

    def is_speech(self, audio: np.ndarray) -> bool:
        return self.speech_ratio(audio) >= config.WEBRTC_SPEECH_RATIO_ON


# ── Silero VAD (опционально) ──────────────────────────────────────────────────

class SileroVADHelper:
    def __init__(self) -> None:
        self._ready = False
        self._get_speech_timestamps = None

    def _ensure(self) -> bool:
        global _silero_model, _silero_utils
        if not config.SILERO_VAD_ENABLED:
            return False
        if self._ready:
            return True
        try:
            import torch
            _silero_model, _silero_utils = torch.hub.load(
                "snakers4/silero-vad", "silero_vad", trust_repo=True,
            )
            self._get_speech_timestamps = _silero_utils[0]
            self._ready = True
            logger.info("Silero VAD loaded")
            return True
        except Exception as exc:
            logger.warning("Silero VAD unavailable: %s", exc)
            return False

    def speech_probability(self, audio: np.ndarray) -> float:
        if not self._ensure() or audio.size < 512:
            return 0.0
        try:
            import torch
            wav = torch.from_numpy(audio.astype(np.float32))
            ts = self._get_speech_timestamps(
                wav, _silero_model,
                sampling_rate=config.SAMPLE_RATE,
                threshold=config.SILERO_VAD_THRESHOLD,
                min_speech_duration_ms=120,
                min_silence_duration_ms=80,
            )
            return 1.0 if ts else 0.0
        except Exception as exc:
            logger.debug("silero: %s", exc)
            return 0.0


# ── Hybrid speech detector ────────────────────────────────────────────────────

class HybridSpeechDetector:
    """
    Комбинирует RMS-порог + WebRTC + Silero для точного end-of-speech.
    """

    def __init__(self, *, thr_on: float, thr_off: float) -> None:
        self.thr_on = thr_on
        self.thr_off = thr_off
        self.webrtc = WebRTCVADHelper()
        self.silero = SileroVADHelper()

    def is_speech(self, audio: np.ndarray, level: float, *, for_start: bool = False) -> bool:
        backend = (config.VAD_BACKEND or "hybrid").lower()
        thr = self.thr_on if for_start else self.thr_off

        if backend == "rms":
            return level >= thr

        rms_hit = level >= thr
        webrtc_hit = self.webrtc.is_speech(audio) if self.webrtc.available else False
        silero_hit = (
            self.silero.speech_probability(audio) >= config.SILERO_VAD_THRESHOLD
            if config.SILERO_VAD_ENABLED else False
        )

        if backend == "webrtc":
            return webrtc_hit or rms_hit

        # hybrid: любой сильный сигнал + подтверждение WebRTC/Silero
        if rms_hit:
            return True
        if webrtc_hit and level >= thr * 0.65:
            return True
        if silero_hit:
            return True
        return False


# ── openWakeWord (опционально) ────────────────────────────────────────────────

class OpenWakeWordDetector:
    def __init__(self) -> None:
        self._model = None
        if config.OPENWAKEWORD_ENABLED and _HAVE_OWW:
            try:
                from openwakeword.model import Model
                if config.OPENWAKEWORD_MODEL:
                    self._model = Model(
                        wakeword_models=[config.OPENWAKEWORD_MODEL],
                        inference_framework="onnx",
                    )
                else:
                    self._model = Model(inference_framework="onnx")
                logger.info("openWakeWord loaded")
            except Exception as exc:
                logger.warning("openWakeWord init failed: %s", exc)

    def detect(self, audio: np.ndarray) -> bool:
        if self._model is None or audio.size == 0:
            return False
        try:
            pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
            scores = self._model.predict(pcm)
            for _, score in (scores or {}).items():
                if float(score) >= config.OPENWAKEWORD_THRESHOLD:
                    return True
        except Exception as exc:
            logger.debug("oww: %s", exc)
        return False


# ── Low-power wake listener ───────────────────────────────────────────────────

class LowPowerWakeListener:
    """
    IDLE режим: постоянный поток микрофона, energy gate + WebRTC.
    Whisper STT только когда в буфере есть речь — минимум ложных срабатываний.
    """

    def __init__(self) -> None:
        self.chunk_n = max(128, int(config.SAMPLE_RATE * config.WAKE_IDLE_CHUNK_MS / 1000))
        self.dt = self.chunk_n / config.SAMPLE_RATE
        self.webrtc = WebRTCVADHelper()
        self.oww = OpenWakeWordDetector()
        self.noise_floor = 0.008
        self.calibrated = False
        self.calib_levels: list[float] = []
        self.calib_need = max(2, int(config.WAKE_CALIBRATION_SEC / self.dt))
        self.buffer: deque[np.ndarray] = deque(
            maxlen=max(3, int(config.WAKE_BUFFER_SEC / self.dt))
        )

    def _energy_threshold(self) -> float:
        return max(
            config.VAD_SPEECH_THRESHOLD * 0.5,
            self.noise_floor * config.WAKE_MIN_ENERGY_MULT,
        )

    def _calibrate(self, level: float) -> None:
        self.calib_levels.append(level)
        if len(self.calib_levels) >= self.calib_need:
            self.noise_floor = float(np.percentile(self.calib_levels, 35))
            self.calibrated = True
            logger.info("idle calibrated: noise_floor=%.5f thr=%.5f", self.noise_floor, self._energy_threshold())

    def _chunk_passes_gate(self, chunk: np.ndarray, level: float) -> bool:
        if not self.calibrated:
            return False
        if config.WAKE_ENERGY_GATE and level < self._energy_threshold():
            return False
        if config.WAKE_SPEECH_REQUIRED and self.webrtc.available:
            if not self.webrtc.is_speech(chunk):
                return False
        return True

    def wait_for_wake(self) -> bool:
        if consume_force_wake():
            self._fire_wake("force")
            return True

        _emit_vad("idle")
        backend = config.WAKE_BACKEND
        logger.debug("idle listen: backend=%s chunk=%.0fms", backend, self.dt * 1000)

        try:
            time.sleep(config.VAD_MIC_WARMUP_SEC)
            with sd.InputStream(blocksize=self.chunk_n, **_sd_kwargs()) as stream:
                while True:
                    if consume_force_wake():
                        self._fire_wake("force")
                        return True

                    data, overflowed = stream.read(self.chunk_n)
                    if overflowed:
                        logger.warning("idle audio overflow")
                    chunk = np.asarray(data, dtype=np.float32).flatten()
                    chunk = _highpass(chunk)
                    level = _voice_level(chunk)
                    _emit_mic(level, idle=True)

                    if not self.calibrated:
                        self._calibrate(level)
                        continue

                    # openWakeWord на каждом чанке (лёгкий)
                    if config.OPENWAKEWORD_ENABLED and self.oww.detect(chunk):
                        self._fire_wake("openwakeword")
                        return True

                    if not self._chunk_passes_gate(chunk, level):
                        self.buffer.clear()
                        time.sleep(config.WAKE_POLL_INTERVAL * 0.5)
                        continue

                    self.buffer.append(chunk)
                    if len(self.buffer) < self.buffer.maxlen:
                        continue

                    audio = np.concatenate(list(self.buffer))
                    self.buffer.clear()
                    text = self._transcribe_wake(audio)
                    if _has_wake(text):
                        self._fire_wake(f"whisper:{text[:40]!r}")
                        return True

                    time.sleep(config.WAKE_POLL_INTERVAL)
        except Exception as exc:
            logger.error("idle listen error: %s", exc)
            time.sleep(config.WAKE_POLL_INTERVAL)
            return False

    def _transcribe_wake(self, audio: np.ndarray) -> str:
        audio = _normalize(_highpass(audio))
        try:
            segments, _ = _get_whisper().transcribe(
                audio,
                language=config.WHISPER_LANGUAGE,
                beam_size=3,
                best_of=2,
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 300, "speech_pad_ms": 200, "threshold": 0.4},
                no_speech_threshold=0.55,
                condition_on_previous_text=False,
                temperature=0.0,
            )
            return _clean_text(" ".join(s.text.strip() for s in segments))
        except Exception as exc:
            logger.warning("wake STT: %s", exc)
            reset_whisper_model()
            return ""

    def _fire_wake(self, reason: str) -> None:
        logger.info("wake detected [%s]", reason)
        _emit_vad("wake")
        if _on_wake:
            try:
                _on_wake()
            except Exception:
                pass


# ── Continuous VAD v3 (post-wake) ─────────────────────────────────────────────

class ContinuousVAD:
    """
    Полноценный VAD после wake-word: hybrid detector + end-of-speech.
    Pre-roll, hangover, адаптивный порог, шумоподавление перед Whisper.
    """

    def __init__(self, *, lead_in_sec: float = 0.0) -> None:
        self.chunk_n = max(32, int(config.SAMPLE_RATE * config.VAD_CHUNK_MS / 1000))
        self.dt = self.chunk_n / config.SAMPLE_RATE

        speech_m, silence_m, release_m, attack_m = config.vad_sensitivity_scale()
        self.release_sec = config.vad_release_sec() if config.END_OF_SPEECH_ENABLED else config.VAD_MAX_RECORD_SEC
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
        self.detector = HybridSpeechDetector(thr_on=self.thr_on, thr_off=self.thr_off)
        self.noise_reducer = NoiseReducer()

        self.recorded: list[np.ndarray] = []
        self.pre_roll: deque[np.ndarray] = deque(maxlen=self.pre_max)
        self.smooth_buf: deque[float] = deque(maxlen=config.VAD_RMS_SMOOTH_WINDOW)
        self.calib_audio: list[np.ndarray] = []

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
            self.thr_on * 0.55,
        )
        self.detector.thr_on = self.thr_on
        self.detector.thr_off = self.thr_off
        logger.info(
            "VAD calibrated: noise=%.5f thr_on=%.5f thr_off=%.5f release=%.1fs backend=%s",
            self.noise_floor, self.thr_on, self.thr_off, self.release_sec, config.VAD_BACKEND,
        )

    def _adapt_noise(self, level: float) -> None:
        alpha = config.VAD_ADAPTIVE_NOISE_ALPHA
        if alpha <= 0:
            return
        self.noise_floor = (1.0 - alpha) * self.noise_floor + alpha * level
        self._update_thresholds()

    def _calibrate_sample(self, level: float, chunk: np.ndarray, *, defer_waiting: bool = False) -> bool:
        self.calib_levels.append(level)
        self.calib_audio.append(chunk)
        self.calib_done += 1
        if self.calib_done < self.calib_chunks:
            return False
        self.noise_floor = float(np.percentile(self.calib_levels, 30)) if self.calib_levels else level
        self._update_thresholds()
        if self.calib_audio:
            prof = np.concatenate(self.calib_audio[-self.calib_chunks:])
            self.noise_reducer.set_noise_profile(prof)
        if not defer_waiting:
            self.phase = VADPhase.WAITING
            _emit_vad("waiting")
        return True

    def _debug_log(self, level: float, *, speech: bool) -> None:
        if not config.VAD_DEBUG_LOG:
            return
        if self.total_t - self._last_debug_t < config.VAD_DEBUG_LOG_INTERVAL_SEC:
            return
        self._last_debug_t = self.total_t
        wr = self.detector.webrtc.speech_ratio(self.recorded[-1]) if self.recorded else 0.0
        logger.info(
            "VAD [%s] lvl=%.5f speech=%s webrtc=%.2f on=%.5f off=%.5f silent=%d speech_t=%.1fs",
            self.phase.name, level, speech, wr, self.thr_on, self.thr_off,
            self.silent_run, self.speech_t,
        )

    def _in_hangover(self) -> bool:
        return (self.total_t - self.last_voice_t) < config.VAD_HANGOVER_SEC

    def _release_threshold_chunks(self) -> int:
        if not config.END_OF_SPEECH_ENABLED:
            return max(self.release_chunks, int(config.VAD_MAX_RECORD_SEC / self.dt))
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
        logger.info("VAD end-of-speech [%s] speech=%.1fs recorded=%.2fs", reason, self.speech_t, dur)
        return True

    def process_chunk(self, data: np.ndarray) -> bool:
        data = _highpass(data)
        level = _smooth(_voice_level(data), self.smooth_buf)
        is_speech = self.detector.is_speech(data, level, for_start=not self.started)
        _emit_mic(level)
        self.total_t += self.dt
        self._debug_log(level, speech=is_speech)

        if self.phase == VADPhase.LEAD_IN:
            self.pre_roll.append(data)
            self.lead_in_left -= self.dt
            self._calibrate_sample(level, data, defer_waiting=True)
            if self.lead_in_left <= 0:
                self.phase = VADPhase.CALIBRATING if self.calib_done < self.calib_chunks else VADPhase.WAITING
                _emit_vad(self.phase.name.lower())
            return False

        if self.phase == VADPhase.CALIBRATING:
            self.pre_roll.append(data)
            if self._calibrate_sample(level, data):
                pass
            return False

        if not self.started:
            self.wait_t += self.dt
            self.pre_roll.append(data)
            if is_speech:
                self.speech_streak += 1
            else:
                self.speech_streak = max(0, self.speech_streak - 1)
                self._adapt_noise(level)
            if self.speech_streak >= self.attack_need:
                self._begin_recording(level)
            elif self.wait_t >= config.VAD_WAIT_SPEECH_SEC:
                return self._finish("timeout")
            return False

        self.recorded.append(data)
        self.speech_t += self.dt

        if is_speech:
            self.silent_run = 0
            self.last_voice_t = self.total_t
        else:
            self.silent_run += 1
            if self.silent_run % 8 == 0:
                self._adapt_noise(level)

        if (
            config.END_OF_SPEECH_ENABLED
            and self.silent_run >= self._release_threshold_chunks()
            and self.speech_t >= config.VAD_MIN_SPEECH_SEC
        ):
            return self._finish("done")

        if self.total_t >= config.VAD_MAX_RECORD_SEC:
            return self._finish("max_duration")
        return False

    def _trim_trailing_silence(self, audio: np.ndarray) -> np.ndarray:
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
        audio = self.noise_reducer.process(audio)
        out = _normalize(_highpass(audio))
        logger.info(
            "VAD audio ready: %.2fs rms=%.5f reason=%s nr=%s",
            out.size / config.SAMPLE_RATE, _rms(out), self._end_reason or "—",
            config.NOISE_REDUCE_ENABLED and _HAVE_NR,
        )
        return out


def record_continuous(*, lead_in_sec: float = 0.0) -> np.ndarray:
    """ACTIVE режим: запись команды до end-of-speech."""
    _emit_vad("active")
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
                    logger.warning("active audio overflow")
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
        return ""
    audio = _normalize(_highpass(audio))
    if _rms(audio) < config.VAD_SILENCE_THRESHOLD * 0.06:
        return ""
    pad = int(config.SAMPLE_RATE * 0.35)
    audio_padded = np.pad(audio, (pad, pad), mode="constant")
    try:
        for attempt, (data, use_vad) in enumerate([
            (audio_padded, False),
            (audio, False),
            (audio_padded, True),
        ]):
            try:
                text = _transcribe_once(data, vad_filter=use_vad)
                if text:
                    return text
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
    config.ensure_wake_aliases()
    return any(alias in norm for alias in sorted(config.WAKE_WORD_ALIASES, key=len, reverse=True))


_idle_listener: LowPowerWakeListener | None = None


def _get_idle_listener() -> LowPowerWakeListener:
    global _idle_listener
    if _idle_listener is None:
        _idle_listener = LowPowerWakeListener()
    return _idle_listener


def wait_for_wake_word() -> bool:
    """IDLE low-power: слушает только wake-word."""
    return _get_idle_listener().wait_for_wake()


def listen_after_wake() -> str:
    """TTS «Слушаю» + ACTIVE VAD параллельно (не теряем начало фразы)."""
    audio_box: list[np.ndarray] = []
    err_box: list[Exception] = []

    def _record_worker() -> None:
        try:
            audio_box.append(record_continuous(lead_in_sec=config.VAD_TTS_OVERLAP_SEC))
        except Exception as exc:
            err_box.append(exc)

    rec = threading.Thread(target=_record_worker, daemon=True, name="vad-active")
    rec.start()
    time.sleep(config.VAD_MIC_WARMUP_SEC)
    speak(config.CONFIRM_WAKE)
    rec.join(timeout=config.VAD_MAX_RECORD_SEC + 15.0)

    if err_box:
        logger.error("listen_after_wake: %s", err_box[0])
        return ""
    if not audio_box:
        return ""
    return transcribe(audio_box[0])


def listen_command() -> str:
    delay = max(0.0, config.POST_TTS_DELAY_SEC)
    if delay > 0:
        time.sleep(delay)
    return transcribe(record_continuous())


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
                return True
        except Exception as exc:
            logger.warning("edge-tts attempt %d: %s", attempt + 1, exc)
            time.sleep(0.5 * (attempt + 1))
    return False


def _sapi_speak(text: str) -> bool:
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
    except Exception:
        pass
    try:
        import winsound
        winsound.PlaySound(str(path), winsound.SND_FILENAME)
        return True
    except Exception as exc:
        logger.warning("play failed: %s", exc)
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
        _sapi_speak(text)
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
            backends = get_voice_backends()
            active = [k for k, v in backends.items() if v]
            return f"{config.WHISPER_MODEL} ({config.WHISPER_DEVICE}) | {','.join(active) or 'rms'}"
        except Exception as exc:
            return f"ошибка: {exc}"

    def reload(self) -> None:
        config.reload_settings()
        config.ensure_wake_aliases()
        reset_whisper_model()
        global _idle_listener
        _idle_listener = None