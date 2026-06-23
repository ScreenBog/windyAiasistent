"""
Windy AI Assistant — entry point.
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
import reminders
from brain import Brain
from plugin_manager import load_plugins
from tools import ToolExecutor
from voice import VoiceEngine, set_wake_callback

_log: list[Callable[[str], None]] = []


class GuiLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            for cb in list(_log):
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
    except Exception:
        pass
    gh = GuiLogHandler()
    gh.setFormatter(fmt)
    root.addHandler(gh)


def add_log_callback(cb: Callable[[str], None]) -> None:
    _log.append(cb)


def remove_log_callback(cb: Callable[[str], None]) -> None:
    if cb in _log:
        _log.remove(cb)


logger = logging.getLogger("windy")


class WindyAssistant:
    def __init__(self) -> None:
        self.voice = VoiceEngine()
        self.brain = Brain()
        self.tools = ToolExecutor()
        self._running = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._status: Callable[[str], None] | None = None

        plugins = load_plugins()
        if plugins:
            logger.info("plugins: %s", ", ".join(plugins))

        reminders.set_reminder_callback(lambda msg: self.voice.speak(msg))
        reminders.start_reminder_service()

    def on_status(self, cb: Callable[[str], None] | None) -> None:
        self._status = cb

    def _status_set(self, s: str) -> None:
        if self._status:
            try:
                self._status(s)
            except Exception:
                pass

    @property
    def is_running(self) -> bool:
        return self._running

    def stop(self) -> None:
        self._stop.set()
        self._running = False
        reminders.stop_reminder_service()
        self._status_set("остановлен")

    def reload_settings(self) -> None:
        config.reload_settings()
        self.voice.reload()
        self.brain.reload_prompt()
        setup_logging()

    def startup(self, speak: bool = True) -> None:
        logger.info("Windy start | whisper=%s/%s | ollama=%s", config.WHISPER_MODEL, config.WHISPER_DEVICE, config.OLLAMA_MODEL)
        if not self.brain.check_connection():
            logger.warning("Ollama unavailable")
        if speak:
            try:
                self.voice.speak(config.STARTUP_GREETING)
            except Exception as exc:
                logger.error("TTS: %s", exc)

    def _strip_wake(self, text: str) -> str:
        t = text.lower()
        for a in sorted(config.WAKE_WORD_ALIASES, key=len, reverse=True):
            t = t.replace(a, "")
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

        logger.info("cmd: %s", clean)
        self._status_set("думаю...")

        try:
            resp = self.brain.think(clean)
        except Exception as exc:
            logger.error("brain: %s", exc)
            if speak:
                self.voice.speak(config.ERROR_GENERIC)
            return config.ERROR_GENERIC

        results: list[str] = []
        if resp.has_actions:
            self._status_set("выполняю...")
            results = self.tools.execute_all(resp.actions)

        speech = resp.speech.strip() or ". ".join(results) or "Готово."
        if speak:
            self._status_set("говорю...")
            try:
                self.voice.speak(speech)
            except Exception as exc:
                logger.error("tts: %s", exc)

        history.add_entry(clean, speech)
        self._status_set("слушаю...")
        return speech

    def run_once(self) -> None:
        if self._stop.is_set():
            return
        self._status_set("жду wake-word...")
        if self.voice.wait_for_wake_word():
            self.voice.speak(config.CONFIRM_WAKE)
            self._status_set("запись...")
            cmd = self.voice.listen_command()
            self.process_command(cmd)

    def run(self) -> None:
        self._running = True
        self._stop.clear()
        set_wake_callback(lambda: self._status_set("wake-word!"))
        self.startup()
        while self._running and not self._stop.is_set():
            try:
                self.run_once()
            except KeyboardInterrupt:
                break
            except Exception as exc:
                logger.exception("loop: %s", exc)
                time.sleep(1)
        self._running = False

    def run_in_thread(self) -> threading.Thread:
        if self._thread and self._thread.is_alive():
            return self._thread
        self._stop.clear()
        self._thread = threading.Thread(target=self.run, daemon=True)
        self._thread.start()
        return self._thread


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gui", action="store_true")
    args = parser.parse_args()
    setup_logging()
    if args.gui:
        from gui import run_gui
        run_gui()
    else:
        WindyAssistant().run()


if __name__ == "__main__":
    main()