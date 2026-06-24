"""
Windy AI Assistant v8.1 — современный GUI (CustomTkinter).

Страницы:
  - Главная: wake-word, VAD, быстрая команда
  - Приложения: автоскан + ручное добавление
  - Telegram: настройки, авторизация, чтение/отправка
  - Логи: живой вывод
  - Настройки: VAD, Whisper, Ollama
"""

from __future__ import annotations

import json
import math
import threading
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog

import bootstrap

bootstrap.ensure_project_path()

import customtkinter as ctk

import app_scanner
import config
import history
from main import WindyAssistant, add_log_callback, remove_log_callback, setup_logging

setup_logging()


class Theme:
    BG = "#0a0e14"
    SIDEBAR = "#0d1117"
    SURFACE = "#151b23"
    SURFACE2 = "#1a2230"
    CARD = "#1e2736"
    BORDER = "#2d3748"
    TEXT = "#f0f4f8"
    MUTED = "#94a3b8"
    ACCENT = config.GUI_ACCENT
    ACCENT_SOFT = "#312e81"
    SUCCESS = "#22c55e"
    WARNING = "#f59e0b"
    DANGER = "#ef4444"
    TG_BLUE = "#229ed9"
    WAKE_IDLE = "#60a5fa"
    WAKE_ACTIVE = "#34d399"
    WAKE_RECORD = "#fb7185"


_VAD_LABELS = {
    "lead_in": "Микрофон (TTS)",
    "calibrating": "Калибровка",
    "waiting": "Ожидание речи",
    "recording": "Запись",
    "done": "Готово",
    "timeout": "Таймаут",
    "max_duration": "Лимит",
    "error": "Ошибка",
}

_WAKE_STATES = {
    "idle": ("Ожидание wake-word", Theme.WAKE_IDLE),
    "listening": ("Слушаю wake-word…", Theme.WAKE_IDLE),
    "wake": ("Wake-word!", Theme.WAKE_ACTIVE),
    "recording": ("Запись команды…", Theme.WAKE_RECORD),
    "thinking": ("Думаю…", Theme.WARNING),
    "speaking": ("Говорю…", Theme.ACCENT),
    "stopped": ("Остановлен", Theme.MUTED),
}

_PAGES = [
    ("home", "🏠  Главная"),
    ("apps", "📦  Приложения"),
    ("telegram", "✈️  Telegram"),
    ("logs", "📋  Логи"),
    ("settings", "⚙️  Настройки"),
]


class StatusPill(ctk.CTkFrame):
    def __init__(self, master, label: str, **kwargs) -> None:
        super().__init__(master, fg_color=Theme.SURFACE2, corner_radius=18, **kwargs)
        self._dot = ctk.CTkLabel(self, text="●", font=ctk.CTkFont(size=13))
        self._dot.pack(side="left", padx=(12, 4), pady=8)
        ctk.CTkLabel(self, text=label, font=ctk.CTkFont(size=11), text_color=Theme.MUTED).pack(side="left")
        self._val = ctk.CTkLabel(self, text="—", font=ctk.CTkFont(size=11, weight="bold"), text_color=Theme.TEXT)
        self._val.pack(side="left", padx=(4, 12))

    def set(self, ok: bool | None, text: str) -> None:
        color = Theme.SUCCESS if ok else (Theme.DANGER if ok is False else Theme.MUTED)
        self._dot.configure(text_color=color)
        self._val.configure(text=text[:32])


class NavButton(ctk.CTkButton):
    def __init__(self, master, page_id: str, label: str, command, **kwargs) -> None:
        super().__init__(
            master, text=label, anchor="w", height=40, corner_radius=10,
            fg_color="transparent", hover_color=Theme.SURFACE2,
            text_color=Theme.MUTED, font=ctk.CTkFont(size=13),
            command=command, **kwargs,
        )
        self.page_id = page_id
        self._active = False

    def set_active(self, active: bool) -> None:
        self._active = active
        if active:
            self.configure(fg_color=Theme.ACCENT_SOFT, text_color=Theme.TEXT, hover_color=Theme.ACCENT_SOFT)
        else:
            self.configure(fg_color="transparent", text_color=Theme.MUTED, hover_color=Theme.SURFACE2)


