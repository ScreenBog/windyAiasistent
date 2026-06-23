"""
Windy AI Assistant — современный GUI (CustomTkinter).

Дизайн: тёмная тема, карточки, анимация wake-word, live-логи,
панель настроек со слайдерами, статусы сервисов, история команд.
"""

from __future__ import annotations

import json
import math
import threading
import time
from tkinter import messagebox

import bootstrap

bootstrap.ensure_project_path()

import customtkinter as ctk

import config
import history
from main import WindyAssistant, add_log_callback, remove_log_callback, setup_logging

setup_logging()


# ── Палитра (GitHub-dark inspired) ────────────────────────────────────────────
class Theme:
    BG = "#0d1117"
    SURFACE = "#161b22"
    SURFACE2 = "#1c2128"
    BORDER = "#30363d"
    TEXT = "#e6edf3"
    MUTED = "#8b949e"
    ACCENT = config.GUI_ACCENT
    SUCCESS = "#3fb950"
    WARNING = "#d29922"
    DANGER = "#f85149"
    WAKE_IDLE = "#58a6ff"
    WAKE_ACTIVE = "#3fb950"
    WAKE_RECORD = "#ff7b72"


_VAD_LABELS = {
    "calibrating": "Калибровка шума",
    "waiting": "Ожидание речи",
    "recording": "Запись команды",
    "done": "Готово",
    "timeout": "Таймаут",
    "max_duration": "Лимит записи",
    "error": "Ошибка микрофона",
}

_WAKE_STATES = {
    "idle": ("Ожидание wake-word", Theme.WAKE_IDLE),
    "listening": ("Слушаю...", Theme.WAKE_IDLE),
    "wake": ("Wake-word!", Theme.WAKE_ACTIVE),
    "recording": ("Запись...", Theme.WAKE_RECORD),
    "thinking": ("Думаю...", Theme.WARNING),
    "speaking": ("Говорю...", Theme.ACCENT),
    "stopped": ("Остановлен", Theme.MUTED),
}


class StatusPill(ctk.CTkFrame):
    """Компактный индикатор статуса сервиса."""

    def __init__(self, master, label: str, **kwargs) -> None:
        super().__init__(master, fg_color=Theme.SURFACE2, corner_radius=20, **kwargs)
        self._dot = ctk.CTkLabel(self, text="●", font=ctk.CTkFont(size=14), text_color=Theme.MUTED)
        self._dot.pack(side="left", padx=(10, 4), pady=6)
        ctk.CTkLabel(self, text=label, font=ctk.CTkFont(size=12), text_color=Theme.MUTED).pack(
            side="left", padx=(0, 4)
        )
        self._val = ctk.CTkLabel(self, text="—", font=ctk.CTkFont(size=12, weight="bold"), text_color=Theme.TEXT)
        self._val.pack(side="left", padx=(0, 12), pady=6)

    def set(self, ok: bool | None, text: str) -> None:
        color = Theme.SUCCESS if ok else (Theme.DANGER if ok is False else Theme.MUTED)
        self._dot.configure(text_color=color)
        self._val.configure(text=text[:28])


