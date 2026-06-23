"""
Windy AI Assistant — точка входа.
  python main.py        → CLI
  python main.py --gui  → GUI
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
from voice import VoiceEngine

_log_callbacks: list[Callable[[str], None]] = []


class GuiLogHandler(logging.Handler):
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
    level = level or config.LOG_LEVEL
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%H:%M:%S")

    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    root.addHandler(ch)

    try:
        fh = logging.FileHandler(config.LOG_DIR / "windy.log", encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except Exception:
        pass

    gh = GuiLogHandler()
    gh.setFormatter(fmt)
    root.addHandler(gh)


def add_log_callback(callback: Callable[[str], None]) -> None:
    _log_callbacks.append(callback)


def remove_log_callback(callback: Callable[[str], None]) -> None:
    if callback in _log_callbacks:
        _log_callbacks.remove(callback)


logger = logging.getLogger("windy")


class WindyAssistant:
    """Оркестратор: голос + мозг + инструменты + плагины."""

    def __init__(self) -> None:
        self.voice = VoiceEngine()
        self.brain = Brain()
        self.tools = ToolExecutor()
        self._running = False
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

        loaded = load_plugins()
        if loaded:
            logger.info("Плагины: %s", ", ".join(loaded))

    @property
    def is_running(self) -> bool:
        return self._running

    def stop(self) -> None:
        self._stop_event.set()
        self._running = False

    def reload_settings(self) -> None:
        config.reload_settings()
        self.voice.reload()
        self.brain.reload_prompt()
        setup_logging()
        logger.info("Hot-reload выполнен")

    def startup(self, speak: bool = True) -> bool:
        logger.info(
            "Windy | wake=%s | ollama=%s | whisper=%s/%s/%s",
            config.WAKE_WORD,
            config.OLLAMA_MODEL,
            config.WHISPER_MODEL,
            config.WHISPER_DEVICE,
            config.WHISPER_COMPUTE_TYPE,
        )
        if not self.brain.check_connection():
            logger.warning(
                "Ollama недоступна. Запусти: ollama serve && "
                "ollama create qwen2.5:3b-windy -f Modelfile"
            )
        if speak:
            try:
                self.voice.speak(config.STARTUP_GREETING)
            except Exception as exc:
                logger.error("TTS старт: %s", exc)
        return True

    def _strip_wake_word(self, text: str) -> str:
        t = text.lower()
        for alias in config.WAKE_WORD_ALIASES:
            t = t.replace(alias, "")
        return t.strip(" ,.")

    def process_command(self, user_text: str, *, speak_response: bool = True) -> str:
        user_text = (user_text or "").strip()
        if not user_text:
            if speak_response:
                self.voice.speak(config.NO_SPEECH)
            return config.NO_SPEECH

        user_text = self._strip_wake_word(user_text)
        if not user_text:
            if speak_response:
                self.voice.speak(config.CONFIRM_WAKE)
            return config.CONFIRM_WAKE

        logger.info("Команда: %s", user_text)

        try:
            response = self.brain.think(user_text)
        except Exception as exc:
            logger.error("Brain: %s", exc)
            if speak_response:
                self.voice.speak(config.ERROR_GENERIC)
            return config.ERROR_GENERIC

        tool_results: list[str] = []
        if response.has_actions:
            try:
                tool_results = self.tools.execute_all(response.actions)
            except Exception as exc:
                logger.error("Tools: %s", exc)
                tool_results = [str(exc)]

        speech = response.speech.strip()
        if not speech and tool_results:
            speech = ". ".join(r for r in tool_results if r)
        if not speech:
            speech = "Готово."

        if speak_response:
            try:
                self.voice.speak(speech)
            except Exception as exc:
                logger.error("TTS: %s", exc)

        return speech

    def run_once(self) -> None:
        if self._stop_event.is_set():
            return
        if self.voice.wait_for_wake_word():
            logger.info("Wake-word!")
            self.voice.speak(config.CONFIRM_WAKE)
            command = self.voice.listen_command()
            self.process_command(command)

    def run(self) -> None:
        self._running = True
        self._stop_event.clear()
        self.startup()
        logger.info("Слушаю «%s»... (Ctrl+C / Stop)", config.WAKE_WORD)

        while self._running and not self._stop_event.is_set():
            try:
                self.run_once()
            except KeyboardInterrupt:
                break
            except Exception as exc:
                logger.exception("Цикл: %s", exc)
                try:
                    self.voice.speak(config.ERROR_GENERIC)
                except Exception:
                    pass
                time.sleep(1.0)

        self._running = False
        logger.info("Windy остановлен")

    def run_in_thread(self) -> threading.Thread:
        if self._thread and self._thread.is_alive():
            return self._thread
        self._stop_event.clear()
        self._thread = threading.Thread(target=self.run, daemon=True, name="WindyLoop")
        self._thread.start()
        return self._thread


def main() -> None:
    parser = argparse.ArgumentParser(description="Windy AI Assistant")
    parser.add_argument("--gui", action="store_true", help="Графический интерфейс")
    args = parser.parse_args()

    setup_logging()
    logger.info("Проект: %s", PROJECT_DIR)

    if args.gui:
        from gui import run_gui
        run_gui()
    else:
        WindyAssistant().run()


if __name__ == "__main__":
    main()