class WindyGUI(ctk.CTk):
    def __init__(self) -> None:
        super().__init__(fg_color=Theme.BG)
        ctk.set_appearance_mode(config.GUI_THEME)
        ctk.set_default_color_theme("blue")

        self.title("Windy AI Assistant")
        self.geometry("1280x820")
        self.minsize(1100, 720)

        self.assistant = WindyAssistant()
        self.assistant.on_status(self._on_assistant_status)
        self._log_fn = self._append_log

        self._wake_state = "idle"
        self._pulse_phase = 0.0
        self._pulse_job: str | None = None
        self._health_job: str | None = None
        self._current_page = "home"

        self._scanned_apps: dict[str, str] = {}
        self._app_checks: dict[str, ctk.CTkCheckBox] = {}
        self._nav_buttons: dict[str, NavButton] = {}
        self._pages: dict[str, ctk.CTkFrame] = {}
        self._tg_dialogs: list[dict] = []

        self._build()
        add_log_callback(self._log_fn)
        self.protocol("WM_DELETE_WINDOW", self._close)

        self.assistant.voice.set_mic_callback(self._on_mic_level)
        self.assistant.voice.set_vad_callback(self._on_vad_state)

        self._refresh_history()
        self._scan_apps_async(initial=True)
        self._start_pulse()
        self._start_health_poll()
        self._show_page("home")

    # ── Shell ─────────────────────────────────────────────────────────────────

    def _build(self) -> None:
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self._build_sidebar()
        self._content_host = ctk.CTkFrame(self, fg_color=Theme.BG, corner_radius=0)
        self._content_host.grid(row=0, column=1, sticky="nsew", padx=(0, 16), pady=16)
        self._content_host.grid_columnconfigure(0, weight=1)
        self._content_host.grid_rowconfigure(0, weight=1)
        self._build_pages()

    def _build_sidebar(self) -> None:
        side = ctk.CTkFrame(self, width=240, corner_radius=0, fg_color=Theme.SIDEBAR)
        side.grid(row=0, column=0, sticky="nsew")
        side.grid_propagate(False)

        hdr = ctk.CTkFrame(side, fg_color="transparent")
        hdr.pack(fill="x", padx=20, pady=(28, 6))
        ctk.CTkLabel(hdr, text="🌬", font=ctk.CTkFont(size=28)).pack(side="left")
        col = ctk.CTkFrame(hdr, fg_color="transparent")
        col.pack(side="left", padx=(10, 0))
        ctk.CTkLabel(col, text="Windy", font=ctk.CTkFont(size=22, weight="bold"), text_color=Theme.TEXT).pack(anchor="w")
        ctk.CTkLabel(col, text=f"AI Assistant {config.GUI_VERSION}", font=ctk.CTkFont(size=10), text_color=Theme.MUTED).pack(anchor="w")

        ctk.CTkFrame(side, fg_color=Theme.BORDER, height=1).pack(fill="x", padx=16, pady=14)

        nav = ctk.CTkFrame(side, fg_color="transparent")
        nav.pack(fill="x", padx=12)
        for pid, label in _PAGES:
            btn = NavButton(nav, pid, label, lambda p=pid: self._show_page(p))
            btn.pack(fill="x", pady=2)
            self._nav_buttons[pid] = btn

        ctk.CTkFrame(side, fg_color=Theme.BORDER, height=1).pack(fill="x", padx=16, pady=14)

        self.btn_start = ctk.CTkButton(
            side, text="▶  Запустить", height=42, corner_radius=10,
            fg_color=Theme.ACCENT, hover_color=config.GUI_ACCENT_HOVER,
            font=ctk.CTkFont(size=13, weight="bold"), command=self._start,
        )
        self.btn_start.pack(fill="x", padx=16, pady=3)
        self.btn_stop = ctk.CTkButton(
            side, text="■  Стоп", height=38, corner_radius=10,
            fg_color=Theme.DANGER, hover_color="#dc2626", state="disabled", command=self._stop,
        )
        self.btn_stop.pack(fill="x", padx=16, pady=3)
        self.btn_force = ctk.CTkButton(
            side, text="⚡  Force Wake", height=34, corner_radius=10,
            fg_color=Theme.SURFACE2, hover_color=Theme.BORDER,
            border_width=1, border_color=Theme.BORDER, command=self._force_wake,
        )
        self.btn_force.pack(fill="x", padx=16, pady=(8, 4))

        ctk.CTkLabel(side, text="БЫСТРЫЙ ЗАПУСК", font=ctk.CTkFont(size=9, weight="bold"), text_color=Theme.MUTED).pack(anchor="w", padx=20, pady=(10, 2))
        self.cmb_quick_app = ctk.CTkComboBox(side, values=["—"], height=32)
        self.cmb_quick_app.set("—")
        self.cmb_quick_app.pack(fill="x", padx=16, pady=2)
        ctk.CTkButton(side, text="Открыть", height=28, fg_color=Theme.SURFACE2, command=self._launch_quick_app).pack(fill="x", padx=16, pady=(2, 4))

        ctk.CTkLabel(side, text="БЫСТРЫЙ САЙТ", font=ctk.CTkFont(size=9, weight="bold"), text_color=Theme.MUTED).pack(anchor="w", padx=20, pady=(6, 2))
        self.cmb_quick_site = ctk.CTkComboBox(side, values=list(config.BROWSER_QUICK_SITES), height=32)
        self.cmb_quick_site.set(config.BROWSER_QUICK_SITES[0])
        self.cmb_quick_site.pack(fill="x", padx=16, pady=2)
        ctk.CTkButton(side, text="В браузере", height=28, fg_color=Theme.SURFACE2, command=self._launch_quick_site).pack(fill="x", padx=16, pady=(2, 8))

        bottom = ctk.CTkFrame(side, fg_color="transparent")
        bottom.pack(side="bottom", fill="x", padx=16, pady=16)
        ctk.CTkButton(bottom, text="💾 Сохранить", height=32, fg_color=Theme.SUCCESS, command=self._save_all).pack(fill="x", pady=2)
        ctk.CTkButton(bottom, text="🔄 Reload", height=32, fg_color=Theme.SURFACE2, command=self._reload).pack(fill="x", pady=2)

    def _build_pages(self) -> None:
        for pid, _ in _PAGES:
            frame = ctk.CTkFrame(self._content_host, fg_color="transparent")
            frame.grid(row=0, column=0, sticky="nsew")
            self._pages[pid] = frame

        self._build_home_page(self._pages["home"])
        self._build_apps_page(self._pages["apps"])
        self._build_telegram_page(self._pages["telegram"])
        self._build_logs_page(self._pages["logs"])
        self._build_settings_page(self._pages["settings"])

    def _show_page(self, page_id: str) -> None:
        self._current_page = page_id
        for pid, frame in self._pages.items():
            if pid == page_id:
                frame.tkraise()
            btn = self._nav_buttons.get(pid)
            if btn:
                btn.set_active(pid == page_id)
        if page_id == "telegram":
            self._refresh_tg_auth_status()
            self._refresh_tg_dialogs_async()

    # ── Home ──────────────────────────────────────────────────────────────────

    def _build_home_page(self, parent) -> None:
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(2, weight=1)

        bar = ctk.CTkFrame(parent, fg_color=Theme.SURFACE, corner_radius=14, border_width=1, border_color=Theme.BORDER)
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        pills = ctk.CTkFrame(bar, fg_color="transparent")
        pills.pack(fill="x", padx=14, pady=12)
        self.pill_ollama = StatusPill(pills, "Ollama")
        self.pill_ollama.pack(side="left", padx=(0, 8))
        self.pill_whisper = StatusPill(pills, "Whisper")
        self.pill_whisper.pack(side="left", padx=(0, 8))
        self.pill_telegram = StatusPill(pills, "Telegram")
        self.pill_telegram.pack(side="left")

        wake = ctk.CTkFrame(parent, fg_color=Theme.SURFACE, corner_radius=16, border_width=1, border_color=Theme.BORDER)
        wake.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        wake.grid_columnconfigure(1, weight=1)

        orb_f = ctk.CTkFrame(wake, fg_color="transparent", width=130, height=130)
        orb_f.grid(row=0, column=0, rowspan=2, padx=24, pady=16)
        orb_f.grid_propagate(False)
        self.wake_orb = ctk.CTkFrame(orb_f, width=90, height=90, corner_radius=45, fg_color=Theme.WAKE_IDLE)
        self.wake_orb.place(relx=0.5, rely=0.5, anchor="center")
        self.wake_ring = ctk.CTkFrame(orb_f, width=104, height=104, corner_radius=52, fg_color="transparent", border_width=2, border_color=Theme.WAKE_IDLE)
        self.wake_ring.place(relx=0.5, rely=0.5, anchor="center")

        self.lbl_wake_title = ctk.CTkLabel(wake, text="Ожидание wake-word", font=ctk.CTkFont(size=22, weight="bold"), anchor="w")
        self.lbl_wake_title.grid(row=0, column=1, sticky="w", pady=(20, 0))
        self.lbl_wake_hint = ctk.CTkLabel(
            wake, text='Скажи «Эй Винди» · «Hey Винди» · «Винди»',
            font=ctk.CTkFont(size=13), text_color=Theme.MUTED, anchor="w",
        )
        self.lbl_wake_hint.grid(row=1, column=1, sticky="w", pady=(4, 0))

        mic = ctk.CTkFrame(wake, fg_color=Theme.SURFACE2, corner_radius=12)
        mic.grid(row=0, column=2, rowspan=2, padx=24, pady=16)
        ctk.CTkLabel(mic, text="Микрофон", text_color=Theme.MUTED, font=ctk.CTkFont(size=11)).pack(padx=16, pady=(10, 4))
        self.mic_bar = ctk.CTkProgressBar(mic, width=180, height=10, progress_color=Theme.ACCENT)
        self.mic_bar.pack(padx=16, pady=4)
        self.mic_bar.set(0)
        self.lbl_vad = ctk.CTkLabel(mic, text="VAD: —", text_color=Theme.MUTED, font=ctk.CTkFont(size=11))
        self.lbl_vad.pack(padx=16, pady=(4, 12))

        cmd_card = ctk.CTkFrame(parent, fg_color=Theme.SURFACE, corner_radius=14, border_width=1, border_color=Theme.BORDER)
        cmd_card.grid(row=2, column=0, sticky="nsew")
        cmd_card.grid_columnconfigure(0, weight=1)
        cmd_card.grid_rowconfigure(2, weight=1)

        ch = ctk.CTkFrame(cmd_card, fg_color="transparent")
        ch.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 6))
        ctk.CTkLabel(ch, text="Быстрая команда", font=ctk.CTkFont(size=15, weight="bold")).pack(side="left")

        row = ctk.CTkFrame(cmd_card, fg_color="transparent")
        row.grid(row=0, column=0, sticky="e", padx=16, pady=(14, 6))
        self.entry_cmd = ctk.CTkEntry(row, placeholder_text="Открой грок / вк / поищи в гугле …", width=420, height=38)
        self.entry_cmd.pack(side="left", padx=(0, 8))
        self.entry_cmd.bind("<Return>", lambda _e: self._send_text())
        ctk.CTkButton(row, text="Отправить", width=110, height=38, fg_color=Theme.ACCENT, command=self._send_text).pack(side="left")

        sites_row = ctk.CTkFrame(cmd_card, fg_color="transparent")
        sites_row.grid(row=1, column=0, sticky="w", padx=16, pady=(0, 8))
        for site in ("youtube", "вк", "грок", "google", "steam"):
            ctk.CTkButton(
                sites_row, text=site, width=72, height=26, corner_radius=8,
                fg_color=Theme.SURFACE2, hover_color=Theme.BORDER,
                font=ctk.CTkFont(size=11),
                command=lambda s=site: self._quick_browser_chip(s),
            ).pack(side="left", padx=(0, 6))

        hist_wrap = ctk.CTkFrame(cmd_card, fg_color=Theme.SURFACE2, corner_radius=10)
        hist_wrap.grid(row=2, column=0, sticky="nsew", padx=16, pady=(0, 16))
        hist_wrap.grid_rowconfigure(0, weight=1)
        hist_wrap.grid_columnconfigure(0, weight=1)
        self.txt_hist = ctk.CTkTextbox(hist_wrap, font=ctk.CTkFont(family="Consolas", size=11), fg_color="transparent")
        self.txt_hist.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)

    # ── Apps ──────────────────────────────────────────────────────────────────

    def _build_apps_page(self, parent) -> None:
        parent.grid_rowconfigure(1, weight=1)
        parent.grid_columnconfigure(0, weight=1)

        toolbar = ctk.CTkFrame(parent, fg_color="transparent")
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ctk.CTkLabel(toolbar, text="Приложения для голосовых команд", font=ctk.CTkFont(size=18, weight="bold")).pack(side="left")
        self.lbl_app_count = ctk.CTkLabel(toolbar, text="", text_color=Theme.MUTED)
        self.lbl_app_count.pack(side="right", padx=8)
        self.btn_scan = ctk.CTkButton(toolbar, text="🔄 Обновить", width=130, fg_color=Theme.ACCENT, command=lambda: self._scan_apps_async(initial=False))
        self.btn_scan.pack(side="right")

        self.apps_scroll = ctk.CTkScrollableFrame(parent, fg_color=Theme.SURFACE, corner_radius=12, border_width=1, border_color=Theme.BORDER)
        self.apps_scroll.grid(row=1, column=0, sticky="nsew", pady=4)

        add_row = ctk.CTkFrame(parent, fg_color=Theme.SURFACE2, corner_radius=10)
        add_row.grid(row=2, column=0, sticky="ew", pady=10)
        inner = ctk.CTkFrame(add_row, fg_color="transparent")
        inner.pack(fill="x", padx=12, pady=10)
        self.e_app_name = ctk.CTkEntry(inner, placeholder_text="Имя (chrome)", width=120, height=34)
        self.e_app_name.pack(side="left", padx=(0, 6))
        self.e_app_path = ctk.CTkEntry(inner, placeholder_text="Путь к .exe", height=34)
        self.e_app_path.pack(side="left", fill="x", expand=True, padx=6)
        ctk.CTkButton(inner, text="📁", width=36, height=34, fg_color=Theme.CARD, command=self._browse_app).pack(side="left", padx=2)
        ctk.CTkButton(inner, text="+ Добавить", width=100, height=34, fg_color=Theme.SUCCESS, command=self._add_manual_app).pack(side="left", padx=4)

        btn_row = ctk.CTkFrame(parent, fg_color="transparent")
        btn_row.grid(row=3, column=0, sticky="ew")
        for txt, cmd, w in [
            ("Выбрать все", self._select_all_apps, 110),
            ("Снять все", self._deselect_all_apps, 110),
            ("▶ Тест", self._test_launch_app, 90),
        ]:
            ctk.CTkButton(btn_row, text=txt, width=w, height=30, fg_color=Theme.SURFACE2, command=cmd).pack(side="left", padx=3)
        ctk.CTkButton(btn_row, text="💾 Сохранить", height=30, fg_color=Theme.ACCENT, command=self._save_apps).pack(side="right", padx=3)

    # ── Telegram ────────────────────────────────────────────────────────────

    def _build_telegram_page(self, parent) -> None:
        parent.grid_columnconfigure(0, weight=2)
        parent.grid_columnconfigure(1, weight=3)
        parent.grid_rowconfigure(0, weight=1)

        # Левая колонка — настройки
        left = ctk.CTkFrame(parent, fg_color=Theme.SURFACE, corner_radius=14, border_width=1, border_color=Theme.BORDER)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        lf = ctk.CTkScrollableFrame(left, fg_color="transparent")
        lf.pack(fill="both", expand=True, padx=12, pady=12)

        ctk.CTkLabel(lf, text="Telegram", font=ctk.CTkFont(size=18, weight="bold")).pack(anchor="w", pady=(0, 4))
        self.lbl_tg_status = ctk.CTkLabel(lf, text="Статус: —", text_color=Theme.MUTED, font=ctk.CTkFont(size=12))
        self.lbl_tg_status.pack(anchor="w", pady=(0, 4))
        self.lbl_tg_auth = ctk.CTkLabel(
            lf, text="Авторизация: проверка…", text_color=Theme.MUTED,
            font=ctk.CTkFont(size=12, weight="bold"),
        )
        self.lbl_tg_auth.pack(anchor="w", pady=(0, 12))

        ctk.CTkLabel(lf, text="API ID", font=ctk.CTkFont(size=11), text_color=Theme.MUTED).pack(anchor="w")
        self.e_api_id = ctk.CTkEntry(lf, height=36)
        self.e_api_id.insert(0, str(config.TELEGRAM_API_ID or ""))
        self.e_api_id.pack(fill="x", pady=(2, 8))

        ctk.CTkLabel(lf, text="API Hash", font=ctk.CTkFont(size=11), text_color=Theme.MUTED).pack(anchor="w")
        self.e_api_hash = ctk.CTkEntry(lf, height=36)
        self.e_api_hash.insert(0, config.TELEGRAM_API_HASH)
        self.e_api_hash.pack(fill="x", pady=(2, 8))

        ctk.CTkLabel(lf, text="Телефон (+7…)", font=ctk.CTkFont(size=11), text_color=Theme.MUTED).pack(anchor="w")
        self.e_phone = ctk.CTkEntry(lf, placeholder_text="+79991234567", height=36)
        self.e_phone.insert(0, config.TELEGRAM_PHONE)
        self.e_phone.pack(fill="x", pady=(2, 8))

        ctk.CTkLabel(lf, text="Контакт по умолчанию", font=ctk.CTkFont(size=11), text_color=Theme.MUTED).pack(anchor="w")
        self.e_default_contact = ctk.CTkEntry(lf, placeholder_text="Имя или @username", height=36)
        self.e_default_contact.insert(0, config.TELEGRAM_DEFAULT_CONTACT)
        self.e_default_contact.pack(fill="x", pady=(2, 12))

        auth_row = ctk.CTkFrame(lf, fg_color="transparent")
        auth_row.pack(fill="x", pady=4)
        ctk.CTkButton(auth_row, text="💾 Сохранить", height=34, fg_color=Theme.ACCENT, command=self._save_tg).pack(side="left", padx=(0, 6))
        ctk.CTkButton(auth_row, text="✓ Проверить", height=34, fg_color=Theme.SURFACE2, command=self._check_tg).pack(side="left")

        ctk.CTkFrame(lf, fg_color=Theme.BORDER, height=1).pack(fill="x", pady=14)
        ctk.CTkLabel(lf, text="Авторизация", font=ctk.CTkFont(size=13, weight="bold")).pack(anchor="w")
        ctk.CTkLabel(lf, text="1. Сохрани API → 2. Отправить код → 3. Ввести код", text_color=Theme.MUTED, font=ctk.CTkFont(size=11)).pack(anchor="w", pady=(2, 8))

        self.btn_tg_send_code = ctk.CTkButton(
            lf, text="📲 Отправить код", height=36, fg_color=Theme.TG_BLUE, command=self._tg_send_code,
        )
        self.btn_tg_send_code.pack(fill="x", pady=4)

        ctk.CTkLabel(lf, text="Код из Telegram", font=ctk.CTkFont(size=11), text_color=Theme.MUTED).pack(anchor="w", pady=(8, 0))
        self.e_tg_code = ctk.CTkEntry(lf, placeholder_text="12345", height=36)
        self.e_tg_code.pack(fill="x", pady=(2, 4))

        ctk.CTkLabel(lf, text="Пароль 2FA (если есть)", font=ctk.CTkFont(size=11), text_color=Theme.MUTED).pack(anchor="w")
        self.e_tg_2fa = ctk.CTkEntry(lf, placeholder_text="опционально", height=36, show="•")
        self.e_tg_2fa.pack(fill="x", pady=(2, 8))

        self.btn_tg_sign_in = ctk.CTkButton(
            lf, text="🔐 Войти", height=36, fg_color=Theme.SUCCESS, command=self._tg_sign_in,
        )
        self.btn_tg_sign_in.pack(fill="x")

        # Правая колонка — чтение/отправка
        right = ctk.CTkFrame(parent, fg_color=Theme.SURFACE, corner_radius=14, border_width=1, border_color=Theme.BORDER)
        right.grid(row=0, column=1, sticky="nsew")
        right.grid_rowconfigure(2, weight=1)
        right.grid_columnconfigure(0, weight=1)

        top = ctk.CTkFrame(right, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=14, pady=(14, 8))
        ctk.CTkLabel(top, text="Сообщения", font=ctk.CTkFont(size=16, weight="bold")).pack(side="left")

        ctrl = ctk.CTkFrame(right, fg_color=Theme.SURFACE2, corner_radius=10)
        ctrl.grid(row=1, column=0, sticky="ew", padx=14, pady=4)
        cr = ctk.CTkFrame(ctrl, fg_color="transparent")
        cr.pack(fill="x", padx=10, pady=10)

        ctk.CTkLabel(cr, text="Контакт:", font=ctk.CTkFont(size=11), text_color=Theme.MUTED).pack(side="left", padx=(0, 6))
        self.cmb_tg_contact = ctk.CTkComboBox(cr, values=["—"], width=220, height=32, command=self._on_tg_contact_pick)
        self.cmb_tg_contact.set("—")
        self.cmb_tg_contact.pack(side="left", padx=4)
        self.e_tg_contact = ctk.CTkEntry(cr, placeholder_text="или введи имя", width=160, height=32)
        self.e_tg_contact.pack(side="left", padx=4)

        ctk.CTkLabel(cr, text="Кол-во:", font=ctk.CTkFont(size=11), text_color=Theme.MUTED).pack(side="left", padx=(12, 4))
        self.cmb_tg_count = ctk.CTkComboBox(cr, values=["5", "10", "15", "20"], width=60, height=32)
        self.cmb_tg_count.set(str(config.TELEGRAM_READ_DEFAULT_COUNT))
        self.cmb_tg_count.pack(side="left")

        ctk.CTkButton(cr, text="📖 Прочитать", width=110, height=32, fg_color=Theme.ACCENT, command=self._tg_read).pack(side="left", padx=(12, 4))
        ctk.CTkButton(cr, text="📬 Непрочитанные", width=130, height=32, fg_color=Theme.SURFACE, command=self._tg_unread).pack(side="left", padx=4)
        ctk.CTkButton(cr, text="🔄 Чаты", width=70, height=32, fg_color=Theme.SURFACE, command=self._refresh_tg_dialogs_async).pack(side="left", padx=4)

        self.txt_tg_msgs = ctk.CTkTextbox(right, font=ctk.CTkFont(family="Segoe UI", size=13), fg_color=Theme.SURFACE2, wrap="word")
        self.txt_tg_msgs.grid(row=2, column=0, sticky="nsew", padx=14, pady=(8, 8))
        self.txt_tg_msgs.insert("1.0", "Выбери контакт и нажми «Прочитать»\n")

        send_row = ctk.CTkFrame(right, fg_color=Theme.CARD, corner_radius=10)
        send_row.grid(row=3, column=0, sticky="ew", padx=14, pady=(0, 14))
        sr = ctk.CTkFrame(send_row, fg_color="transparent")
        sr.pack(fill="x", padx=10, pady=10)
        self.e_tg_send = ctk.CTkEntry(sr, placeholder_text="Текст сообщения…", height=36)
        self.e_tg_send.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.e_tg_send.bind("<Return>", lambda _e: self._tg_send())
        ctk.CTkButton(sr, text="Отправить ✈️", width=120, height=36, fg_color=Theme.TG_BLUE, command=self._tg_send).pack(side="right")

    # ── Logs ──────────────────────────────────────────────────────────────────

    def _build_logs_page(self, parent) -> None:
        parent.grid_rowconfigure(1, weight=1)
        parent.grid_columnconfigure(0, weight=1)

        hdr = ctk.CTkFrame(parent, fg_color="transparent")
        hdr.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ctk.CTkLabel(hdr, text="Живые логи", font=ctk.CTkFont(size=18, weight="bold")).pack(side="left")
        ctk.CTkButton(hdr, text="Очистить", width=90, height=30, fg_color=Theme.SURFACE2, command=self._clear_log).pack(side="right")

        self.txt_log = ctk.CTkTextbox(parent, font=ctk.CTkFont(family="Consolas", size=11), fg_color=Theme.SURFACE, corner_radius=12, border_width=1, border_color=Theme.BORDER)
        self.txt_log.grid(row=1, column=0, sticky="nsew")

    # ── Settings ────────────────────────────────────────────────────────────

    def _build_settings_page(self, parent) -> None:
        sf = ctk.CTkScrollableFrame(parent, fg_color=Theme.SURFACE, corner_radius=14, border_width=1, border_color=Theme.BORDER)
        sf.pack(fill="both", expand=True)
        inner = ctk.CTkFrame(sf, fg_color="transparent")
        inner.pack(fill="x", padx=16, pady=16)

        ctk.CTkLabel(inner, text="Настройки", font=ctk.CTkFont(size=18, weight="bold")).pack(anchor="w", pady=(0, 12))

        ctk.CTkLabel(inner, text="VAD чувствительность", font=ctk.CTkFont(size=12)).pack(anchor="w")
        self.slider_vad = ctk.CTkSlider(inner, from_=0.1, to=0.95, number_of_steps=17, command=self._on_vad_slider)
        self.slider_vad.set(config.VAD_SENSITIVITY)
        self.slider_vad.pack(fill="x", pady=4)
        self.lbl_vad_val = ctk.CTkLabel(inner, text=f"{config.VAD_SENSITIVITY:.2f}", text_color=Theme.MUTED)
        self.lbl_vad_val.pack(anchor="w", pady=(0, 10))

        ctk.CTkLabel(inner, text="Громкость TTS", font=ctk.CTkFont(size=12)).pack(anchor="w")
        self.slider_vol = ctk.CTkSlider(inner, from_=-50, to=50, number_of_steps=20, command=self._on_vol_slider)
        vol_num = int(config.TTS_VOLUME.replace("%", "").replace("+", "") or 0)
        self.slider_vol.set(vol_num)
        self.slider_vol.pack(fill="x", pady=4)
        self.lbl_vol_val = ctk.CTkLabel(inner, text=config.TTS_VOLUME, text_color=Theme.MUTED)
        self.lbl_vol_val.pack(anchor="w", pady=(0, 10))

        ctk.CTkLabel(inner, text="Whisper модель", font=ctk.CTkFont(size=12)).pack(anchor="w")
        self.cmb_whisper = ctk.CTkComboBox(inner, values=list(config.WHISPER_MODELS), width=200)
        self.cmb_whisper.set(config.WHISPER_MODEL)
        self.cmb_whisper.pack(anchor="w", pady=(2, 12))

        self.vars: dict = {}
        for key, label, val in [
            ("vad_release_sec", "Пауза VAD release (сек)", config.VAD_RELEASE_SEC),
            ("vad_hangover_sec", "Hangover (сек)", config.VAD_HANGOVER_SEC),
            ("vad_pre_roll_sec", "Pre-roll (сек)", config.VAD_PRE_ROLL_SEC),
            ("whisper_device", "Whisper device", config.WHISPER_DEVICE),
            ("ollama_model", "Ollama model", config.OLLAMA_MODEL),
            ("telegram_read_default_count", "TG: сообщений по умолчанию", config.TELEGRAM_READ_DEFAULT_COUNT),
        ]:
            row = ctk.CTkFrame(inner, fg_color="transparent")
            row.pack(fill="x", pady=5)
            ctk.CTkLabel(row, text=label, width=220, anchor="w", font=ctk.CTkFont(size=11)).pack(side="left")
            e = ctk.CTkEntry(row, width=160, height=30)
            e.insert(0, str(val))
            e.pack(side="right")
            self.vars[key] = e

        ctk.CTkButton(inner, text="💾 Сохранить настройки", height=36, fg_color=Theme.ACCENT, command=self._save_settings_only).pack(anchor="w", pady=(16, 0))

    # ── Apps logic ────────────────────────────────────────────────────────────

    def _scan_apps_async(self, *, initial: bool = False) -> None:
        def _work():
            try:
                config.invalidate_app_cache()
                scanned = app_scanner.scan_installed_apps()
                merged = app_scanner.merge_with_manual(scanned, config.APP_PATHS_MANUAL)
                self.after(0, lambda: self._populate_apps(merged, initial=initial))
            except Exception as exc:
                self.after(0, lambda: messagebox.showerror("Сканирование", str(exc)))

        self.btn_scan.configure(state="disabled", text="Сканирую…")
        threading.Thread(target=_work, daemon=True).start()

    def _populate_apps(self, apps: dict[str, str], *, initial: bool = False) -> None:
        self._scanned_apps = apps
        enabled = set(config.APP_PATHS.keys())
        for w in self.apps_scroll.winfo_children():
            w.destroy()
        self._app_checks.clear()

        for name, path in sorted(apps.items()):
            row = ctk.CTkFrame(self.apps_scroll, fg_color=Theme.SURFACE2, corner_radius=8)
            row.pack(fill="x", padx=4, pady=3)
            inner = ctk.CTkFrame(row, fg_color="transparent")
            inner.pack(fill="x", padx=10, pady=8)
            label = app_scanner.label_for(name)
            short_path = path if len(path) <= 55 else "…" + path[-53:]
            checked = name in enabled if initial or name in enabled else name in config.APP_PATHS
            cb = ctk.CTkCheckBox(inner, text=f"{label}  ({name})", font=ctk.CTkFont(size=12, weight="bold"))
            if checked:
                cb.select()
            cb.pack(anchor="w")
            ctk.CTkLabel(inner, text=short_path, text_color=Theme.MUTED, font=ctk.CTkFont(size=10)).pack(anchor="w", padx=26)
            self._app_checks[name] = cb

        self.lbl_app_count.configure(text=f"{len(apps)} найдено")
        self.btn_scan.configure(state="normal", text="🔄 Обновить")
        self._update_quick_launch()

    def _get_enabled_apps(self) -> dict[str, str]:
        enabled: dict[str, str] = {}
        for name, cb in self._app_checks.items():
            try:
                checked = cb.get() == 1
            except Exception:
                checked = False
            if checked:
                path = self._scanned_apps.get(name) or config.APP_PATHS.get(name, "")
                if path:
                    enabled[name] = path
        return enabled

    def _test_launch_app(self) -> None:
        enabled = self._get_enabled_apps()
        if not enabled:
            messagebox.showwarning("", "Отметь хотя бы одно приложение")
            return
        name = next(iter(enabled))
        threading.Thread(target=lambda: self._run_tool_result("open_app", {"name": name}, "Тест"), daemon=True).start()

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
        manual = dict(config.APP_PATHS_MANUAL)
        manual[name] = path
        config.APP_PATHS_MANUAL = manual
        merged = app_scanner.merge_with_manual(self._scanned_apps, manual)
        merged.setdefault(name, path)
        self._scanned_apps = merged
        self._populate_apps(merged)
        self.e_app_name.delete(0, "end")
        self.e_app_path.delete(0, "end")
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

    def _launch_quick_app(self) -> None:
        sel = self.cmb_quick_app.get()
        if sel == "—":
            return
        alias = sel.rsplit("(", 1)[1].rstrip(")") if "(" in sel else sel
        threading.Thread(
            target=lambda: self._append_log(f"[quick] {alias}: {self.assistant.tools.execute('open_app', {'name': alias})}"),
            daemon=True,
        ).start()

    def _launch_quick_site(self) -> None:
        site = self.cmb_quick_site.get().strip()
        if not site:
            return
        threading.Thread(
            target=lambda: self._append_log(
                f"[browser] {site}: {self.assistant.tools.execute('open_browser', {'query': site})}"
            ),
            daemon=True,
        ).start()

    def _quick_browser_chip(self, site: str) -> None:
        threading.Thread(
            target=lambda: self._append_log(
                f"[browser] {site}: {self.assistant.tools.execute('open_browser', {'query': site})}"
            ),
            daemon=True,
        ).start()

    # ── Telegram logic ────────────────────────────────────────────────────────

    def _tg_contact_value(self) -> str:
        manual = self.e_tg_contact.get().strip()
        if manual:
            return manual
        sel = self.cmb_tg_contact.get()
        if sel and sel != "—":
            return sel.split(" (")[0].strip()
        return config.TELEGRAM_DEFAULT_CONTACT.strip()

    def _on_tg_contact_pick(self, choice: str) -> None:
        if choice and choice != "—":
            self.e_tg_contact.delete(0, "end")
            self.e_tg_contact.insert(0, choice.split(" (")[0].strip())

    def _refresh_tg_dialogs_async(self) -> None:
        def _work():
            try:
                import telegram_client as tg
                dialogs = tg.list_dialogs()
                self.after(0, lambda: self._apply_tg_dialogs(dialogs))
            except Exception as exc:
                self.after(0, lambda: self.lbl_tg_status.configure(text=f"Чаты: {exc}"))

        threading.Thread(target=_work, daemon=True).start()

    def _apply_tg_dialogs(self, dialogs: list[dict]) -> None:
        self._tg_dialogs = dialogs
        labels = [d.get("label", d.get("name", "?")) for d in dialogs] or ["—"]
        self.cmb_tg_contact.configure(values=labels)
        if labels[0] != "—":
            self.cmb_tg_contact.set(labels[0])
        self.lbl_tg_status.configure(text=f"Загружено {len(dialogs)} чатов")

    def _display_tg_text(self, text: str) -> None:
        self.txt_tg_msgs.delete("1.0", "end")
        self.txt_tg_msgs.insert("1.0", text)
        if self._current_page != "telegram":
            self._show_page("telegram")

    def _tg_read(self) -> None:
        contact = self._tg_contact_value()
        if not contact:
            messagebox.showwarning("", "Укажи контакт")
            return
        try:
            count = int(self.cmb_tg_count.get())
        except ValueError:
            count = 5

        def _work():
            import telegram_client as tg
            try:
                msgs = tg.read_last_raw(contact, count)
                text = tg.format_messages(msgs, contact)
            except Exception as exc:
                text = str(exc)
            self.after(0, lambda: self._display_tg_text(text))
            self.after(0, lambda: self._append_log(f"[TG read] {contact}: {len(text)} chars"))

        self.txt_tg_msgs.delete("1.0", "end")
        self.txt_tg_msgs.insert("1.0", "Загрузка…\n")
        threading.Thread(target=_work, daemon=True).start()

    def _tg_unread(self) -> None:
        def _work():
            r = self.assistant.tools.execute("telegram_get_unread", {"limit": 12})
            self.after(0, lambda: self._display_tg_text(r))

        threading.Thread(target=_work, daemon=True).start()

    def _tg_send(self) -> None:
        text = self.e_tg_send.get().strip()
        contact = self._tg_contact_value()
        if not text or not contact:
            messagebox.showwarning("", "Контакт и текст обязательны")
            return
        self.e_tg_send.delete(0, "end")

        def _work():
            r = self.assistant.tools.execute("telegram_send_message", {"contact": contact, "message": text})
            self.after(0, lambda: self._append_log(f"[TG send] {r}"))
            self.after(0, lambda: messagebox.showinfo("Telegram", r[:200]))

        threading.Thread(target=_work, daemon=True).start()

    def _set_tg_auth_badge(self, state: str, message: str, ok: bool | None = None) -> None:
        colors = {
            "authorized": Theme.SUCCESS,
            "code_sent": Theme.WARNING,
            "ready": Theme.WAKE_IDLE,
            "not_configured": Theme.MUTED,
            "error": Theme.DANGER,
        }
        icons = {
            "authorized": "✓",
            "code_sent": "⏳",
            "ready": "○",
            "not_configured": "—",
            "error": "✗",
        }
        color = colors.get(state, Theme.MUTED)
        icon = icons.get(state, "•")
        self.lbl_tg_auth.configure(text=f"{icon} {message}", text_color=color)
        if ok is not None:
            self.pill_telegram.set(ok, message[:32])

    def _refresh_tg_auth_status(self) -> None:
        def _work():
            try:
                import telegram_client as tg
                st = tg.get_auth_status()
                self.after(0, lambda: self._set_tg_auth_badge(st["state"], st["message"], st.get("ok")))
                self.after(0, lambda: self.lbl_tg_status.configure(text=f"Telegram: {st['message']}"))
            except Exception as exc:
                self.after(0, lambda: self._set_tg_auth_badge("error", str(exc)[:80], False))

        threading.Thread(target=_work, daemon=True).start()

    def _save_tg(self, *, silent: bool = False) -> bool:
        data = config.to_dict()
        try:
            data["telegram_api_id"] = int(self.e_api_id.get() or 0)
        except ValueError:
            messagebox.showerror("", "API ID — число")
            return False
        data["telegram_api_hash"] = self.e_api_hash.get().strip()
        data["telegram_phone"] = self.e_phone.get().strip()
        data["telegram_default_contact"] = self.e_default_contact.get().strip()
        config.SETTINGS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        config.reload_settings()
        try:
            import telegram_client as tg
            tg.reset_client()
        except Exception:
            pass
        self._refresh_tg_auth_status()
        if not silent:
            messagebox.showinfo("", "Telegram настройки сохранены")
        return True

    def _tg_send_code(self) -> None:
        phone = self.e_phone.get().strip() or config.TELEGRAM_PHONE
        if not phone:
            messagebox.showwarning("", "Укажи телефон (+7999…)")
            return
        if not self._save_tg(silent=True):
            return

        self.btn_tg_send_code.configure(state="disabled", text="Отправка…")
        self._set_tg_auth_badge("ready", "Отправляем код…", False)

        def _work():
            import telegram_client as tg
            ok, msg = tg.send_code(phone)

            def _done():
                self.btn_tg_send_code.configure(state="normal", text="📲 Отправить код")
                if ok:
                    self._set_tg_auth_badge("code_sent", msg, False)
                    messagebox.showinfo("Код отправлен", msg)
                else:
                    self._set_tg_auth_badge("error", msg, False)
                    messagebox.showerror("Ошибка отправки кода", msg)
                self._refresh_tg_auth_status()

            self.after(0, _done)

        threading.Thread(target=_work, daemon=True).start()

    def _tg_sign_in(self) -> None:
        phone = self.e_phone.get().strip() or config.TELEGRAM_PHONE
        code = self.e_tg_code.get().strip()
        password = self.e_tg_2fa.get().strip()
        if not phone or not code:
            messagebox.showwarning("", "Телефон и код обязательны")
            return

        self.btn_tg_sign_in.configure(state="disabled", text="Вход…")
        self._set_tg_auth_badge("code_sent", "Авторизация…", False)

        def _work():
            import telegram_client as tg
            ok, msg = tg.sign_in(phone, code, password)

            def _done():
                self.btn_tg_sign_in.configure(state="normal", text="🔐 Войти")
                if ok:
                    self.e_tg_code.delete(0, "end")
                    self._set_tg_auth_badge("authorized", msg, True)
                    messagebox.showinfo("Telegram", msg)
                    self._refresh_tg_dialogs_async()
                else:
                    self._set_tg_auth_badge("error", msg, False)
                    messagebox.showerror("Ошибка входа", msg)
                self._refresh_tg_auth_status()

            self.after(0, _done)

        threading.Thread(target=_work, daemon=True).start()

    def _check_tg(self) -> None:
        self._refresh_tg_auth_status()

    # ── Animation & health ────────────────────────────────────────────────────

    def _start_pulse(self) -> None:
        self._pulse_tick()

    def _pulse_tick(self) -> None:
        self._pulse_phase += 0.12
        if self._wake_state in ("idle", "listening"):
            pulse = 0.55 + 0.45 * math.sin(self._pulse_phase)
            sz = int(90 + 6 * pulse)
            self.wake_orb.configure(fg_color=Theme.WAKE_IDLE, width=sz, height=sz)
            self.wake_ring.configure(border_color=Theme.WAKE_IDLE)
        elif self._wake_state == "recording":
            pulse = 0.5 + 0.5 * math.sin(self._pulse_phase * 2)
            sz = int(92 + 8 * pulse)
            self.wake_orb.configure(fg_color=Theme.WAKE_RECORD, width=sz, height=sz)
            self.wake_ring.configure(border_color=Theme.WAKE_RECORD)
        elif self._wake_state == "wake":
            self.wake_orb.configure(fg_color=Theme.WAKE_ACTIVE, width=94, height=94)
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
            self.pill_whisper.set(w_ok, w_msg[:28])
            self.pill_telegram.set(t_ok, t_msg)
            if self._current_page == "telegram":
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
            if int(self.txt_log.index("end-1c").split(".")[0]) > 500:
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
        self.lbl_vol_val.configure(text=f"{'+' if v >= 0 else ''}{v}%")

    # ── Actions ───────────────────────────────────────────────────────────────

    def _clear_log(self) -> None:
        self.txt_log.delete("1.0", "end")

    def _refresh_history(self) -> None:
        self.txt_hist.delete("1.0", "end")
        for entry in history.get_history(30):
            self.txt_hist.insert("end", f"▸ {entry['time']}\n  {entry['command']}\n  ↳ {entry['response'][:140]}\n\n")

    def _run_tool_result(self, tool: str, params: dict, title: str) -> None:
        r = self.assistant.tools.execute(tool, params)
        self.after(0, lambda: messagebox.showinfo(title, r[:500]))

    def _save_all(self) -> None:
        self._save_settings_only()
        if self._app_checks:
            self._save_apps()
        self._save_tg()

    def _save_settings_only(self) -> None:
        data = config.to_dict()
        data["vad_sensitivity"] = round(self.slider_vad.get(), 2)
        vol = int(self.slider_vol.get())
        data["tts_volume"] = f"{'+' if vol >= 0 else ''}{vol}%"
        data["whisper_model"] = self.cmb_whisper.get()
        float_keys = {"vad_release_sec", "vad_hangover_sec", "vad_pre_roll_sec"}
        int_keys = {"telegram_read_default_count"}
        for key, entry in self.vars.items():
            val = entry.get().strip()
            try:
                if key in float_keys:
                    data[key] = float(val)
                elif key in int_keys:
                    data[key] = int(val)
                else:
                    data[key] = val
            except ValueError:
                data[key] = val
        config.SETTINGS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        config.reload_settings()
        self.assistant.reload_settings()
        messagebox.showinfo("Windy", "Настройки сохранены")

    def _reload(self) -> None:
        self.assistant.reload_settings()
        self.cmb_whisper.set(config.WHISPER_MODEL)
        self.slider_vad.set(config.VAD_SENSITIVITY)
        self._scan_apps_async(initial=True)
        self._check_tg()
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