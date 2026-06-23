"""
Windy AI Assistant — entry point.
  python main.py         CLI
  python main.py --gui   GUI
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
from brain import Brain
from plugin_manager import load_plugins
from tools import ToolExecutor
from voice import VoiceEngine, set_wake_callback

_log_cbs: list[Callable[[str], None]] = []


class GuiLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            for cb in list(_log_cbs):
                try:
                    cb(msg)
                except Exception:
                    pass
        except Exception:
            pass


def setup_logging(level: str | None = None) -> None:
    lvl = getattr(logging, (level or config.LOG_LEVEL).upper(), logging.INFO)
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(lvl)
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
    _log_cbs.append(cb)


def remove_log_callback(cb: Callable[[str], None]) -> None:
    if cb in _log_cbs:
        _log_cbs.remove(cb)


logger = logging.getLogger("windy")


class WindyAssistant:
    def __init__(self) -> None:
        self.voice = VoiceEngine()
        self.brain = Brain()
        self.tools = ToolExecutor()
        self._running = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._status_cb: Callable[[str], None] | None = None

        plugins = load_plugins()
        if plugins:
            logger.info("plugins: %s", ", ".join(plugins))

    def on_status(self, cb: Callable[[str], None] | None) -> None:
        self._status_cb = cb

    def _set_status(self, s: str) -> None:
        if self._status_cb:
            try:
                self._status_cb(s)
            except Exception:
                pass

    @property
    def is_running(self) -> bool:
        return self._running

    def stop(self) -> None:
        self._stop.set()
        self._running = False
        self._set_status("остановлен")

    def reload_settings(self) -> None:
        config.reload_settings()
        self.voice.reload()
        self.brain.reload_prompt()
        setup_logging()
        logger.info("hot-reload OK")

    def startup(self, speak: bool = True) -> None:
        logger.info(
            "Windy | wake=%s | whisper=%s/%s/%s | ollama=%s",
            config.WAKE_WORD,
            config.WHISPER_MODEL,
            config.WHISPER_DEVICE,
            config.WHISPER_COMPUTE_TYPE,
            config.OLLAMA_MODEL,
        )
        if not self.brain.check_connection():
            logger.warning("Ollama недоступна")
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

        user_text = self._strip_wake(user_text)
        if not user_text:
            if speak:
                self.voice.speak(config.CONFIRM_WAKE)
            return config.CONFIRM_WAKE

        logger.info("cmd: %s", user_text)
        self._set_status("думаю...")

        try:
            resp = self.brain.think(user_text)
        except Exception as exc:
            logger.error("brain: %s", exc)
            if speak:
                self.voice.speak(config.ERROR_GENERIC)
            self._set_status("слушаю...")
            return config.ERROR_GENERIC

        results: list[str] = []
        if resp.has_actions:
            self._set_status("выполняю...")
            try:
                results = self.tools.execute_all(resp.actions)
            except Exception as exc:
                results = [str(exc)]

        speech = resp.speech.strip()
        if not speech and results:
            speech = ". ".join(results)
        if not speech:
            speech = "Готово."

        if speak:
            self._set_status("говорю...")
            try:
                self.voice.speak(speech)
            except Exception as exc:
                logger.error("tts: %s", exc)

        self._set_status("слушаю...")
        return speech

    def run_once(self) -> None:
        if self._stop.is_set():
            return
        self._set_status("жду wake-word...")
        if self.voice.wait_for_wake_word():
            self._set_status("wake-word!")
            self.voice.speak(config.CONFIRM_WAKE)
            self._set_status("запись...")
            cmd = self.voice.listen_command()
            self.process_command(cmd)

    def run(self) -> None:
        self._running = True
        self._stop.clear()
        set_wake_callback(lambda: self._set_status("wake-word!"))
        self.startup()
        logger.info("Слушаю «Эй Винди» / «Винди»...")

        while self._running and not self._stop.is_set():
            try:
                self.run_once()
            except KeyboardInterrupt:
                break
            except Exception as exc:
                logger.exception("loop: %s", exc)
                time.sleep(1)

        self._running = False
        self._set_status("остановлен")
        logger.info("stopped")

    def run_in_thread(self) -> threading.Thread:
        if self._thread and self._thread.is_alive():
            return self._thread
        self._stop.clear()
        self._thread = threading.Thread(target=self.run, daemon=True, name="Windy")
        self._thread.start()
        return self._thread


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gui", action="store_true")
    args = parser.parse_args()
    setup_logging()
    logger.info("project: %s", PROJECT_DIR)
    if args.gui:
        from gui import run_gui
        run_gui()
    else:
        WindyAssistant().run()


if __name__ == "__main__":
    main()