"""
Windy AI Assistant — GUI на CustomTkinter.

Вкладки: Главная (логи, микрофон, Force Wake), Настройки VAD/Whisper, Telegram, История.
"""

from __future__ import annotations

import json
import threading
import tkinter as tk
from tkinter import messagebox

import bootstrap

bootstrap.ensure_project_path()

import customtkinter as ctk

import config
import history
from main import WindyAssistant, add_log_callback, remove_log_callback, setup_logging

setup_logging()

_VAD_LABELS = {
    "calibrating": "🔧 Калибровка шума...",
    "waiting": "👂 Жду речь...",
    "recording": "🔴 Запись...",
    "done": "✅ Запись завершена",
    "timeout": "⏱ Таймаут ожидания",
    "max_duration": "⏱ Макс. длина записи",
    "error": "❌ Ошибка микрофона",
}


class WindyGUI(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        ctk.set_appearance_mode(config.GUI_THEME)
        ctk.set_default_color_theme("blue")

        self.title("Windy AI Assistant")
        self.geometry("1040x760")
        self.minsize(900, 640)

        self.assistant = WindyAssistant()
        self.assistant.on_status(self._on_status)
        self._log_fn = self._append_log

        self._build()
        add_log_callback(self._log_fn)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self._refresh_history()

        # Подписка на уровень микрофона и VAD
        self.assistant.voice.set_mic_callback(self._on_mic_level)
        self.assistant.voice.set_vad_callback(self._on_vad_state)

    def _build(self) -> None:
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # ── Sidebar ──────────────────────────────────────────────────────────
        side = ctk.CTkFrame(self, width=210, corner_radius=0)
        side.grid(row=0, column=0, sticky="nsew")
        side.grid_rowconfigure(10, weight=1)

        ctk.CTkLabel(side, text="🌬 Windy", font=ctk.CTkFont(size=22, weight="bold")).grid(
            row=0, column=0, padx=16, pady=(20, 8)
        )
        self.lbl_status = ctk.CTkLabel(side, text="● остановлен", text_color="#888")
        self.lbl_status.grid(row=1, column=0, padx=16, pady=4)

        self.btn_start = ctk.CTkButton(side, text="▶ Старт", command=self._start)
        self.btn_start.grid(row=2, column=0, padx=16, pady=6, sticky="ew")
        self.btn_stop = ctk.CTkButton(
            side, text="■ Стоп", command=self._stop, state="disabled", fg_color="#8b2222"
        )
        self.btn_stop.grid(row=3, column=0, padx=16, pady=6, sticky="ew")

        self.btn_force_wake = ctk.CTkButton(
            side, text="⚡ Force Wake", command=self._force_wake, fg_color="#1a5276"
        )
        self.btn_force_wake.grid(row=4, column=0, padx=16, pady=(12, 6), sticky="ew")

        ctk.CTkLabel(side, text="Быстрые тесты", font=ctk.CTkFont(weight="bold")).grid(
            row=5, column=0, padx=16, pady=(16, 4), sticky="w"
        )
        for i, (txt, cmd) in enumerate([
            ("Тест TTS", self._test_tts),
            ("Тест Whisper", self._test_whisper),
            ("Тест Ollama", self._test_ollama),
            ("TG unread", self._test_unread),
            ("TG read last", self._test_read_last),
        ]):
            ctk.CTkButton(side, text=txt, command=cmd, height=28, fg_color="#333").grid(
                row=6 + i, column=0, padx=16, pady=3, sticky="ew"
            )

        ctk.CTkButton(side, text="💾 Сохранить", command=self._save).grid(
            row=12, column=0, padx=16, pady=8, sticky="ew"
        )
        ctk.CTkButton(side, text="🔄 Reload", command=self._reload, fg_color="#444").grid(
            row=13, column=0, padx=16, pady=(0, 16), sticky="ew"
        )

        # ── Tabs ─────────────────────────────────────────────────────────────
        tabs = ctk.CTkTabview(self)
        tabs.grid(row=0, column=1, sticky="nsew", padx=12, pady=12)
        t_home = tabs.add("Главная")
        t_set = tabs.add("Настройки")
        t_apps = tabs.add("Приложения")
        t_tg = tabs.add("Telegram")
        t_hist = tabs.add("История")

        # ── Главная ──────────────────────────────────────────────────────────
        t_home.grid_columnconfigure(0, weight=1)
        t_home.grid_rowconfigure(2, weight=1)

        wake_f = ctk.CTkFrame(t_home)
        wake_f.grid(row=0, column=0, sticky="ew", padx=8, pady=8)
        ctk.CTkLabel(
            wake_f,
            text="Wake-word: «Эй Винди» · «Hey Винди» · «Винди»",
            font=ctk.CTkFont(size=14),
        ).pack(anchor="w", padx=12, pady=(8, 4))
        self.lbl_wake = ctk.CTkLabel(wake_f, text="Ожидание...", text_color="#5dade2")
        self.lbl_wake.pack(anchor="w", padx=12, pady=(0, 4))
        self.lbl_vad = ctk.CTkLabel(wake_f, text="VAD: —", text_color="#aaa", font=ctk.CTkFont(size=12))
        self.lbl_vad.pack(anchor="w", padx=12, pady=(0, 8))

        mic_f = ctk.CTkFrame(t_home)
        mic_f.grid(row=1, column=0, sticky="ew", padx=8, pady=4)
        ctk.CTkLabel(mic_f, text="Микрофон", font=ctk.CTkFont(weight="bold")).pack(anchor="w", padx=12, pady=(8, 2))
        self.mic_bar = ctk.CTkProgressBar(mic_f, width=400)
        self.mic_bar.pack(anchor="w", padx=12, pady=(0, 8))
        self.mic_bar.set(0)

        cmd_f = ctk.CTkFrame(t_home)
        cmd_f.grid(row=1, column=0, sticky="ew", padx=8, pady=(70, 4))
        self.entry_cmd = ctk.CTkEntry(cmd_f, placeholder_text="Текстовая команда...")
        self.entry_cmd.pack(side="left", fill="x", expand=True, padx=8, pady=8)
        self.entry_cmd.bind("<Return>", lambda _e: self._send_text())
        ctk.CTkButton(cmd_f, text="Отправить", width=100, command=self._send_text).pack(
            side="left", padx=8, pady=8
        )

        self.txt_log = ctk.CTkTextbox(t_home, font=ctk.CTkFont(family="Consolas", size=12))
        self.txt_log.grid(row=2, column=0, sticky="nsew", padx=8, pady=8)
        ctk.CTkButton(t_home, text="Очистить логи", command=self._clear_log, width=120).grid(
            row=3, column=0, padx=8, pady=4, sticky="w"
        )

        # ── Настройки VAD / Whisper ──────────────────────────────────────────
        sf = ctk.CTkScrollableFrame(t_set)
        sf.pack(fill="both", expand=True, padx=8, pady=8)
        self.vars: dict = {}

        fields = [
            ("vad_sensitivity", "VAD чувствительность (0–1)", config.VAD_SENSITIVITY),
            ("vad_speech_threshold", "VAD speech threshold", config.VAD_SPEECH_THRESHOLD),
            ("vad_silence_threshold", "VAD silence threshold", config.VAD_SILENCE_THRESHOLD),
            ("vad_silence_sec", "VAD пауза для стопа (сек)", config.VAD_SILENCE_SEC),
            ("vad_hangover_sec", "VAD hangover (сек)", config.VAD_HANGOVER_SEC),
            ("vad_pre_roll_sec", "Pre-roll buffer (сек)", config.VAD_PRE_ROLL_SEC),
            ("post_tts_delay_sec", "Пауза после «Слушаю»", config.POST_TTS_DELAY_SEC),
            ("whisper_model", "Whisper model", config.WHISPER_MODEL),
            ("whisper_device", "Whisper device (auto/cpu/cuda)", config.WHISPER_DEVICE),
            ("whisper_compute_type", "Whisper compute (int8)", config.WHISPER_COMPUTE_TYPE),
            ("tts_voice", "TTS voice", config.TTS_VOICE),
            ("ollama_model", "Ollama model", config.OLLAMA_MODEL),
        ]
        for i, (key, label, val) in enumerate(fields):
            ctk.CTkLabel(sf, text=label).grid(row=i, column=0, sticky="w", pady=4, padx=4)
            entry = ctk.CTkEntry(sf, width=300)
            entry.insert(0, str(val))
            entry.grid(row=i, column=1, sticky="ew", pady=4, padx=4)
            self.vars[key] = entry
        sf.grid_columnconfigure(1, weight=1)

        # ── Приложения ───────────────────────────────────────────────────────
        self.lst_apps = ctk.CTkTextbox(t_apps, height=200)
        self.lst_apps.pack(fill="x", padx=8, pady=8)
        self._refresh_apps()
        af = ctk.CTkFrame(t_apps)
        af.pack(fill="x", padx=8, pady=4)
        self.e_app_n = ctk.CTkEntry(af, placeholder_text="имя", width=100)
        self.e_app_n.pack(side="left", padx=4)
        self.e_app_p = ctk.CTkEntry(af, placeholder_text="путь", width=400)
        self.e_app_p.pack(side="left", padx=4, fill="x", expand=True)
        ctk.CTkButton(af, text="+", width=40, command=self._app_add).pack(side="left", padx=4)

        # ── Telegram ─────────────────────────────────────────────────────────
        tg = ctk.CTkFrame(t_tg)
        tg.pack(fill="both", expand=True, padx=8, pady=8)
        self.e_api_id = ctk.CTkEntry(tg, placeholder_text="API ID")
        self.e_api_id.insert(0, str(config.TELEGRAM_API_ID or ""))
        self.e_api_id.pack(fill="x", padx=8, pady=6)
        self.e_api_hash = ctk.CTkEntry(tg, placeholder_text="API Hash")
        self.e_api_hash.insert(0, config.TELEGRAM_API_HASH)
        self.e_api_hash.pack(fill="x", padx=8, pady=6)
        self.e_tg_contact = ctk.CTkEntry(tg, placeholder_text="Контакт для теста чтения")
        self.e_tg_contact.pack(fill="x", padx=8, pady=6)
        ctk.CTkLabel(tg, text="Авторизация: python telegram_client.py", text_color="gray").pack(
            anchor="w", padx=8
        )
        ctk.CTkButton(tg, text="Сохранить Telegram", command=self._save_tg).pack(padx=8, pady=8, anchor="w")

        # ── История ──────────────────────────────────────────────────────────
        self.txt_hist = ctk.CTkTextbox(t_hist, font=ctk.CTkFont(family="Consolas", size=11))
        self.txt_hist.pack(fill="both", expand=True, padx=8, pady=8)
        ctk.CTkButton(t_hist, text="Обновить", command=self._refresh_history).pack(padx=8, pady=4, anchor="w")

    # ── Callbacks ──────────────────────────────────────────────────────────────

    def _on_status(self, status: str) -> None:
        def _do():
            self.lbl_status.configure(text=f"● {status}")
            low = status.lower()
            if "wake" in low:
                self.lbl_wake.configure(text="🎤 Wake-word!", text_color="#2ecc71")
            elif "запись" in low:
                self.lbl_wake.configure(text="🔴 Запись команды...", text_color="#e74c3c")
            elif "слушаю" in low or "жду" in low:
                self.lbl_wake.configure(text="👂 Слушаю...", text_color="#5dade2")
        try:
            self.after(0, _do)
        except Exception:
            pass

    def _on_mic_level(self, level: float) -> None:
        def _do():
            self.mic_bar.set(max(0.0, min(1.0, level)))
        try:
            self.after(0, _do)
        except Exception:
            pass

    def _on_vad_state(self, state: str) -> None:
        def _do():
            self.lbl_vad.configure(text=f"VAD: {_VAD_LABELS.get(state, state)}")
        try:
            self.after(0, _do)
        except Exception:
            pass

    def _append_log(self, msg: str) -> None:
        def _do():
            self.txt_log.insert("end", msg + "\n")
            self.txt_log.see("end")
        try:
            self.after(0, _do)
        except Exception:
            pass

    # ── Actions ────────────────────────────────────────────────────────────────

    def _clear_log(self) -> None:
        self.txt_log.delete("1.0", "end")

    def _refresh_apps(self) -> None:
        self.lst_apps.delete("1.0", "end")
        for name, path in sorted(config.APP_PATHS.items()):
            self.lst_apps.insert("end", f"{name} → {path}\n")

    def _refresh_history(self) -> None:
        self.txt_hist.delete("1.0", "end")
        for entry in history.get_history(30):
            self.txt_hist.insert(
                "end", f"[{entry['time']}] {entry['command']}\n  → {entry['response']}\n\n"
            )

    def _save(self) -> None:
        data = config.to_dict()
        float_keys = {
            "vad_sensitivity", "vad_speech_threshold", "vad_silence_threshold",
            "vad_silence_sec", "vad_hangover_sec", "vad_pre_roll_sec", "post_tts_delay_sec",
        }
        for key, entry in self.vars.items():
            val = entry.get().strip()
            try:
                data[key] = float(val) if key in float_keys else val
            except ValueError:
                data[key] = val
        config.SETTINGS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        config.reload_settings()
        messagebox.showinfo("", "Настройки сохранены")

    def _save_tg(self) -> None:
        data = config.to_dict()
        try:
            data["telegram_api_id"] = int(self.e_api_id.get() or 0)
        except ValueError:
            messagebox.showerror("", "API ID должен быть числом")
            return
        data["telegram_api_hash"] = self.e_api_hash.get().strip()
        config.SETTINGS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        config.reload_settings()
        messagebox.showinfo("", "Telegram настройки сохранены")

    def _app_add(self) -> None:
        name, path = self.e_app_n.get().strip().lower(), self.e_app_p.get().strip()
        if name and path:
            config.APP_PATHS[name] = path
            config.save_settings()
            self._refresh_apps()

    def _reload(self) -> None:
        self.assistant.reload_settings()
        messagebox.showinfo("", "Конфигурация перезагружена")

    def _start(self) -> None:
        if self.assistant.is_running:
            return
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.assistant.run_in_thread()
        self.after(600, self._poll)

    def _poll(self) -> None:
        if self.assistant.is_running:
            self.after(600, self._poll)
        else:
            self.btn_start.configure(state="normal")
            self.btn_stop.configure(state="disabled")

    def _stop(self) -> None:
        self.assistant.stop()

    def _force_wake(self) -> None:
        """Принудительный wake — сразу «Слушаю» и запись команды."""
        if not self.assistant.is_running:
            messagebox.showwarning("", "Сначала нажми Старт")
            return
        threading.Thread(target=self.assistant.force_wake_cycle, daemon=True).start()

    def _test_ollama(self) -> None:
        ok = self.assistant.brain.check_connection()
        messagebox.showinfo("Ollama", "Подключено" if ok else "Недоступно")

    def _test_tts(self) -> None:
        threading.Thread(target=lambda: self.assistant.voice.speak("Тест голоса Винди"), daemon=True).start()

    def _test_whisper(self) -> None:
        def _w():
            try:
                from voice import _get_whisper
                _get_whisper()
                self.after(0, lambda: messagebox.showinfo("Whisper", "Модель загружена"))
            except Exception as exc:
                self.after(0, lambda: messagebox.showerror("Whisper", str(exc)))
        threading.Thread(target=_w, daemon=True).start()

    def _test_unread(self) -> None:
        result = self.assistant.tools.execute("telegram_get_unread", {})
        messagebox.showinfo("Unread", result[:800])

    def _test_read_last(self) -> None:
        contact = self.e_tg_contact.get().strip() or "Saved Messages"
        result = self.assistant.tools.execute("telegram_read_last", {"contact": contact, "count": 5})
        messagebox.showinfo("Read last", result[:800])

    def _send_text(self) -> None:
        text = self.entry_cmd.get().strip()
        if text:
            threading.Thread(target=lambda: self._run_cmd(text), daemon=True).start()

    def _run_cmd(self, text: str) -> None:
        response = self.assistant.process_command(text)
        history.add_entry(text, response)
        self.after(0, self._refresh_history)

    def _close(self) -> None:
        remove_log_callback(self._log_fn)
        self.assistant.stop()
        self.destroy()


def run_gui() -> None:
    app = WindyGUI()
    app.mainloop()


if __name__ == "__main__":
    run_gui()