class WindyGUI(ctk.CTk):
    def __init__(self) -> None:
        super().__init__(fg_color=Theme.BG)
        ctk.set_appearance_mode(config.GUI_THEME)
        ctk.set_default_color_theme("blue")

        self.title("Windy AI Assistant")
        self.geometry("1180x780")
        self.minsize(1000, 680)

        self.assistant = WindyAssistant()
        self.assistant.on_status(self._on_assistant_status)
        self._log_fn = self._append_log

        self._wake_state = "idle"
        self._pulse_phase = 0.0
        self._pulse_job: str | None = None
        self._health_job: str | None = None

        self._build()
        add_log_callback(self._log_fn)
        self.protocol("WM_DELETE_WINDOW", self._close)

        self.assistant.voice.set_mic_callback(self._on_mic_level)
        self.assistant.voice.set_vad_callback(self._on_vad_state)

        self._refresh_history()
        self._start_pulse()
        self._start_health_poll()

    # ── Layout ────────────────────────────────────────────────────────────────

    def _build(self) -> None:
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self._build_sidebar()
        self._build_main()

    def _build_sidebar(self) -> None:
        side = ctk.CTkFrame(self, width=240, corner_radius=0, fg_color="#010409", border_width=0)
        side.grid(row=0, column=0, sticky="nsew")
        side.grid_propagate(False)

        # Logo
        logo = ctk.CTkFrame(side, fg_color="transparent")
        logo.pack(fill="x", padx=20, pady=(24, 8))
        ctk.CTkLabel(logo, text="🌬", font=ctk.CTkFont(size=32)).pack(side="left")
        ctk.CTkLabel(
            logo, text="Windy", font=ctk.CTkFont(size=26, weight="bold"), text_color=Theme.TEXT
        ).pack(side="left", padx=(8, 0))
        ctk.CTkLabel(
            side, text="AI Voice Assistant", font=ctk.CTkFont(size=11), text_color=Theme.MUTED
        ).pack(anchor="w", padx=24, pady=(0, 16))

        # Control buttons
        self.btn_start = ctk.CTkButton(
            side, text="▶  Запустить", height=42, corner_radius=10,
            fg_color=Theme.ACCENT, hover_color=config.GUI_ACCENT_HOVER,
            font=ctk.CTkFont(size=14, weight="bold"), command=self._start,
        )
        self.btn_start.pack(fill="x", padx=16, pady=4)

        self.btn_stop = ctk.CTkButton(
            side, text="■  Остановить", height=42, corner_radius=10,
            fg_color=Theme.DANGER, hover_color="#da3633", state="disabled",
            font=ctk.CTkFont(size=14), command=self._stop,
        )
        self.btn_stop.pack(fill="x", padx=16, pady=4)

        self.btn_force = ctk.CTkButton(
            side, text="⚡  Force Wake", height=36, corner_radius=10,
            fg_color=Theme.SURFACE2, hover_color=Theme.BORDER, border_width=1, border_color=Theme.BORDER,
            command=self._force_wake,
        )
        self.btn_force.pack(fill="x", padx=16, pady=(12, 4))

        ctk.CTkLabel(side, text="БЫСТРЫЕ ДЕЙСТВИЯ", font=ctk.CTkFont(size=10, weight="bold"), text_color=Theme.MUTED).pack(
            anchor="w", padx=20, pady=(20, 6)
        )
        for txt, cmd in [
            ("🔊 Тест TTS", self._test_tts),
            ("🎙 Тест Whisper", self._test_whisper),
            ("🧠 Тест Ollama", self._test_ollama),
            ("✉️ TG непрочитанные", self._test_unread),
        ]:
            ctk.CTkButton(
                side, text=txt, height=32, corner_radius=8,
                fg_color=Theme.SURFACE2, hover_color=Theme.BORDER, anchor="w",
                font=ctk.CTkFont(size=12), command=cmd,
            ).pack(fill="x", padx=16, pady=2)

        ctk.CTkFrame(side, fg_color=Theme.BORDER, height=1).pack(fill="x", padx=16, pady=16)

        ctk.CTkButton(
            side, text="💾 Сохранить настройки", height=34, corner_radius=8,
            fg_color=Theme.SUCCESS, hover_color="#2ea043", command=self._save,
        ).pack(fill="x", padx=16, pady=4)
        ctk.CTkButton(
            side, text="🔄 Перезагрузить", height=34, corner_radius=8,
            fg_color=Theme.SURFACE2, hover_color=Theme.BORDER, command=self._reload,
        ).pack(fill="x", padx=16, pady=4)

        # Version
        ctk.CTkLabel(side, text="v5.0", font=ctk.CTkFont(size=10), text_color=Theme.MUTED).pack(
            side="bottom", pady=16
        )

    def _build_main(self) -> None:
        main = ctk.CTkFrame(self, fg_color=Theme.BG, corner_radius=0)
        main.grid(row=0, column=1, sticky="nsew", padx=(0, 12), pady=12)
        main.grid_columnconfigure(0, weight=1)
        main.grid_rowconfigure(2, weight=1)

        # Status bar
        bar = ctk.CTkFrame(main, fg_color=Theme.SURFACE, corner_radius=12, border_width=1, border_color=Theme.BORDER)
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        bar.grid_columnconfigure(3, weight=1)

        pills = ctk.CTkFrame(bar, fg_color="transparent")
        pills.grid(row=0, column=0, columnspan=4, sticky="ew", padx=12, pady=10)
        self.pill_ollama = StatusPill(pills, "Ollama")
        self.pill_ollama.pack(side="left", padx=(0, 8))
        self.pill_whisper = StatusPill(pills, "Whisper")
        self.pill_whisper.pack(side="left", padx=(0, 8))
        self.pill_telegram = StatusPill(pills, "Telegram")
        self.pill_telegram.pack(side="left")

        # Wake orb + mic
        wake_card = ctk.CTkFrame(main, fg_color=Theme.SURFACE, corner_radius=16, border_width=1, border_color=Theme.BORDER)
        wake_card.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        wake_card.grid_columnconfigure(1, weight=1)

        orb_frame = ctk.CTkFrame(wake_card, fg_color="transparent", width=120, height=120)
        orb_frame.grid(row=0, column=0, rowspan=2, padx=20, pady=16)
        orb_frame.grid_propagate(False)
        self.wake_orb = ctk.CTkFrame(orb_frame, width=88, height=88, corner_radius=44, fg_color=Theme.WAKE_IDLE)
        self.wake_orb.place(relx=0.5, rely=0.5, anchor="center")
        self.wake_ring = ctk.CTkFrame(orb_frame, width=100, height=100, corner_radius=50, fg_color="transparent", border_width=2, border_color=Theme.WAKE_IDLE)
        self.wake_ring.place(relx=0.5, rely=0.5, anchor="center")

        self.lbl_wake_title = ctk.CTkLabel(
            wake_card, text="Ожидание wake-word",
            font=ctk.CTkFont(size=20, weight="bold"), text_color=Theme.TEXT, anchor="w",
        )
        self.lbl_wake_title.grid(row=0, column=1, sticky="w", padx=(0, 16), pady=(20, 0))

        self.lbl_wake_hint = ctk.CTkLabel(
            wake_card, text='Скажи «Эй Винди» или нажми Force Wake',
            font=ctk.CTkFont(size=13), text_color=Theme.MUTED, anchor="w",
        )
        self.lbl_wake_hint.grid(row=1, column=1, sticky="w", padx=(0, 16), pady=(4, 0))

        mic_box = ctk.CTkFrame(wake_card, fg_color=Theme.SURFACE2, corner_radius=10)
        mic_box.grid(row=0, column=2, rowspan=2, sticky="e", padx=20, pady=16)
        ctk.CTkLabel(mic_box, text="Микрофон", font=ctk.CTkFont(size=11), text_color=Theme.MUTED).pack(padx=16, pady=(10, 2))
        self.mic_bar = ctk.CTkProgressBar(mic_box, width=160, height=10, corner_radius=5, progress_color=Theme.ACCENT)
        self.mic_bar.pack(padx=16, pady=4)
        self.mic_bar.set(0)
        self.lbl_vad = ctk.CTkLabel(mic_box, text="VAD: —", font=ctk.CTkFont(size=11), text_color=Theme.MUTED)
        self.lbl_vad.pack(padx=16, pady=(4, 10))

        # Content: logs + right panel
        content = ctk.CTkFrame(main, fg_color="transparent")
        content.grid(row=2, column=0, sticky="nsew")
        content.grid_columnconfigure(0, weight=3)
        content.grid_columnconfigure(1, weight=2)
        content.grid_rowconfigure(0, weight=1)

        # Logs
        log_card = ctk.CTkFrame(content, fg_color=Theme.SURFACE, corner_radius=12, border_width=1, border_color=Theme.BORDER)
        log_card.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        log_card.grid_rowconfigure(1, weight=1)
        log_card.grid_columnconfigure(0, weight=1)

        log_hdr = ctk.CTkFrame(log_card, fg_color="transparent")
        log_hdr.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 4))
        ctk.CTkLabel(log_hdr, text="Живые логи", font=ctk.CTkFont(size=14, weight="bold")).pack(side="left")
        ctk.CTkButton(log_hdr, text="Очистить", width=80, height=26, fg_color=Theme.SURFACE2, command=self._clear_log).pack(side="right")

        self.txt_log = ctk.CTkTextbox(
            log_card, font=ctk.CTkFont(family="Cascadia Mono", size=11),
            fg_color=Theme.SURFACE2, text_color=Theme.TEXT, corner_radius=8,
        )
        self.txt_log.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 12))

        # Right: command + history + settings
        right = ctk.CTkTabview(content, fg_color=Theme.SURFACE, segmented_button_fg_color=Theme.SURFACE2)
        right.grid(row=0, column=1, sticky="nsew")
        t_cmd = right.add("Команда")
        t_hist = right.add("История")
        t_set = right.add("Настройки")
        t_tg = right.add("Telegram")

        # Command tab
        t_cmd.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(t_cmd, text="Текстовая команда", font=ctk.CTkFont(weight="bold")).pack(anchor="w", padx=12, pady=(12, 4))
        self.entry_cmd = ctk.CTkEntry(t_cmd, placeholder_text="Например: открой хром", height=38, corner_radius=8)
        self.entry_cmd.pack(fill="x", padx=12, pady=4)
        self.entry_cmd.bind("<Return>", lambda _e: self._send_text())
        ctk.CTkButton(t_cmd, text="Отправить", height=36, fg_color=Theme.ACCENT, command=self._send_text).pack(
            anchor="w", padx=12, pady=8
        )

        # History tab
        t_hist.grid_rowconfigure(0, weight=1)
        t_hist.grid_columnconfigure(0, weight=1)
        self.txt_hist = ctk.CTkTextbox(t_hist, font=ctk.CTkFont(family="Consolas", size=11), fg_color=Theme.SURFACE2)
        self.txt_hist.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)
        ctk.CTkButton(t_hist, text="Обновить", width=100, fg_color=Theme.SURFACE2, command=self._refresh_history).grid(
            row=1, column=0, padx=8, pady=4, sticky="w"
        )

        # Settings tab
        sf = ctk.CTkScrollableFrame(t_set, fg_color="transparent")
        sf.pack(fill="both", expand=True, padx=4, pady=4)

        ctk.CTkLabel(sf, text="VAD чувствительность", font=ctk.CTkFont(size=12)).pack(anchor="w", padx=8, pady=(8, 0))
        self.slider_vad = ctk.CTkSlider(sf, from_=0.1, to=0.95, number_of_steps=17, command=self._on_vad_slider)
        self.slider_vad.set(config.VAD_SENSITIVITY)
        self.slider_vad.pack(fill="x", padx=8, pady=4)
        self.lbl_vad_val = ctk.CTkLabel(sf, text=f"{config.VAD_SENSITIVITY:.2f}", text_color=Theme.MUTED, font=ctk.CTkFont(size=11))
        self.lbl_vad_val.pack(anchor="w", padx=8)

        ctk.CTkLabel(sf, text="Громкость TTS", font=ctk.CTkFont(size=12)).pack(anchor="w", padx=8, pady=(12, 0))
        self.slider_vol = ctk.CTkSlider(sf, from_=-50, to=50, number_of_steps=20, command=self._on_vol_slider)
        vol_num = int(config.TTS_VOLUME.replace("%", "").replace("+", "") or 0)
        self.slider_vol.set(vol_num)
        self.slider_vol.pack(fill="x", padx=8, pady=4)
        self.lbl_vol_val = ctk.CTkLabel(sf, text=config.TTS_VOLUME, text_color=Theme.MUTED, font=ctk.CTkFont(size=11))
        self.lbl_vol_val.pack(anchor="w", padx=8)

        ctk.CTkLabel(sf, text="Whisper модель", font=ctk.CTkFont(size=12)).pack(anchor="w", padx=8, pady=(12, 0))
        self.cmb_whisper = ctk.CTkComboBox(sf, values=list(config.WHISPER_MODELS), width=200)
        self.cmb_whisper.set(config.WHISPER_MODEL)
        self.cmb_whisper.pack(anchor="w", padx=8, pady=4)

        self.vars: dict = {}
        adv = [
            ("vad_silence_sec", "Пауза для стопа (сек)", config.VAD_SILENCE_SEC),
            ("vad_hangover_sec", "Hangover (сек)", config.VAD_HANGOVER_SEC),
            ("vad_pre_roll_sec", "Pre-roll (сек)", config.VAD_PRE_ROLL_SEC),
            ("whisper_device", "Device (auto/cpu/cuda)", config.WHISPER_DEVICE),
            ("ollama_model", "Ollama model", config.OLLAMA_MODEL),
        ]
        for key, label, val in adv:
            row = ctk.CTkFrame(sf, fg_color="transparent")
            row.pack(fill="x", padx=8, pady=4)
            ctk.CTkLabel(row, text=label, font=ctk.CTkFont(size=11), width=160, anchor="w").pack(side="left")
            e = ctk.CTkEntry(row, width=140, height=28)
            e.insert(0, str(val))
            e.pack(side="right")
            self.vars[key] = e

        # Telegram tab
        tg = ctk.CTkFrame(t_tg, fg_color="transparent")
        tg.pack(fill="both", expand=True, padx=8, pady=8)
        self.lbl_tg_status = ctk.CTkLabel(tg, text="Статус: проверка...", text_color=Theme.MUTED)
        self.lbl_tg_status.pack(anchor="w", pady=(0, 8))
        self.e_api_id = ctk.CTkEntry(tg, placeholder_text="API ID", height=34)
        self.e_api_id.insert(0, str(config.TELEGRAM_API_ID or ""))
        self.e_api_id.pack(fill="x", pady=4)
        self.e_api_hash = ctk.CTkEntry(tg, placeholder_text="API Hash", height=34)
        self.e_api_hash.insert(0, config.TELEGRAM_API_HASH)
        self.e_api_hash.pack(fill="x", pady=4)
        self.e_tg_contact = ctk.CTkEntry(tg, placeholder_text="Контакт для теста", height=34)
        self.e_tg_contact.pack(fill="x", pady=4)
        ctk.CTkLabel(tg, text="python telegram_client.py — авторизация", text_color=Theme.MUTED, font=ctk.CTkFont(size=11)).pack(anchor="w", pady=4)
        ctk.CTkButton(tg, text="Сохранить", fg_color=Theme.ACCENT, command=self._save_tg).pack(anchor="w", pady=8)
        ctk.CTkButton(tg, text="Проверить подключение", fg_color=Theme.SURFACE2, command=self._check_tg).pack(anchor="w")

    # ── Animation ─────────────────────────────────────────────────────────────

    def _start_pulse(self) -> None:
        self._pulse_tick()

    def _pulse_tick(self) -> None:
        self._pulse_phase += 0.12
        if self._wake_state in ("idle", "listening"):
            pulse = 0.55 + 0.45 * math.sin(self._pulse_phase)
            color = Theme.WAKE_IDLE
            size = int(88 + 6 * pulse)
            self.wake_orb.configure(fg_color=color, width=size, height=size)
            self.wake_ring.configure(border_color=color)
        elif self._wake_state == "recording":
            pulse = 0.5 + 0.5 * math.sin(self._pulse_phase * 2)
            self.wake_orb.configure(fg_color=Theme.WAKE_RECORD, width=int(90 + 8 * pulse), height=int(90 + 8 * pulse))
            self.wake_ring.configure(border_color=Theme.WAKE_RECORD)
        elif self._wake_state == "wake":
            self.wake_orb.configure(fg_color=Theme.WAKE_ACTIVE, width=92, height=92)
            self.wake_ring.configure(border_color=Theme.WAKE_ACTIVE)

        self._pulse_job = self.after(50, self._pulse_tick)

    def _set_wake_visual(self, state: str) -> None:
        self._wake_state = state
        title, color = _WAKE_STATES.get(state, ("—", Theme.MUTED))
        self.lbl_wake_title.configure(text=title)
        self.wake_orb.configure(fg_color=color)

    # ── Health poll ───────────────────────────────────────────────────────────

    def _start_health_poll(self) -> None:
        self._poll_health()

    def _poll_health(self) -> None:
        threading.Thread(target=self._health_worker, daemon=True).start()
        self._health_job = self.after(8000, self._poll_health)

    def _health_worker(self) -> None:
        ollama_ok = self.assistant.brain.check_connection()
        whisper_st = self.assistant.voice.get_whisper_status()
        whisper_ok = "ошибка" not in whisper_st.lower()
        try:
            import telegram_client as tg
            tg_ok, tg_msg = tg.check_connection()
        except Exception as exc:
            tg_ok, tg_msg = False, str(exc)[:40]

        def _apply():
            self.pill_ollama.set(ollama_ok, "OK" if ollama_ok else "offline")
            self.pill_whisper.set(whisper_ok, whisper_st[:24])
            self.pill_telegram.set(tg_ok, tg_msg)
            self.lbl_tg_status.configure(text=f"Telegram: {tg_msg}")

        try:
            self.after(0, _apply)
        except Exception:
            pass

    # ── Callbacks ─────────────────────────────────────────────────────────────

    def _on_assistant_status(self, status: str) -> None:
        def _do():
            low = status.lower()
            if "останов" in low:
                self._set_wake_visual("stopped")
            elif "wake" in low or "force" in low:
                self._set_wake_visual("wake")
            elif "запись" in low:
                self._set_wake_visual("recording")
            elif "думаю" in low or "выполня" in low:
                self._set_wake_visual("thinking")
            elif "говорю" in low:
                self._set_wake_visual("speaking")
            elif "жду" in low or "слушаю" in low:
                self._set_wake_visual("listening")
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
            if state == "recording":
                self._set_wake_visual("recording")
        try:
            self.after(0, _do)
        except Exception:
            pass

    def _append_log(self, msg: str) -> None:
        def _do():
            self.txt_log.insert("end", msg + "\n")
            # Ограничение размера лога
            lines = int(self.txt_log.index("end-1c").split(".")[0])
            if lines > 400:
                self.txt_log.delete("1.0", "100.0")
            self.txt_log.see("end")
        try:
            self.after(0, _do)
        except Exception:
            pass

    def _on_vad_slider(self, val: float) -> None:
        self.lbl_vad_val.configure(text=f"{val:.2f}")

    def _on_vol_slider(self, val: float) -> None:
        v = int(val)
        sign = "+" if v >= 0 else ""
        self.lbl_vol_val.configure(text=f"{sign}{v}%")

    # ── Actions ───────────────────────────────────────────────────────────────

    def _clear_log(self) -> None:
        self.txt_log.delete("1.0", "end")

    def _refresh_history(self) -> None:
        self.txt_hist.delete("1.0", "end")
        for entry in history.get_history(25):
            self.txt_hist.insert(
                "end",
                f"▸ {entry['time']}\n  {entry['command']}\n  ↳ {entry['response'][:120]}\n\n",
            )

    def _save(self) -> None:
        data = config.to_dict()
        data["vad_sensitivity"] = round(self.slider_vad.get(), 2)
        vol = int(self.slider_vol.get())
        data["tts_volume"] = f"{'+' if vol >= 0 else ''}{vol}%"
        data["whisper_model"] = self.cmb_whisper.get()
        float_keys = {"vad_silence_sec", "vad_hangover_sec", "vad_pre_roll_sec"}
        for key, entry in self.vars.items():
            val = entry.get().strip()
            try:
                data[key] = float(val) if key in float_keys else val
            except ValueError:
                data[key] = val
        config.SETTINGS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        config.reload_settings()
        messagebox.showinfo("Windy", "Настройки сохранены")

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
        self._check_tg()
        messagebox.showinfo("", "Telegram сохранён")

    def _check_tg(self) -> None:
        def _w():
            try:
                import telegram_client as tg
                ok, msg = tg.check_connection()
                self.after(0, lambda: self.lbl_tg_status.configure(text=f"Telegram: {msg}"))
                self.after(0, lambda: self.pill_telegram.set(ok, msg))
            except Exception as exc:
                self.after(0, lambda: self.lbl_tg_status.configure(text=f"Ошибка: {exc}"))
        threading.Thread(target=_w, daemon=True).start()

    def _reload(self) -> None:
        self.assistant.reload_settings()
        self.cmb_whisper.set(config.WHISPER_MODEL)
        self.slider_vad.set(config.VAD_SENSITIVITY)
        messagebox.showinfo("", "Перезагружено")

    def _start(self) -> None:
        if self.assistant.is_running:
            return
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self._set_wake_visual("listening")
        self.assistant.run_in_thread()
        self.after(600, self._poll_running)

    def _poll_running(self) -> None:
        if self.assistant.is_running:
            self.after(600, self._poll_running)
        else:
            self.btn_start.configure(state="normal")
            self.btn_stop.configure(state="disabled")
            self._set_wake_visual("stopped")

    def _stop(self) -> None:
        self.assistant.stop()
        self._set_wake_visual("stopped")

    def _force_wake(self) -> None:
        if not self.assistant.is_running:
            messagebox.showwarning("", "Сначала нажми «Запустить»")
            return
        threading.Thread(target=self.assistant.force_wake_cycle, daemon=True).start()

    def _test_ollama(self) -> None:
        ok = self.assistant.brain.check_connection()
        messagebox.showinfo("Ollama", "Подключено ✓" if ok else "Недоступно — запусти ollama serve")

    def _test_tts(self) -> None:
        threading.Thread(target=lambda: self.assistant.voice.speak("Привет, я Винди"), daemon=True).start()

    def _test_whisper(self) -> None:
        def _w():
            try:
                from voice import _get_whisper
                _get_whisper()
                self.after(0, lambda: messagebox.showinfo("Whisper", "Модель загружена ✓"))
            except Exception as exc:
                self.after(0, lambda: messagebox.showerror("Whisper", str(exc)))
        threading.Thread(target=_w, daemon=True).start()

    def _test_unread(self) -> None:
        r = self.assistant.tools.execute("telegram_get_unread", {})
        messagebox.showinfo("Непрочитанные", r[:900])

    def _send_text(self) -> None:
        text = self.entry_cmd.get().strip()
        if text:
            self.entry_cmd.delete(0, "end")
            threading.Thread(target=lambda: self._run_cmd(text), daemon=True).start()

    def _run_cmd(self, text: str) -> None:
        r = self.assistant.process_command(text)
        history.add_entry(text, r)
        self.after(0, self._refresh_history)

    def _close(self) -> None:
        if self._pulse_job:
            self.after_cancel(self._pulse_job)
        if self._health_job:
            self.after_cancel(self._health_job)
        remove_log_callback(self._log_fn)
        self.assistant.stop()
        self.destroy()


def run_gui() -> None:
    app = WindyGUI()
    app.mainloop()


if __name__ == "__main__":
    run_gui()