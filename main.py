"""
Windy AI Assistant — entry point.

Цикл: wake-word → «Слушаю» → continuous VAD → STT → Ollama JSON → tools → TTS.
"""

from __future__ import annotations

import argparse
import logging
import threading
import time
from typing import Callable

import bootstrap
from bootstrap import PROJECT_DIR

bootstrap.ensure_project_path()

import config
import history
import learning
import reminders
from brain import Brain
from plugin_manager import load_plugins
from tools import ToolExecutor
from voice import VoiceEngine, set_wake_callback

_log_callbacks: list[Callable[[str], None]] = []
logger = logging.getLogger("windy")


class GuiLogHandler(logging.Handler):
    """Пробрасывает логи в GUI textbox."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            for cb in list(_log_callbacks):
                try:
                    cb(msg)
                except Exception:
                    pass
        except Exception:
            pass


def setup_logging(level: str | None = None) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(getattr(logging, (level or config.LOG_LEVEL).upper(), logging.INFO))
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%H:%M:%S")

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)

    try:
        fh = logging.FileHandler(config.LOG_DIR / "windy.log", encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except Exception as exc:
        logger.debug("file log unavailable: %s", exc)

    gh = GuiLogHandler()
    gh.setFormatter(fmt)
    root.addHandler(gh)


def add_log_callback(cb: Callable[[str], None]) -> None:
    _log_callbacks.append(cb)


def remove_log_callback(cb: Callable[[str], None]) -> None:
    if cb in _log_callbacks:
        _log_callbacks.remove(cb)


class WindyAssistant:
    def __init__(self) -> None:
        self.voice = VoiceEngine()
        self.brain = Brain()
        self.tools = ToolExecutor()
        self._running = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._status_cb: Callable[[str], None] | None = None
        self._force_lock = threading.Lock()
        self._last_command = ""
        self._last_entry_id = ""

        plugins = load_plugins()
        if plugins:
            logger.info("plugins loaded: %s", ", ".join(plugins))

        reminders.set_reminder_callback(lambda msg: self.voice.speak(msg))
        reminders.start_reminder_service()

        if config.LEARNING_ENABLED and config.LEARNING_AUTO_SCAN:
            learning.scan_apps_async(on_done=lambda _: learning.merge_scanned_into_config())

    def on_status(self, cb: Callable[[str], None] | None) -> None:
        self._status_cb = cb

    def _set_status(self, text: str) -> None:
        if self._status_cb:
            try:
                self._status_cb(text)
            except Exception:
                pass

    @property
    def is_running(self) -> bool:
        return self._running

    def stop(self) -> None:
        self._stop.set()
        self._running = False
        reminders.stop_reminder_service()
        self._set_status("остановлен")

    def get_service_health(self) -> dict[str, tuple[bool, str]]:
        """Статусы сервисов для GUI."""
        ollama_ok = self.brain.check_connection()
        hs = self.brain.get_hybrid_status()
        ollama_lbl = (
            f"{hs.get('fast', '?')} / {hs.get('slow', '?')}"
            if hs.get("hybrid_enabled") else config.OLLAMA_MODEL
        )
        whisper_st = self.voice.get_whisper_status()
        whisper_ok = "ошибка" not in whisper_st.lower()
        try:
            import telegram_client as tg
            tg_ok, tg_msg = tg.check_connection()
        except Exception as exc:
            tg_ok, tg_msg = False, str(exc)[:50]
        return {
            "ollama": (ollama_ok, ollama_lbl if ollama_ok else "offline"),
            "whisper": (whisper_ok, whisper_st),
            "telegram": (tg_ok, tg_msg),
        }

    def reload_settings(self) -> None:
        config.reload_settings()
        config.validate_app_paths()
        config.ensure_app_paths()
        config.invalidate_app_cache()
        self.voice.reload()
        self.brain.reload_prompt()
        try:
            import telegram_client as tg
            tg.reset_client()
        except Exception:
            pass
        setup_logging()

    def startup(self, speak: bool = True) -> None:
        from voice import get_voice_backends
        vb = get_voice_backends()
        hs = self.brain.get_hybrid_status()
        logger.info(
            "Windy v%s | hybrid=%s fast=%s slow=%s | whisper=%s/%s | vad_release=%.1fs",
            config.GUI_VERSION,
            hs.get("hybrid_enabled"),
            hs.get("fast"),
            hs.get("slow"),
            config.WHISPER_MODEL, config.WHISPER_DEVICE,
            config.vad_release_sec(),
        )
        if not self.brain.check_connection():
            logger.warning("Ollama unavailable — запусти ollama serve")
        if speak:
            try:
                self.voice.speak(config.STARTUP_GREETING)
            except Exception as exc:
                logger.error("startup TTS: %s", exc)

    def _strip_wake(self, text: str) -> str:
        t = text.lower()
        for alias in sorted(config.WAKE_WORD_ALIASES, key=len, reverse=True):
            t = t.replace(alias, "")
        return t.strip(" ,.")

    def process_command(self, user_text: str, *, speak: bool = True) -> str:
        user_text = (user_text or "").strip()
        if not user_text:
            if speak:
                self.voice.speak(config.NO_SPEECH)
            return config.NO_SPEECH

        clean = self._strip_wake(user_text)
        if not clean:
            if speak:
                self.voice.speak(config.CONFIRM_WAKE)
            return config.CONFIRM_WAKE

        logger.info("command: %s", clean)
        self._set_status("думаю...")

        try:
            response = self.brain.think(clean)
            if response.route_tier:
                logger.info(
                    "model route: tier=%s model=%s reason=%s",
                    response.route_tier, response.model_used, response.route_reason,
                )
        except Exception as exc:
            logger.error("brain error: %s", exc, exc_info=True)
            if speak:
                self.voice.speak(config.ERROR_GENERIC)
            return config.ERROR_GENERIC

        tool_results: list[str] = []
        if response.has_actions:
            self._set_status("выполняю...")
            try:
                tool_results = self.tools.execute_response(
                    macros=response.macros_as_dicts() if response.macros else None,
                    actions=response.actions if response.actions and not response.macros else None,
                    model_used=response.model_used,
                    route_tier=response.route_tier,
                )
            except Exception as exc:
                logger.error("tools.execute_response: %s", exc, exc_info=True)
                tool_results = [config.ERROR_GENERIC]
            for r in tool_results:
                logger.info("macro result: %s", r[:200])

        speech = response.speech.strip() or ". ".join(tool_results) or "Готово."
        if speak:
            self._set_status("говорю...")
            try:
                self.voice.speak(speech)
            except Exception as exc:
                logger.error("TTS error: %s", exc)

        self._last_command = clean
        macros_log = response.macros_as_dicts() if response.macros else []
        self._last_entry_id = history.add_entry(
            clean, speech, macros=macros_log, model=response.model_used,
        )
        self._set_status("слушаю...")
        return speech

    def mark_last_wrong(self, feedback: str = "") -> str:
        """Пометить последний ответ как неверный (обучение)."""
        cmd = self._last_command or (history.get_last_entry() or {}).get("command", "")
        if not cmd:
            return "Нет последней команды"
        history.mark_last_wrong(cmd, feedback)
        if config.LEARNING_ENABLED:
            return learning.mark_wrong(cmd, feedback=feedback, entry_id=self._last_entry_id)
        return "Помечено как неверное"

    def run_once(self) -> None:
        if self._stop.is_set():
            return

        self._set_status("жду wake-word...")
        if self.voice.wait_for_wake_word():
            self._set_status("запись...")
            # TTS «Слушаю» + VAD параллельно — не обрезаем начало длинной команды
            cmd = self.voice.listen_after_wake()
            if cmd:
                self.process_command(cmd)
            else:
                logger.info("empty command after VAD")
                self.voice.speak(config.NO_SPEECH)

    def force_wake_cycle(self) -> None:
        """Force Wake из GUI: пропуск wake-word, сразу запись команды."""
        if not self._force_lock.acquire(blocking=False):
            return
        try:
            if self._stop.is_set() or not self._running:
                return
            logger.info("force wake cycle")
            self._set_status("force wake!")
            self.voice.trigger_force_wake()
            self._set_status("запись...")
            cmd = self.voice.listen_after_wake()
            if cmd:
                self.process_command(cmd)
            else:
                self.voice.speak(config.NO_SPEECH)
        except Exception as exc:
            logger.error("force_wake_cycle: %s", exc, exc_info=True)
        finally:
            self._force_lock.release()

    def run(self) -> None:
        self._running = True
        self._stop.clear()
        set_wake_callback(lambda: self._set_status("wake-word!"))
        self.startup()

        while self._running and not self._stop.is_set():
            try:
                self.run_once()
            except KeyboardInterrupt:
                break
            except Exception as exc:
                logger.exception("main loop error: %s", exc)
                time.sleep(1)

        self._running = False
        self._set_status("остановлен")

    def run_in_thread(self) -> threading.Thread:
        if self._thread and self._thread.is_alive():
            return self._thread
        self._stop.clear()
        self._thread = threading.Thread(target=self.run, daemon=True, name="windy-main")
        self._thread.start()
        return self._thread


def main() -> None:
    parser = argparse.ArgumentParser(description="Windy AI Assistant")
    parser.add_argument("--gui", action="store_true", help="Запуск с GUI")
    args = parser.parse_args()
    setup_logging()

    if args.gui:
        from gui import run_gui
        run_gui()
    else:
        WindyAssistant().run()


if __name__ == "__main__":
    main()