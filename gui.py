"""
Windy AI Assistant — GUI на CustomTkinter.
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


class WindyGUI(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        ctk.set_appearance_mode(config.GUI_THEME)
        ctk.set_default_color_theme("blue")

        self.title("Windy AI Assistant")
        self.geometry("1000x720")
        self.minsize(860, 600)

        self.assistant = WindyAssistant()
        self.assistant.on_status(self._on_status)
        self._log_fn = self._append_log

        self._build()
        add_log_callback(self._log_fn)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self._refresh_history()

    def _build(self) -> None:
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # Sidebar
        side = ctk.CTkFrame(self, width=200, corner_radius=0)
        side.grid(row=0, column=0, sticky="nsew")
        side.grid_rowconfigure(8, weight=1)

        ctk.CTkLabel(side, text="🌬 Windy", font=ctk.CTkFont(size=22, weight="bold")).grid(row=0, column=0, padx=16, pady=(20, 8))
        self.lbl_status = ctk.CTkLabel(side, text="● остановлен", text_color="#888")
        self.lbl_status.grid(row=1, column=0, padx=16, pady=4)

        self.btn_start = ctk.CTkButton(side, text="▶ Старт", command=self._start)
        self.btn_start.grid(row=2, column=0, padx=16, pady=6, sticky="ew")
        self.btn_stop = ctk.CTkButton(side, text="■ Стоп", command=self._stop, state="disabled", fg_color="#8b2222")
        self.btn_stop.grid(row=3, column=0, padx=16, pady=6, sticky="ew")

        ctk.CTkLabel(side, text="Быстрые действия", font=ctk.CTkFont(weight="bold")).grid(row=4, column=0, padx=16, pady=(16, 4), sticky="w")
        for i, (txt, cmd) in enumerate([
            ("Тест TTS", self._test_tts),
            ("Тест Whisper", self._test_whisper),
            ("Тест Ollama", self._test_ollama),
            ("TG unread", self._test_unread),
        ]):
            ctk.CTkButton(side, text=txt, command=cmd, height=28, fg_color="#333").grid(row=5 + i, column=0, padx=16, pady=3, sticky="ew")

        ctk.CTkButton(side, text="💾 Сохранить", command=self._save).grid(row=13, column=0, padx=16, pady=8, sticky="ew")
        ctk.CTkButton(side, text="🔄 Reload", command=self._reload, fg_color="#444").grid(row=14, column=0, padx=16, pady=(0, 16), sticky="ew")

        # Main tabs
        tabs = ctk.CTkTabview(self)
        tabs.grid(row=0, column=1, sticky="nsew", padx=12, pady=12)
        t_home = tabs.add("Главная")
        t_set = tabs.add("Настройки")
        t_apps = tabs.add("Приложения")
        t_tg = tabs.add("Telegram")
        t_hist = tabs.add("История")

        # --- Главная ---
        t_home.grid_columnconfigure(0, weight=1)
        t_home.grid_rowconfigure(1, weight=1)

        wake_f = ctk.CTkFrame(t_home)
        wake_f.grid(row=0, column=0, sticky="ew", padx=8, pady=8)
        ctk.CTkLabel(wake_f, text="Wake-word: «Эй Винди» · «Hey Винди» · «Винди»", font=ctk.CTkFont(size=14)).pack(anchor="w", padx=12, pady=8)
        self.lbl_wake = ctk.CTkLabel(wake_f, text="Ожидание...", text_color="#5dade2")
        self.lbl_wake.pack(anchor="w", padx=12, pady=(0, 8))

        cmd_f = ctk.CTkFrame(t_home)
        cmd_f.grid(row=0, column=0, sticky="ew", padx=8, pady=(60, 8))
        self.entry_cmd = ctk.CTkEntry(cmd_f, placeholder_text="Текстовая команда...")
        self.entry_cmd.pack(side="left", fill="x", expand=True, padx=8, pady=8)
        self.entry_cmd.bind("<Return>", lambda e: self._send_text())
        ctk.CTkButton(cmd_f, text="Отправить", width=100, command=self._send_text).pack(side="left", padx=8, pady=8)

        self.txt_log = ctk.CTkTextbox(t_home, font=ctk.CTkFont(family="Consolas", size=12))
        self.txt_log.grid(row=1, column=0, sticky="nsew", padx=8, pady=8)
        ctk.CTkButton(t_home, text="Очистить логи", command=self._clear_log, width=120).grid(row=2, column=0, padx=8, pady=4, sticky="w")

        # --- Настройки ---
        sf = ctk.CTkScrollableFrame(t_set)
        sf.pack(fill="both", expand=True, padx=8, pady=8)
        self.vars: dict = {}
        fields = [
            ("whisper_model", "Whisper model", config.WHISPER_MODEL),
            ("whisper_device", "Whisper device", config.WHISPER_DEVICE),
            ("vad_speech_threshold", "VAD speech", config.VAD_SPEECH_THRESHOLD),
            ("vad_silence_sec", "VAD silence sec", config.VAD_SILENCE_SEC),
            ("vad_hangover_sec", "VAD hangover", config.VAD_HANGOVER_SEC),
            ("vad_pre_roll_sec", "Pre-roll sec", config.VAD_PRE_ROLL_SEC),
            ("tts_voice", "TTS voice", config.TTS_VOICE),
            ("tts_volume", "TTS volume (+0%)", config.TTS_VOLUME),
            ("ollama_model", "Ollama model", config.OLLAMA_MODEL),
        ]
        for i, (k, lbl, val) in enumerate(fields):
            ctk.CTkLabel(sf, text=lbl).grid(row=i, column=0, sticky="w", pady=4, padx=4)
            e = ctk.CTkEntry(sf, width=280)
            e.insert(0, str(val))
            e.grid(row=i, column=1, sticky="ew", pady=4, padx=4)
            self.vars[k] = e
        sf.grid_columnconfigure(1, weight=1)

        # --- Приложения ---
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

        # --- Telegram ---
        tg = ctk.CTkFrame(t_tg)
        tg.pack(fill="both", expand=True, padx=8, pady=8)
        self.e_api_id = ctk.CTkEntry(tg, placeholder_text="API ID")
        self.e_api_id.insert(0, str(config.TELEGRAM_API_ID or ""))
        self.e_api_id.pack(fill="x", padx=8, pady=6)
        self.e_api_hash = ctk.CTkEntry(tg, placeholder_text="API Hash")
        self.e_api_hash.insert(0, config.TELEGRAM_API_HASH)
        self.e_api_hash.pack(fill="x", padx=8, pady=6)
        ctk.CTkLabel(tg, text="Авторизация: python telegram_client.py", text_color="gray").pack(anchor="w", padx=8)
        ctk.CTkButton(tg, text="Сохранить Telegram", command=self._save_tg).pack(padx=8, pady=8, anchor="w")

        # --- История ---
        self.txt_hist = ctk.CTkTextbox(t_hist, font=ctk.CTkFont(family="Consolas", size=11))
        self.txt_hist.pack(fill="both", expand=True, padx=8, pady=8)
        ctk.CTkButton(t_hist, text="Обновить", command=self._refresh_history).pack(padx=8, pady=4, anchor="w")

    def _on_status(self, s: str) -> None:
        def _do():
            self.lbl_status.configure(text=f"● {s}")
            if "wake" in s.lower():
                self.lbl_wake.configure(text="🎤 Wake-word!", text_color="#2ecc71")
            elif "запись" in s.lower():
                self.lbl_wake.configure(text="🔴 Запись...", text_color="#e74c3c")
            elif "слушаю" in s.lower() or "жду" in s.lower():
                self.lbl_wake.configure(text="👂 Слушаю...", text_color="#5dade2")
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

    def _clear_log(self) -> None:
        self.txt_log.delete("1.0", "end")

    def _refresh_apps(self) -> None:
        self.lst_apps.delete("1.0", "end")
        for n, p in sorted(config.APP_PATHS.items()):
            self.lst_apps.insert("end", f"{n} → {p}\n")

    def _refresh_history(self) -> None:
        self.txt_hist.delete("1.0", "end")
        for h in history.get_history(30):
            self.txt_hist.insert("end", f"[{h['time']}] {h['command']}\n  → {h['response']}\n\n")

    def _save(self) -> None:
        data = config.to_dict()
        for k, e in self.vars.items():
            v = e.get().strip()
            try:
                data[k] = float(v) if k.startswith("vad_") else v
            except ValueError:
                data[k] = v
        config.SETTINGS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        config.reload_settings()
        messagebox.showinfo("", "Сохранено")

    def _save_tg(self) -> None:
        data = config.to_dict()
        try:
            data["telegram_api_id"] = int(self.e_api_id.get() or 0)
        except ValueError:
            messagebox.showerror("", "API ID — число")
            return
        data["telegram_api_hash"] = self.e_api_hash.get().strip()
        config.SETTINGS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        config.reload_settings()
        messagebox.showinfo("", "Telegram OK")

    def _app_add(self) -> None:
        n, p = self.e_app_n.get().strip().lower(), self.e_app_p.get().strip()
        if n and p:
            config.APP_PATHS[n] = p
            config.save_settings()
            self._refresh_apps()

    def _reload(self) -> None:
        self.assistant.reload_settings()
        messagebox.showinfo("", "Reload OK")

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

    def _test_ollama(self) -> None:
        messagebox.showinfo("Ollama", "OK" if self.assistant.brain.check_connection() else "Нет")

    def _test_tts(self) -> None:
        threading.Thread(target=lambda: self.assistant.voice.speak("Тест Винди"), daemon=True).start()

    def _test_whisper(self) -> None:
        def _w():
            try:
                from voice import _get_whisper
                _get_whisper()
                self.after(0, lambda: messagebox.showinfo("Whisper", "OK"))
            except Exception as e:
                self.after(0, lambda: messagebox.showerror("Whisper", str(e)))
        threading.Thread(target=_w, daemon=True).start()

    def _test_unread(self) -> None:
        r = self.assistant.tools.execute("telegram_get_unread", {})
        messagebox.showinfo("Unread", r)

    def _send_text(self) -> None:
        t = self.entry_cmd.get().strip()
        if t:
            threading.Thread(target=lambda: self._run_cmd(t), daemon=True).start()

    def _run_cmd(self, t: str) -> None:
        r = self.assistant.process_command(t)
        history.add_entry(t, r)
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