"""
GUI Windy AI Assistant (tkinter).
Вкладки: Статус | Настройки | Приложения | Telegram | Логи
"""

from __future__ import annotations

import json
import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

import bootstrap

bootstrap.ensure_project_path()

import config
from main import WindyAssistant, add_log_callback, remove_log_callback, setup_logging
from tools import TelegramBot, _get_bot

setup_logging()


class WindyGUI:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Windy AI Assistant")
        self.root.geometry("860x680")
        self.root.minsize(720, 520)

        self.assistant = WindyAssistant()
        self._log_cb = self._append_log

        self._build_ui()
        add_log_callback(self._log_cb)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        nb = ttk.Notebook(self.root)
        nb.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        # --- Статус ---
        tab_status = ttk.Frame(nb)
        nb.add(tab_status, text="Статус")

        self.status_lbl = ttk.Label(tab_status, text="Статус: остановлен", font=("Segoe UI", 11))
        self.status_lbl.pack(anchor=tk.W, padx=10, pady=8)

        btns = ttk.Frame(tab_status)
        btns.pack(anchor=tk.W, padx=10)
        self.btn_start = ttk.Button(btns, text="▶ Старт", command=self._start)
        self.btn_start.pack(side=tk.LEFT, padx=3)
        self.btn_stop = ttk.Button(btns, text="■ Стоп", command=self._stop, state=tk.DISABLED)
        self.btn_stop.pack(side=tk.LEFT, padx=3)
        ttk.Button(btns, text="Ollama", command=self._test_ollama).pack(side=tk.LEFT, padx=3)
        ttk.Button(btns, text="TTS", command=self._test_tts).pack(side=tk.LEFT, padx=3)
        ttk.Button(btns, text="Whisper", command=self._test_whisper).pack(side=tk.LEFT, padx=3)

        mf = ttk.LabelFrame(tab_status, text="Текстовая команда")
        mf.pack(fill=tk.X, padx=10, pady=8)
        self.manual_entry = ttk.Entry(mf)
        self.manual_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5, pady=5)
        self.manual_entry.bind("<Return>", lambda e: self._manual_command())
        ttk.Button(mf, text="Отправить", command=self._manual_command).pack(side=tk.LEFT, padx=5)

        self.info_lbl = ttk.Label(tab_status, text=self._info_text(), justify=tk.LEFT)
        self.info_lbl.pack(anchor=tk.W, padx=10, pady=5)

        # --- Настройки ---
        tab_set = ttk.Frame(nb)
        nb.add(tab_set, text="Настройки")

        sf = ttk.Frame(tab_set)
        sf.pack(fill=tk.BOTH, expand=True, padx=10, pady=8)

        self.vars: dict[str, tk.Variable] = {}
        fields: list[tuple[str, str, object]] = [
            ("wake_word", "Wake-word", config.WAKE_WORD),
            ("ollama_model", "Ollama модель", config.OLLAMA_MODEL),
            ("whisper_model", "Whisper модель", config.WHISPER_MODEL),
            ("whisper_device", "Whisper device (cpu/cuda/auto)", config.WHISPER_DEVICE),
            ("whisper_compute_type", "Whisper compute (int8)", config.WHISPER_COMPUTE_TYPE),
            ("tts_voice", "TTS голос", config.TTS_VOICE),
            ("vad_silence_sec", "VAD тишина (сек)", config.VAD_SILENCE_SEC),
            ("vad_hangover_sec", "VAD hangover (сек)", config.VAD_HANGOVER_SEC),
            ("post_tts_delay_sec", "Пауза после TTS (сек)", config.POST_TTS_DELAY_SEC),
            ("ollama_num_ctx", "Ollama num_ctx", config.OLLAMA_NUM_CTX),
            ("ollama_num_gpu", "Ollama num_gpu (-1=авто)", config.OLLAMA_NUM_GPU),
        ]
        for i, (key, label, default) in enumerate(fields):
            ttk.Label(sf, text=label).grid(row=i, column=0, sticky=tk.W, pady=2)
            if isinstance(default, float):
                var: tk.Variable = tk.DoubleVar(value=default)
            elif isinstance(default, int):
                var = tk.IntVar(value=default)
            else:
                var = tk.StringVar(value=str(default))
            self.vars[key] = var
            ttk.Entry(sf, textvariable=var, width=42).grid(row=i, column=1, sticky=tk.EW, pady=2)
        sf.columnconfigure(1, weight=1)

        ttk.Button(tab_set, text="💾 Сохранить", command=self._save_settings).pack(pady=6)
        ttk.Button(tab_set, text="🔄 Hot-reload", command=self._reload).pack(pady=2)

        # --- Приложения ---
        tab_apps = ttk.Frame(nb)
        nb.add(tab_apps, text="Приложения")
        af = ttk.Frame(tab_apps)
        af.pack(fill=tk.BOTH, expand=True, padx=10, pady=8)
        self.apps_list = tk.Listbox(af, height=10)
        self.apps_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb = ttk.Scrollbar(af, command=self.apps_list.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.apps_list.config(yscrollcommand=sb.set)
        self._refresh_apps()

        ef = ttk.Frame(tab_apps)
        ef.pack(fill=tk.X, padx=10, pady=4)
        self.app_name = tk.StringVar()
        self.app_path = tk.StringVar()
        ttk.Label(ef, text="Имя:").pack(side=tk.LEFT)
        ttk.Entry(ef, textvariable=self.app_name, width=10).pack(side=tk.LEFT, padx=3)
        ttk.Label(ef, text="Путь:").pack(side=tk.LEFT)
        ttk.Entry(ef, textvariable=self.app_path, width=48).pack(side=tk.LEFT, padx=3, fill=tk.X, expand=True)
        ttk.Button(ef, text="+", command=self._add_app).pack(side=tk.LEFT, padx=2)
        ttk.Button(ef, text="−", command=self._del_app).pack(side=tk.LEFT)

        # --- Telegram ---
        tab_tg = ttk.Frame(nb)
        nb.add(tab_tg, text="Telegram")
        tg = ttk.Frame(tab_tg)
        tg.pack(fill=tk.BOTH, expand=True, padx=10, pady=8)

        self.tg_token = tk.StringVar(value=config.TELEGRAM_BOT_TOKEN)
        self.tg_chat = tk.StringVar(value=config.TELEGRAM_DEFAULT_CHAT_ID)
        self.tg_contact = tk.StringVar()
        self.tg_chatid_map = tk.StringVar()

        ttk.Label(tg, text="Bot Token (@BotFather):").grid(row=0, column=0, sticky=tk.W)
        ttk.Entry(tg, textvariable=self.tg_token, width=50, show="*").grid(row=0, column=1, sticky=tk.EW, pady=3)
        ttk.Label(tg, text="Default chat_id:").grid(row=1, column=0, sticky=tk.W)
        ttk.Entry(tg, textvariable=self.tg_chat, width=50).grid(row=1, column=1, sticky=tk.EW, pady=3)
        ttk.Label(tg, text="Контакт → chat_id (JSON):").grid(row=2, column=0, sticky=tk.NW)
        self.tg_chatid_map.set(json.dumps(config.TELEGRAM_CHATS, ensure_ascii=False, indent=2))
        ttk.Entry(tg, textvariable=self.tg_chatid_map, width=50).grid(row=2, column=1, sticky=tk.EW, pady=3)

        hint = (
            "Для read: напиши боту /start, узнай chat_id через getUpdates,\n"
            "добавь в telegram_chats: {\"маша\": \"123456789\"}"
        )
        ttk.Label(tg, text=hint, foreground="gray").grid(row=3, column=0, columnspan=2, sticky=tk.W, pady=6)

        tgf = ttk.Frame(tg)
        tgf.grid(row=4, column=0, columnspan=2, sticky=tk.W)
        ttk.Button(tgf, text="Сохранить TG", command=self._save_telegram).pack(side=tk.LEFT, padx=3)
        ttk.Button(tgf, text="Тест бота", command=self._test_telegram).pack(side=tk.LEFT, padx=3)
        ttk.Button(tgf, text="Тест read", command=self._test_tg_read).pack(side=tk.LEFT, padx=3)
        tg.columnconfigure(1, weight=1)

        # --- Логи ---
        tab_log = ttk.Frame(nb)
        nb.add(tab_log, text="Логи")
        self.log_box = scrolledtext.ScrolledText(tab_log, height=28, state=tk.DISABLED, font=("Consolas", 9))
        self.log_box.pack(fill=tk.BOTH, expand=True, padx=10, pady=8)
        ttk.Button(tab_log, text="Очистить", command=self._clear_log).pack(pady=4)

    def _info_text(self) -> str:
        return (
            f"Wake-word: {config.WAKE_WORD}\n"
            f"Ollama: {config.OLLAMA_MODEL}\n"
            f"Whisper: {config.WHISPER_MODEL} ({config.WHISPER_DEVICE}/{config.WHISPER_COMPUTE_TYPE})\n"
            f"Инструментов: {len(self.assistant.tools.list_tools())}"
        )

    def _append_log(self, msg: str) -> None:
        def _do():
            self.log_box.config(state=tk.NORMAL)
            self.log_box.insert(tk.END, msg + "\n")
            self.log_box.see(tk.END)
            self.log_box.config(state=tk.DISABLED)
        try:
            self.root.after(0, _do)
        except Exception:
            pass

    def _clear_log(self) -> None:
        self.log_box.config(state=tk.NORMAL)
        self.log_box.delete("1.0", tk.END)
        self.log_box.config(state=tk.DISABLED)

    def _refresh_apps(self) -> None:
        self.apps_list.delete(0, tk.END)
        for n, p in sorted(config.APP_PATHS.items()):
            self.apps_list.insert(tk.END, f"{n} → {p}")

    def _add_app(self) -> None:
        n, p = self.app_name.get().strip().lower(), self.app_path.get().strip()
        if not n or not p:
            messagebox.showwarning("", "Имя и путь обязательны")
            return
        config.APP_PATHS[n] = p
        config.save_settings()
        self._refresh_apps()

    def _del_app(self) -> None:
        sel = self.apps_list.curselection()
        if not sel:
            return
        name = self.apps_list.get(sel[0]).split(" → ")[0].strip()
        config.APP_PATHS.pop(name, None)
        config.save_settings()
        self._refresh_apps()

    def _save_settings(self) -> None:
        data = config.to_dict()
        for k, v in self.vars.items():
            data[k] = v.get()
        try:
            config.SETTINGS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            config.reload_settings()
            self.info_lbl.config(text=self._info_text())
            messagebox.showinfo("", "Сохранено")
        except Exception as exc:
            messagebox.showerror("", str(exc))

    def _save_telegram(self) -> None:
        data = config.to_dict()
        data["telegram_bot_token"] = self.tg_token.get().strip()
        data["telegram_default_chat_id"] = self.tg_chat.get().strip()
        try:
            chats = json.loads(self.tg_chatid_map.get() or "{}")
            data["telegram_chats"] = {str(k).lower(): str(v) for k, v in chats.items()}
        except json.JSONDecodeError as exc:
            messagebox.showerror("", f"Невалидный JSON telegram_chats: {exc}")
            return
        try:
            config.SETTINGS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            config.reload_settings()
            messagebox.showinfo("", "Telegram настройки сохранены")
        except Exception as exc:
            messagebox.showerror("", str(exc))

    def _reload(self) -> None:
        config.reload_settings()
        self.assistant.reload_settings()
        self.info_lbl.config(text=self._info_text())
        messagebox.showinfo("", "Hot-reload OK")

    def _start(self) -> None:
        if self.assistant.is_running:
            return
        self.btn_start.config(state=tk.DISABLED)
        self.btn_stop.config(state=tk.NORMAL)
        self.status_lbl.config(text="Статус: слушаю...")
        self.assistant.run_in_thread()
        self.root.after(500, self._poll)

    def _poll(self) -> None:
        if self.assistant.is_running:
            self.root.after(500, self._poll)
        else:
            self._on_stopped()

    def _stop(self) -> None:
        self.assistant.stop()
        self._on_stopped()

    def _on_stopped(self) -> None:
        self.btn_start.config(state=tk.NORMAL)
        self.btn_stop.config(state=tk.DISABLED)
        self.status_lbl.config(text="Статус: остановлен")

    def _test_ollama(self) -> None:
        ok = self.assistant.brain.check_connection()
        messagebox.showinfo("Ollama", "OK" if ok else "Недоступна")

    def _test_tts(self) -> None:
        threading.Thread(target=lambda: self.assistant.voice.speak("Тест Винди"), daemon=True).start()

    def _test_whisper(self) -> None:
        def _run():
            try:
                from voice import _get_whisper
                _get_whisper()
                self.root.after(0, lambda: messagebox.showinfo(
                    "Whisper",
                    f"OK: {config.WHISPER_DEVICE}/{config.WHISPER_COMPUTE_TYPE}",
                ))
            except Exception as exc:
                self.root.after(0, lambda: messagebox.showerror("Whisper", str(exc)))
        threading.Thread(target=_run, daemon=True).start()

    def _test_telegram(self) -> None:
        self._save_telegram()
        bot = _get_bot()
        if not bot:
            messagebox.showerror("", "Укажи bot token")
            return
        try:
            msg = bot.test_connection()
            messagebox.showinfo("", msg)
        except Exception as exc:
            messagebox.showerror("", str(exc))

    def _test_tg_read(self) -> None:
        self._save_telegram()
        result = self.assistant.tools.execute("telegram_read", {"limit": 3})
        messagebox.showinfo("telegram_read", result)

    def _manual_command(self) -> None:
        text = self.manual_entry.get().strip()
        if text:
            threading.Thread(target=lambda: self.assistant.process_command(text), daemon=True).start()

    def _on_close(self) -> None:
        remove_log_callback(self._log_cb)
        self.assistant.stop()
        self.root.destroy()


def run_gui() -> None:
    root = tk.Tk()
    WindyGUI(root)
    root.mainloop()


if __name__ == "__main__":
    run_gui()