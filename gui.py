"""
Windy AI Assistant v6 — современный GUI (CustomTkinter).

Функции:
  - Анимация wake-word, live-логи, настройки VAD/Whisper
  - Управление приложениями: автоскан, чекбоксы, ручное добавление
  - Быстрый запуск из выпадающего меню
"""

from __future__ import annotations

import json
import math
import threading
from pathlib import Path
from tkinter import filedialog, messagebox

import bootstrap

bootstrap.ensure_project_path()

import customtkinter as ctk

import app_scanner
import config
import history
from main import WindyAssistant, add_log_callback, remove_log_callback, setup_logging

setup_logging()


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
    def __init__(self, master, label: str, **kwargs) -> None:
        super().__init__(master, fg_color=Theme.SURFACE2, corner_radius=20, **kwargs)
        self._dot = ctk.CTkLabel(self, text="●", font=ctk.CTkFont(size=14), text_color=Theme.MUTED)
        self._dot.pack(side="left", padx=(10, 4), pady=6)
        ctk.CTkLabel(self, text=label, font=ctk.CTkFont(size=12), text_color=Theme.MUTED).pack(side="left")
        self._val = ctk.CTkLabel(self, text="—", font=ctk.CTkFont(size=12, weight="bold"), text_color=Theme.TEXT)
        self._val.pack(side="left", padx=(4, 12), pady=6)

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
        self.geometry("1220x800")
        self.minsize(1040, 700)

        self.assistant = WindyAssistant()
        self.assistant.on_status(self._on_assistant_status)
        self._log_fn = self._append_log

        self._wake_state = "idle"
        self._pulse_phase = 0.0
        self._pulse_job: str | None = None
        self._health_job: str | None = None

        # Приложения: все найденные + чекбоксы
        self._scanned_apps: dict[str, str] = {}
        self._app_checks: dict[str, ctk.CTkCheckBox] = {}

        self._build()
        add_log_callback(self._log_fn)
        self.protocol("WM_DELETE_WINDOW", self._close)

        self.assistant.voice.set_mic_callback(self._on_mic_level)
        self.assistant.voice.set_vad_callback(self._on_vad_state)

        self._refresh_history()
        self._scan_apps_async(initial=True)
        self._start_pulse()
        self._start_health_poll()

    # ── Layout ────────────────────────────────────────────────────────────────

    def _build(self) -> None:
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self._build_sidebar()
        self._build_main()

    def _build_sidebar(self) -> None:
        side = ctk.CTkFrame(self, width=250, corner_radius=0, fg_color="#010409")
        side.grid(row=0, column=0, sticky="nsew")
        side.grid_propagate(False)

        logo = ctk.CTkFrame(side, fg_color="transparent")
        logo.pack(fill="x", padx=20, pady=(24, 4))
        ctk.CTkLabel(logo, text="🌬", font=ctk.CTkFont(size=32)).pack(side="left")
        ctk.CTkLabel(logo, text="Windy", font=ctk.CTkFont(size=26, weight="bold"), text_color=Theme.TEXT).pack(
            side="left", padx=(8, 0)
        )
        ctk.CTkLabel(side, text="AI Voice Assistant v6", font=ctk.CTkFont(size=11), text_color=Theme.MUTED).pack(
            anchor="w", padx=24, pady=(0, 14)
        )

        self.btn_start = ctk.CTkButton(
            side, text="▶  Запустить", height=42, corner_radius=10,
            fg_color=Theme.ACCENT, hover_color=config.GUI_ACCENT_HOVER,
            font=ctk.CTkFont(size=14, weight="bold"), command=self._start,
        )
        self.btn_start.pack(fill="x", padx=16, pady=4)
        self.btn_stop = ctk.CTkButton(
            side, text="■  Остановить", height=42, corner_radius=10,
            fg_color=Theme.DANGER, hover_color="#da3633", state="disabled", command=self._stop,
        )
        self.btn_stop.pack(fill="x", padx=16, pady=4)
        self.btn_force = ctk.CTkButton(
            side, text="⚡  Force Wake", height=36, corner_radius=10,
            fg_color=Theme.SURFACE2, hover_color=Theme.BORDER,
            border_width=1, border_color=Theme.BORDER, command=self._force_wake,
        )
        self.btn_force.pack(fill="x", padx=16, pady=(10, 4))

        # Быстрый запуск приложения
        ctk.CTkLabel(side, text="БЫСТРЫЙ ЗАПУСК", font=ctk.CTkFont(size=10, weight="bold"), text_color=Theme.MUTED).pack(
            anchor="w", padx=20, pady=(14, 4)
        )
        self.cmb_quick_app = ctk.CTkComboBox(side, values=["—"], height=34, command=self._on_quick_app)
        self.cmb_quick_app.set("—")
        self.cmb_quick_app.pack(fill="x", padx=16, pady=2)
        ctk.CTkButton(
            side, text="Открыть", height=30, fg_color=Theme.SURFACE2,
            hover_color=Theme.BORDER, command=self._launch_quick_app,
        ).pack(fill="x", padx=16, pady=(2, 8))

        ctk.CTkLabel(side, text="ТЕСТЫ", font=ctk.CTkFont(size=10, weight="bold"), text_color=Theme.MUTED).pack(
            anchor="w", padx=20, pady=(8, 4)
        )
        for txt, cmd in [
            ("🔊 TTS", self._test_tts),
            ("🎙 Whisper", self._test_whisper),
            ("🧠 Ollama", self._test_ollama),
            ("✉️ TG unread", self._test_unread),
        ]:
            ctk.CTkButton(
                side, text=txt, height=30, corner_radius=8,
                fg_color=Theme.SURFACE2, hover_color=Theme.BORDER, anchor="w", command=cmd,
            ).pack(fill="x", padx=16, pady=2)

        ctk.CTkFrame(side, fg_color=Theme.BORDER, height=1).pack(fill="x", padx=16, pady=12)
        ctk.CTkButton(side, text="💾 Сохранить всё", height=34, fg_color=Theme.SUCCESS, command=self._save_all).pack(
            fill="x", padx=16, pady=3
        )
        ctk.CTkButton(side, text="🔄 Reload", height=34, fg_color=Theme.SURFACE2, command=self._reload).pack(
            fill="x", padx=16, pady=3
        )

    def _build_main(self) -> None:
        main = ctk.CTkFrame(self, fg_color=Theme.BG, corner_radius=0)
        main.grid(row=0, column=1, sticky="nsew", padx=(0, 12), pady=12)
        main.grid_columnconfigure(0, weight=1)
        main.grid_rowconfigure(2, weight=1)

        # Status pills
        bar = ctk.CTkFrame(main, fg_color=Theme.SURFACE, corner_radius=12, border_width=1, border_color=Theme.BORDER)
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        pills = ctk.CTkFrame(bar, fg_color="transparent")
        pills.pack(fill="x", padx=12, pady=10)
        self.pill_ollama = StatusPill(pills, "Ollama")
        self.pill_ollama.pack(side="left", padx=(0, 8))
        self.pill_whisper = StatusPill(pills, "Whisper")
        self.pill_whisper.pack(side="left", padx=(0, 8))
        self.pill_telegram = StatusPill(pills, "Telegram")
        self.pill_telegram.pack(side="left")

        # Wake card
        wake_card = ctk.CTkFrame(main, fg_color=Theme.SURFACE, corner_radius=16, border_width=1, border_color=Theme.BORDER)
        wake_card.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        wake_card.grid_columnconfigure(1, weight=1)

        orb_frame = ctk.CTkFrame(wake_card, fg_color="transparent", width=120, height=120)
        orb_frame.grid(row=0, column=0, rowspan=2, padx=20, pady=14)
        orb_frame.grid_propagate(False)
        self.wake_orb = ctk.CTkFrame(orb_frame, width=88, height=88, corner_radius=44, fg_color=Theme.WAKE_IDLE)
        self.wake_orb.place(relx=0.5, rely=0.5, anchor="center")
        self.wake_ring = ctk.CTkFrame(
            orb_frame, width=100, height=100, corner_radius=50,
            fg_color="transparent", border_width=2, border_color=Theme.WAKE_IDLE,
        )
        self.wake_ring.place(relx=0.5, rely=0.5, anchor="center")

        self.lbl_wake_title = ctk.CTkLabel(
            wake_card, text="Ожидание wake-word",
            font=ctk.CTkFont(size=20, weight="bold"), text_color=Theme.TEXT, anchor="w",
        )
        self.lbl_wake_title.grid(row=0, column=1, sticky="w", pady=(18, 0))
        self.lbl_wake_hint = ctk.CTkLabel(
            wake_card, text='Скажи «Эй Винди» · «Hey Винди» · «Винди»',
            font=ctk.CTkFont(size=13), text_color=Theme.MUTED, anchor="w",
        )
        self.lbl_wake_hint.grid(row=1, column=1, sticky="w", pady=(4, 0))

        mic_box = ctk.CTkFrame(wake_card, fg_color=Theme.SURFACE2, corner_radius=10)
        mic_box.grid(row=0, column=2, rowspan=2, padx=20, pady=14)
        ctk.CTkLabel(mic_box, text="Микрофон", text_color=Theme.MUTED, font=ctk.CTkFont(size=11)).pack(padx=14, pady=(8, 2))
        self.mic_bar = ctk.CTkProgressBar(mic_box, width=170, height=10, progress_color=Theme.ACCENT)
        self.mic_bar.pack(padx=14, pady=4)
        self.mic_bar.set(0)
        self.lbl_vad = ctk.CTkLabel(mic_box, text="VAD: —", text_color=Theme.MUTED, font=ctk.CTkFont(size=11))
        self.lbl_vad.pack(padx=14, pady=(2, 10))

        # Content area
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
        hdr = ctk.CTkFrame(log_card, fg_color="transparent")
        hdr.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 4))
        ctk.CTkLabel(hdr, text="Живые логи", font=ctk.CTkFont(size=14, weight="bold")).pack(side="left")
        ctk.CTkButton(hdr, text="Очистить", width=80, height=26, fg_color=Theme.SURFACE2, command=self._clear_log).pack(side="right")
        self.txt_log = ctk.CTkTextbox(log_card, font=ctk.CTkFont(family="Consolas", size=11), fg_color=Theme.SURFACE2)
        self.txt_log.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 12))

        # Tabs
        right = ctk.CTkTabview(content, fg_color=Theme.SURFACE, segmented_button_fg_color=Theme.SURFACE2)
        right.grid(row=0, column=1, sticky="nsew")
        t_cmd = right.add("Команда")
        t_apps = right.add("Приложения")
        t_hist = right.add("История")
        t_set = right.add("Настройки")
        t_tg = right.add("Telegram")

        # Command
        ctk.CTkLabel(t_cmd, text="Текстовая команда", font=ctk.CTkFont(weight="bold")).pack(anchor="w", padx=12, pady=(12, 4))
        self.entry_cmd = ctk.CTkEntry(t_cmd, placeholder_text="Открой телеграм", height=38)
        self.entry_cmd.pack(fill="x", padx=12, pady=4)
        self.entry_cmd.bind("<Return>", lambda _e: self._send_text())
        ctk.CTkButton(t_cmd, text="Отправить", height=36, fg_color=Theme.ACCENT, command=self._send_text).pack(anchor="w", padx=12, pady=8)

        # Apps tab
        self._build_apps_tab(t_apps)

        # History
        t_hist.grid_rowconfigure(0, weight=1)
        t_hist.grid_columnconfigure(0, weight=1)
        self.txt_hist = ctk.CTkTextbox(t_hist, font=ctk.CTkFont(family="Consolas", size=11), fg_color=Theme.SURFACE2)
        self.txt_hist.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)
        ctk.CTkButton(t_hist, text="Обновить", width=100, fg_color=Theme.SURFACE2, command=self._refresh_history).grid(
            row=1, column=0, padx=8, pady=4, sticky="w"
        )

        # Settings
        sf = ctk.CTkScrollableFrame(t_set, fg_color="transparent")
        sf.pack(fill="both", expand=True, padx=4, pady=4)
        ctk.CTkLabel(sf, text="VAD чувствительность", font=ctk.CTkFont(size=12)).pack(anchor="w", padx=8, pady=(8, 0))
        self.slider_vad = ctk.CTkSlider(sf, from_=0.1, to=0.95, number_of_steps=17, command=self._on_vad_slider)
        self.slider_vad.set(config.VAD_SENSITIVITY)
        self.slider_vad.pack(fill="x", padx=8, pady=4)
        self.lbl_vad_val = ctk.CTkLabel(sf, text=f"{config.VAD_SENSITIVITY:.2f}", text_color=Theme.MUTED)
        self.lbl_vad_val.pack(anchor="w", padx=8)
        ctk.CTkLabel(sf, text="Громкость TTS", font=ctk.CTkFont(size=12)).pack(anchor="w", padx=8, pady=(12, 0))
        self.slider_vol = ctk.CTkSlider(sf, from_=-50, to=50, number_of_steps=20, command=self._on_vol_slider)
        vol_num = int(config.TTS_VOLUME.replace("%", "").replace("+", "") or 0)
        self.slider_vol.set(vol_num)
        self.slider_vol.pack(fill="x", padx=8, pady=4)
        self.lbl_vol_val = ctk.CTkLabel(sf, text=config.TTS_VOLUME, text_color=Theme.MUTED)
        self.lbl_vol_val.pack(anchor="w", padx=8)
        ctk.CTkLabel(sf, text="Whisper модель", font=ctk.CTkFont(size=12)).pack(anchor="w", padx=8, pady=(12, 0))
        self.cmb_whisper = ctk.CTkComboBox(sf, values=list(config.WHISPER_MODELS), width=200)
        self.cmb_whisper.set(config.WHISPER_MODEL)
        self.cmb_whisper.pack(anchor="w", padx=8, pady=4)
        self.vars: dict = {}
        for key, label, val in [
            ("vad_silence_sec", "Пауза VAD (сек)", config.VAD_SILENCE_SEC),
            ("vad_hangover_sec", "Hangover (сек)", config.VAD_HANGOVER_SEC),
            ("vad_pre_roll_sec", "Pre-roll (сек)", config.VAD_PRE_ROLL_SEC),
            ("whisper_device", "Whisper device", config.WHISPER_DEVICE),
            ("ollama_model", "Ollama model", config.OLLAMA_MODEL),
        ]:
            row = ctk.CTkFrame(sf, fg_color="transparent")
            row.pack(fill="x", padx=8, pady=4)
            ctk.CTkLabel(row, text=label, width=150, anchor="w", font=ctk.CTkFont(size=11)).pack(side="left")
            e = ctk.CTkEntry(row, width=150, height=28)
            e.insert(0, str(val))
            e.pack(side="right")
            self.vars[key] = e

        # Telegram
        tg = ctk.CTkFrame(t_tg, fg_color="transparent")
        tg.pack(fill="both", expand=True, padx=8, pady=8)
        self.lbl_tg_status = ctk.CTkLabel(tg, text="Статус: —", text_color=Theme.MUTED)
        self.lbl_tg_status.pack(anchor="w", pady=(0, 8))
        self.e_api_id = ctk.CTkEntry(tg, placeholder_text="API ID", height=34)
        self.e_api_id.insert(0, str(config.TELEGRAM_API_ID or ""))
        self.e_api_id.pack(fill="x", pady=4)
        self.e_api_hash = ctk.CTkEntry(tg, placeholder_text="API Hash", height=34)
        self.e_api_hash.insert(0, config.TELEGRAM_API_HASH)
        self.e_api_hash.pack(fill="x", pady=4)
        ctk.CTkButton(tg, text="Сохранить", fg_color=Theme.ACCENT, command=self._save_tg).pack(anchor="w", pady=8)
        ctk.CTkButton(tg, text="Проверить", fg_color=Theme.SURFACE2, command=self._check_tg).pack(anchor="w")

    def _build_apps_tab(self, parent) -> None:
        parent.grid_rowconfigure(1, weight=1)
        parent.grid_columnconfigure(0, weight=1)

        toolbar = ctk.CTkFrame(parent, fg_color="transparent")
        toolbar.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))
        ctk.CTkLabel(toolbar, text="Приложения для голосовых команд", font=ctk.CTkFont(weight="bold")).pack(side="left")
        self.lbl_app_count = ctk.CTkLabel(toolbar, text="", text_color=Theme.MUTED, font=ctk.CTkFont(size=11))
        self.lbl_app_count.pack(side="right", padx=8)
        self.btn_scan = ctk.CTkButton(
            toolbar, text="🔄 Обновить список", width=150, height=30,
            fg_color=Theme.ACCENT, command=lambda: self._scan_apps_async(initial=False),
        )
        self.btn_scan.pack(side="right")

        self.apps_scroll = ctk.CTkScrollableFrame(parent, fg_color=Theme.SURFACE2, corner_radius=8)
        self.apps_scroll.grid(row=1, column=0, sticky="nsew", padx=8, pady=4)

        add_row = ctk.CTkFrame(parent, fg_color="transparent")
        add_row.grid(row=2, column=0, sticky="ew", padx=8, pady=8)
        self.e_app_name = ctk.CTkEntry(add_row, placeholder_text="Имя (chrome)", width=110, height=32)
        self.e_app_name.pack(side="left", padx=(0, 4))
        self.e_app_path = ctk.CTkEntry(add_row, placeholder_text="Путь к .exe", height=32)
        self.e_app_path.pack(side="left", fill="x", expand=True, padx=4)
        ctk.CTkButton(add_row, text="📁", width=36, height=32, fg_color=Theme.SURFACE2, command=self._browse_app).pack(side="left", padx=2)
        ctk.CTkButton(add_row, text="+ Добавить", width=100, height=32, fg_color=Theme.SUCCESS, command=self._add_manual_app).pack(side="left", padx=2)

        btn_row = ctk.CTkFrame(parent, fg_color="transparent")
        btn_row.grid(row=3, column=0, sticky="ew", padx=8, pady=(0, 8))
        ctk.CTkButton(btn_row, text="Выбрать все", width=110, height=28, fg_color=Theme.SURFACE2, command=self._select_all_apps).pack(side="left", padx=2)
        ctk.CTkButton(btn_row, text="Снять все", width=110, height=28, fg_color=Theme.SURFACE2, command=self._deselect_all_apps).pack(side="left", padx=2)
        ctk.CTkButton(btn_row, text="▶ Тест запуска", width=120, height=28, fg_color=Theme.SURFACE2, command=self._test_launch_app).pack(side="left", padx=2)
        ctk.CTkButton(btn_row, text="💾 Сохранить приложения", height=28, fg_color=Theme.ACCENT, command=self._save_apps).pack(side="right", padx=2)

    # ── Apps management ───────────────────────────────────────────────────────

    def _scan_apps_async(self, *, initial: bool = False) -> None:
        def _work():
            try:
                config.invalidate_app_cache()
                scanned = app_scanner.scan_installed_apps()
                merged = app_scanner.merge_with_manual(scanned, config.APP_PATHS_MANUAL)
                self.after(0, lambda: self._populate_apps(merged, initial=initial))
            except Exception as exc:
                self.after(0, lambda: messagebox.showerror("Сканирование", str(exc)))

        self.btn_scan.configure(state="disabled", text="Сканирую...")
        threading.Thread(target=_work, daemon=True).start()

    def _populate_apps(self, apps: dict[str, str], *, initial: bool = False) -> None:
        self._scanned_apps = apps
        enabled = set(config.APP_PATHS.keys())

        for w in self.apps_scroll.winfo_children():
            w.destroy()
        self._app_checks.clear()

        for name, path in apps.items():
            row = ctk.CTkFrame(self.apps_scroll, fg_color="transparent")
            row.pack(fill="x", padx=6, pady=3)
            label = app_scanner.label_for(name)
            short_path = path if len(path) <= 52 else "…" + path[-50:]
            checked = name in enabled if initial or name in enabled else name in config.APP_PATHS
            var = ctk.StringVar(value="on" if checked else "off")
            cb = ctk.CTkCheckBox(
                row, text=f"{label}  ({name})", variable=var, onvalue="on", offvalue="off",
                font=ctk.CTkFont(size=12, weight="bold"),
            )
            if checked:
                cb.select()
            cb.pack(anchor="w")
            ctk.CTkLabel(row, text=short_path, text_color=Theme.MUTED, font=ctk.CTkFont(size=10)).pack(anchor="w", padx=28)
            self._app_checks[name] = cb

        self.lbl_app_count.configure(text=f"{len(apps)} найдено")
        self.btn_scan.configure(state="normal", text="🔄 Обновить список")
        self._update_quick_launch()

    def _get_enabled_apps(self) -> dict[str, str]:
        enabled: dict[str, str] = {}
        for name, cb in self._app_checks.items():
            try:
                checked = cb.get() == 1
            except Exception:
                checked = bool(cb.cget("variable"))  # fallback
            if checked:
                path = self._scanned_apps.get(name) or config.APP_PATHS.get(name, "")
                if path:
                    enabled[name] = path
        return enabled

    def _test_launch_app(self) -> None:
        """Тест запуска первого отмеченного приложения."""
        enabled = self._get_enabled_apps()
        if not enabled:
            messagebox.showwarning("", "Отметь хотя бы одно приложение")
            return
        name = next(iter(enabled))
        def _w():
            r = self.assistant.tools.execute("open_app", {"name": name})
            self.after(0, lambda: messagebox.showinfo("Тест", r))
        threading.Thread(target=_w, daemon=True).start()

    def _save_apps(self) -> None:
        enabled = self._get_enabled_apps()
        if not enabled:
            messagebox.showwarning("", "Выбери хотя бы одно приложение")
            return
        config.set_app_paths(enabled, config.APP_PATHS_MANUAL)
        self.assistant.reload_settings()
        self._update_quick_launch()
        messagebox.showinfo("Windy", f"Сохранено {len(enabled)} приложений")

    def _add_manual_app(self) -> None:
        name = self.e_app_name.get().strip().lower()
        path = self.e_app_path.get().strip()
        if not name or not path:
            messagebox.showwarning("", "Укажи имя и путь")
            return
        if not path.lower().endswith(".exe") and not path.lower().endswith(".bat"):
            messagebox.showwarning("", "Укажи путь к .exe")
            return
        manual = dict(config.APP_PATHS_MANUAL)
        manual[name] = path
        config.APP_PATHS_MANUAL = manual
        merged = app_scanner.merge_with_manual(self._scanned_apps, manual)
        if name not in merged:
            merged[name] = path
        self._scanned_apps = merged
        self._populate_apps(merged)
        self.e_app_name.delete(0, "end")
        self.e_app_path.delete(0, "end")
        # Автовыбор нового
        if name in self._app_checks:
            self._app_checks[name].select()

    def _browse_app(self) -> None:
        p = filedialog.askopenfilename(filetypes=[("Executable", "*.exe"), ("All", "*.*")])
        if p:
            self.e_app_path.delete(0, "end")
            self.e_app_path.insert(0, p)
            if not self.e_app_name.get().strip():
                self.e_app_name.insert(0, Path(p).stem.lower())

    def _select_all_apps(self) -> None:
        for cb in self._app_checks.values():
            cb.select()

    def _deselect_all_apps(self) -> None:
        for cb in self._app_checks.values():
            cb.deselect()

    def _update_quick_launch(self) -> None:
        names = sorted(config.APP_PATHS.keys())
        labels = [f"{app_scanner.label_for(n)} ({n})" for n in names] or ["—"]
        self.cmb_quick_app.configure(values=labels)
        self.cmb_quick_app.set(labels[0])

    def _on_quick_app(self, _choice: str) -> None:
        pass

    def _launch_quick_app(self) -> None:
        sel = self.cmb_quick_app.get()
        if sel == "—":
            return
        if "(" in sel and ")" in sel:
            alias = sel.rsplit("(", 1)[1].rstrip(")")
        else:
            alias = sel

        def _w():
            r = self.assistant.tools.execute("open_app", {"name": alias})
            self._append_log(f"[quick] {alias}: {r}")

        threading.Thread(target=_w, daemon=True).start()

    # ── Animation & health (from v5) ──────────────────────────────────────────

    def _start_pulse(self) -> None:
        self._pulse_tick()

    def _pulse_tick(self) -> None:
        self._pulse_phase += 0.12
        if self._wake_state in ("idle", "listening"):
            pulse = 0.55 + 0.45 * math.sin(self._pulse_phase)
            self.wake_orb.configure(fg_color=Theme.WAKE_IDLE, width=int(88 + 6 * pulse), height=int(88 + 6 * pulse))
            self.wake_ring.configure(border_color=Theme.WAKE_IDLE)
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

    def _start_health_poll(self) -> None:
        self._poll_health()

    def _poll_health(self) -> None:
        threading.Thread(target=self._health_worker, daemon=True).start()
        self._health_job = self.after(8000, self._poll_health)

    def _health_worker(self) -> None:
        health = self.assistant.get_service_health()

        def _apply():
            o_ok, o_msg = health["ollama"]
            w_ok, w_msg = health["whisper"]
            t_ok, t_msg = health["telegram"]
            self.pill_ollama.set(o_ok, o_msg)
            self.pill_whisper.set(w_ok, w_msg[:24])
            self.pill_telegram.set(t_ok, t_msg)
            self.lbl_tg_status.configure(text=f"Telegram: {t_msg}")

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
        try:
            self.after(0, lambda: self.mic_bar.set(max(0.0, min(1.0, level))))
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
            if int(self.txt_log.index("end-1c").split(".")[0]) > 400:
                self.txt_log.delete("1.0", "80.0")
            self.txt_log.see("end")
        try:
            self.after(0, _do)
        except Exception:
            pass

    def _on_vad_slider(self, val: float) -> None:
        self.lbl_vad_val.configure(text=f"{val:.2f}")

    def _on_vol_slider(self, val: float) -> None:
        v = int(val)
        self.lbl_vol_val.configure(text=f"{'+' if v >= 0 else ''}{v}%")

    # ── Actions ───────────────────────────────────────────────────────────────

    def _clear_log(self) -> None:
        self.txt_log.delete("1.0", "end")

    def _refresh_history(self) -> None:
        self.txt_hist.delete("1.0", "end")
        for entry in history.get_history(25):
            self.txt_hist.insert("end", f"▸ {entry['time']}\n  {entry['command']}\n  ↳ {entry['response'][:120]}\n\n")

    def _save_all(self) -> None:
        self._save_settings_only()
        if self._app_checks:
            self._save_apps()

    def _save_settings_only(self) -> None:
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
        self._scan_apps_async(initial=True)
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
        messagebox.showinfo("Ollama", "OK ✓" if ok else "Запусти ollama serve")

    def _test_tts(self) -> None:
        threading.Thread(target=lambda: self.assistant.voice.speak("Привет, я Винди"), daemon=True).start()

    def _test_whisper(self) -> None:
        def _w():
            try:
                from voice import _get_whisper
                _get_whisper()
                self.after(0, lambda: messagebox.showinfo("Whisper", "OK ✓"))
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
    WindyGUI().mainloop()


if __name__ == "__main__":
    run_gui()