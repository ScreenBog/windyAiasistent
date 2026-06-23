"""
Голосовой модуль: STT (faster-whisper), TTS, VAD с hangover.
Whisper: small + int8, fallback cuda→cpu (без float16 на GTX 10xx).
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
from typing import Optional

import numpy as np
import sounddevice as sd
from scipy.signal import butter, filtfilt

import bootstrap  # noqa: F401 — гарантия sys.path
import config

logger = logging.getLogger(__name__)

_whisper_model = None
_whisper_config_key: tuple[str, str, str] | None = None

_FILLER_PATTERN = re.compile(
    r"\b(э+|мм+|ну+|ага|угу|типа|как бы|в общем)\b",
    re.IGNORECASE,
)


def reset_whisper_model() -> None:
    global _whisper_model, _whisper_config_key
    _whisper_model = None
    _whisper_config_key = None


def _load_whisper_with_fallback() -> tuple[object, str, str]:
    """
    Загружает WhisperModel с цепочкой fallback.
    Возвращает (model, device, compute_type).
    """
    from faster_whisper import WhisperModel

    chain = config.resolve_whisper_backend()
    last_error: Exception | None = None

    for device, compute in chain:
        try:
            logger.info(
                "Пробую Whisper: model=%s device=%s compute=%s",
                config.WHISPER_MODEL, device, compute,
            )
            model = WhisperModel(
                config.WHISPER_MODEL,
                device=device,
                compute_type=compute,
            )
            logger.info("Whisper загружен: %s / %s", device, compute)
            return model, device, compute
        except Exception as exc:
            last_error = exc
            logger.warning("Whisper %s/%s не запустился: %s", device, compute, exc)

    raise RuntimeError(f"Не удалось загрузить Whisper: {last_error}")


def _get_whisper():
    global _whisper_model, _whisper_config_key
    key = (config.WHISPER_MODEL, config.WHISPER_DEVICE, config.WHISPER_COMPUTE_TYPE)
    if _whisper_model is not None and _whisper_config_key == key:
        return _whisper_model

    model, device, compute = _load_whisper_with_fallback()
    _whisper_model = model
    _whisper_config_key = (config.WHISPER_MODEL, device, compute)
    return _whisper_model


def _rms(audio: np.ndarray) -> float:
    if audio.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(audio, dtype=np.float64))))


def _smooth_level(level: float, history: deque[float]) -> float:
    history.append(level)
    return float(np.mean(history))


def _highpass_filter(audio: np.ndarray, sample_rate: int = config.SAMPLE_RATE) -> np.ndarray:
    try:
        if audio.size < 64:
            return audio
        nyq = sample_rate / 2
        b, a = butter(2, max(80.0 / nyq, 0.001), btype="high")
        return filtfilt(b, a, audio).astype(np.float32)
    except Exception as exc:
        logger.debug("highpass: %s", exc)
        return audio


def _normalize_audio(audio: np.ndarray, target_rms: float = 0.05) -> np.ndarray:
    rms = _rms(audio)
    if rms < 1e-6:
        return audio
    gain = min(target_rms / rms, 10.0)
    return np.clip(audio * gain, -1.0, 1.0).astype(np.float32)


def _clean_transcript(text: str) -> str:
    if not text:
        return ""
    text = _FILLER_PATTERN.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    words = text.split()
    deduped: list[str] = []
    for w in words:
        if not deduped or w.lower() != deduped[-1].lower():
            deduped.append(w)
    return " ".join(deduped)


def _record_seconds(duration: float) -> np.ndarray:
    frames = int(duration * config.SAMPLE_RATE)
    if frames <= 0:
        return np.array([], dtype=np.float32)
    try:
        recording = sd.rec(
            frames,
            samplerate=config.SAMPLE_RATE,
            channels=config.CHANNELS,
            dtype=config.DTYPE,
        )
        sd.wait()
        return recording.flatten().astype(np.float32)
    except Exception as exc:
        logger.error("Ошибка записи: %s", exc)
        raise


def _measure_ambient_noise(seconds: float = 0.6) -> float:
    """Фоновый шум ДО основной записи (не во время TTS-эха)."""
    try:
        audio = _record_seconds(seconds)
        return _rms(audio)
    except Exception:
        return 0.0


def record_continuous(
    max_duration: float | None = None,
    silence_sec: float | None = None,
    speech_threshold: float | None = None,
    silence_threshold: float | None = None,
) -> np.ndarray:
    """
    Непрерывная запись с VAD:
    - сглаженный RMS (меньше ложных срабатываний);
    - hangover — паузы внутри фразы не обрезают;
    - адаптивные пороги от фонового шума;
    - N подряд тихих чанков для завершения.
    """
    max_duration = max_duration or config.VAD_MAX_RECORD_SEC
    silence_sec = silence_sec or config.VAD_SILENCE_SEC
    speech_threshold = speech_threshold or config.VAD_SPEECH_THRESHOLD
    silence_threshold = silence_threshold or config.VAD_SILENCE_THRESHOLD
    hangover = config.VAD_HANGOVER_SEC
    min_speech = config.VAD_MIN_SPEECH_SEC
    wait_speech = config.VAD_WAIT_SPEECH_SEC

    chunk_samples = int(config.SAMPLE_RATE * config.VAD_CHUNK_MS / 1000)
    pre_roll_chunks = max(1, int(config.VAD_PRE_ROLL_SEC / (config.VAD_CHUNK_MS / 1000)))
    chunk_duration = config.VAD_CHUNK_MS / 1000.0
    silent_chunks_needed = max(3, int(silence_sec / chunk_duration))
    hangover_chunks = max(2, int(hangover / chunk_duration))

    # Калибровка шума отдельно (после post_tts_delay)
    noise_floor = _measure_ambient_noise(0.5)
    adaptive_speech = max(speech_threshold, noise_floor * 3.0)
    adaptive_silence = max(silence_threshold, noise_floor * 1.8)
    logger.debug(
        "VAD: noise=%.4f speech_thr=%.4f silence_thr=%.4f",
        noise_floor, adaptive_speech, adaptive_silence,
    )

    recorded: list[np.ndarray] = []
    pre_buffer: deque[np.ndarray] = deque(maxlen=pre_roll_chunks)
    rms_history: deque[float] = deque(maxlen=config.VAD_RMS_SMOOTH_WINDOW)

    speech_started = False
    speech_time = 0.0
    total_time = 0.0
    wait_time = 0.0
    consecutive_silent = 0
    last_speech_time = 0.0

    try:
        with sd.InputStream(
            samplerate=config.SAMPLE_RATE,
            channels=config.CHANNELS,
            dtype=config.DTYPE,
            blocksize=chunk_samples,
        ) as stream:
            while total_time < max_duration:
                chunk, overflowed = stream.read(chunk_samples)
                if overflowed:
                    logger.warning("Переполнение буфера")

                chunk = np.asarray(chunk, dtype=np.float32).flatten()
                level = _smooth_level(_rms(chunk), rms_history)
                total_time += chunk_duration

                if not speech_started:
                    wait_time += chunk_duration
                    pre_buffer.append(chunk)
                    if level >= adaptive_speech:
                        speech_started = True
                        recorded.extend(pre_buffer)
                        pre_buffer.clear()
                        speech_time = 0.0
                        consecutive_silent = 0
                        last_speech_time = total_time
                        logger.debug("VAD: речь началась (%.4f)", level)
                    elif wait_time >= wait_speech:
                        logger.debug("VAD: таймаут ожидания речи")
                        break
                    continue

                recorded.append(chunk)
                speech_time += chunk_duration

                if level < adaptive_silence:
                    consecutive_silent += 1
                else:
                    consecutive_silent = 0
                    last_speech_time = total_time

                # Hangover: недавняя речь → нужно больше тишины
                required_silent = silent_chunks_needed
                if (total_time - last_speech_time) < hangover:
                    required_silent += hangover_chunks

                if (
                    consecutive_silent >= required_silent
                    and speech_time >= min_speech
                ):
                    logger.debug(
                        "VAD: конец (речь %.1fс, тихих чанков %d)",
                        speech_time, consecutive_silent,
                    )
                    break

    except Exception as exc:
        logger.error("continuous recording: %s", exc)
        raise

    if not recorded:
        return np.array([], dtype=np.float32)

    audio = np.concatenate(recorded)
    return _normalize_audio(_highpass_filter(audio))


def transcribe(audio: np.ndarray, language: str | None = None) -> str:
    if audio is None or audio.size < config.SAMPLE_RATE * 0.25:
        return ""

    audio = _normalize_audio(_highpass_filter(audio))
    if _rms(audio) < config.VAD_SILENCE_THRESHOLD * 0.3:
        return ""

    lang = language or config.WHISPER_LANGUAGE

    try:
        model = _get_whisper()
        segments, info = model.transcribe(
            audio,
            language=lang,
            beam_size=config.WHISPER_BEAM_SIZE,
            best_of=config.WHISPER_BEST_OF,
            vad_filter=True,
            vad_parameters={
                "min_silence_duration_ms": 400,
                "speech_pad_ms": 300,
                "threshold": 0.45,
            },
            no_speech_threshold=config.WHISPER_NO_SPEECH_THRESHOLD,
            compression_ratio_threshold=config.WHISPER_COMPRESSION_RATIO_THRESHOLD,
            log_prob_threshold=config.WHISPER_LOG_PROB_THRESHOLD,
            condition_on_previous_text=False,
            temperature=0.0,
        )
        text = " ".join(seg.text.strip() for seg in segments).strip()
        text = _clean_transcript(text)
        logger.info("STT [%.1fс]: %r", getattr(info, "duration", 0) or 0, text)
        return text
    except Exception as exc:
        logger.error("STT: %s", exc)
        # Попытка перезагрузки модели при сбое CUDA
        reset_whisper_model()
        return ""


def listen_chunk(duration: float | None = None) -> str:
    duration = duration or config.WAKE_CHUNK_SEC
    return transcribe(_record_seconds(duration))


def listen_command() -> str:
    """Пауза после TTS → калибровка шума → VAD-запись → STT."""
    time.sleep(config.POST_TTS_DELAY_SEC)
    audio = record_continuous()
    return transcribe(audio)


def _contains_wake_word(text: str) -> bool:
    normalized = text.lower().strip()
    return bool(normalized) and any(a in normalized for a in config.WAKE_WORD_ALIASES)


def wait_for_wake_word() -> bool:
    try:
        return _contains_wake_word(listen_chunk(config.WAKE_CHUNK_SEC))
    except Exception as exc:
        logger.error("wake-word: %s", exc)
        time.sleep(config.WAKE_POLL_INTERVAL)
        return False


# --- TTS ---

async def _speak_edge_tts(text: str, output_path: Path) -> bool:
    try:
        import edge_tts

        comm = edge_tts.Communicate(text, voice=config.TTS_VOICE, rate=config.TTS_RATE, volume=config.TTS_VOLUME)
        await comm.save(str(output_path))
        return output_path.exists() and output_path.stat().st_size > 0
    except Exception as exc:
        logger.warning("edge-tts: %s", exc)
        return False


def _speak_sapi(text: str) -> bool:
    if not config.TTS_USE_SAPI_FALLBACK:
        return False
    safe = text.replace("'", "''")
    script = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        f"$s.Speak('{safe}')"
    )
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", script], check=True, capture_output=True, timeout=60)
        return True
    except Exception as exc:
        logger.warning("SAPI: %s", exc)
        return False


def speak(text: str) -> None:
    if not text or not text.strip():
        return
    text = text.strip()
    logger.info("TTS: %r", text)
    tmp_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False, dir=config.TEMP_DIR) as tmp:
            tmp_path = Path(tmp.name)
        if asyncio.run(_speak_edge_tts(text, tmp_path)):
            from playsound3 import playsound
            playsound(str(tmp_path), block=True)
            return
        _speak_sapi(text)
    except Exception as exc:
        logger.error("TTS: %s", exc)
        _speak_sapi(text)
    finally:
        if tmp_path and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


class VoiceEngine:
    def wait_for_wake_word(self) -> bool:
        return wait_for_wake_word()

    def listen_command(self) -> str:
        return listen_command()

    def speak(self, text: str) -> None:
        speak(text)

    def transcribe(self, audio: np.ndarray) -> str:
        return transcribe(audio)

    def record_continuous(self, **kwargs) -> np.ndarray:
        return record_continuous(**kwargs)

    def reload(self) -> None:
        config.reload_settings()
        reset_whisper_model()