"""
GUI Windy AI Assistant — tkinter.
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

setup_logging()


class WindyGUI:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Windy AI Assistant")
        self.root.geometry("900x700")
        self.root.minsize(760, 560)

        self.assistant = WindyAssistant()
        self.assistant.on_status(self._on_status)
        self._log_fn = self._log

        self._build()
        add_log_callback(self._log_fn)
        self.root.protocol("WM_DELETE_WINDOW", self._close)

    def _build(self) -> None:
        nb = ttk.Notebook(self.root)
        nb.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        # ---- Статус ----
        t0 = ttk.Frame(nb)
        nb.add(t0, text="Статус")

        self.lbl_status = ttk.Label(t0, text="Статус: остановлен", font=("Segoe UI", 12, "bold"))
        self.lbl_status.pack(anchor=tk.W, padx=12, pady=8)

        self.lbl_wake = ttk.Label(t0, text=self._wake_info(), foreground="#2060a0")
        self.lbl_wake.pack(anchor=tk.W, padx=12)

        bf = ttk.Frame(t0)
        bf.pack(anchor=tk.W, padx=12, pady=10)
        self.btn_start = ttk.Button(bf, text="▶ Старт", command=self._start)
        self.btn_start.pack(side=tk.LEFT, padx=3)
        self.btn_stop = ttk.Button(bf, text="■ Стоп", command=self._stop, state=tk.DISABLED)
        self.btn_stop.pack(side=tk.LEFT, padx=3)
        for txt, cmd in [
            ("Ollama", self._test_ollama),
            ("Whisper", self._test_whisper),
            ("TTS", self._test_tts),
        ]:
            ttk.Button(bf, text=txt, command=cmd).pack(side=tk.LEFT, padx=3)

        mf = ttk.LabelFrame(t0, text="Текстовая команда")
        mf.pack(fill=tk.X, padx=12, pady=8)
        self.entry_cmd = ttk.Entry(mf)
        self.entry_cmd.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6, pady=6)
        self.entry_cmd.bind("<Return>", lambda e: self._send_text())
        ttk.Button(mf, text="Отправить", command=self._send_text).pack(side=tk.LEFT, padx=6)

        self.lbl_info = ttk.Label(t0, text=self._sys_info(), justify=tk.LEFT)
        self.lbl_info.pack(anchor=tk.W, padx=12, pady=6)

        # ---- Настройки ----
        t1 = ttk.Frame(nb)
        nb.add(t1, text="Настройки")
        sf = ttk.Frame(t1)
        sf.pack(fill=tk.BOTH, expand=True, padx=12, pady=8)
        self.vars: dict[str, tk.Variable] = {}
        fields = [
            ("wake_word", "Wake-word", config.WAKE_WORD),
            ("ollama_model", "Ollama", config.OLLAMA_MODEL),
            ("whisper_model", "Whisper model", config.WHISPER_MODEL),
            ("whisper_device", "Whisper device (auto/cpu/cuda)", config.WHISPER_DEVICE),
            ("whisper_compute_type", "Whisper compute (int8)", config.WHISPER_COMPUTE_TYPE),
            ("vad_silence_sec", "VAD тишина (сек)", config.VAD_SILENCE_SEC),
            ("vad_hangover_sec", "VAD hangover", config.VAD_HANGOVER_SEC),
            ("vad_pre_roll_sec", "Pre-roll (сек)", config.VAD_PRE_ROLL_SEC),
            ("post_tts_delay_sec", "Пауза после TTS", config.POST_TTS_DELAY_SEC),
            ("ollama_num_ctx", "num_ctx", config.OLLAMA_NUM_CTX),
            ("ollama_num_gpu", "num_gpu (-1=авто)", config.OLLAMA_NUM_GPU),
        ]
        for i, (k, lbl, val) in enumerate(fields):
            ttk.Label(sf, text=lbl).grid(row=i, column=0, sticky=tk.W, pady=2)
            v: tk.Variable
            v = tk.DoubleVar(value=val) if isinstance(val, float) else (
                tk.IntVar(value=val) if isinstance(val, int) else tk.StringVar(value=str(val))
            )
            self.vars[k] = v
            ttk.Entry(sf, textvariable=v, width=44).grid(row=i, column=1, sticky=tk.EW, pady=2)
        sf.columnconfigure(1, weight=1)
        ttk.Button(t1, text="💾 Сохранить", command=self._save).pack(pady=6)
        ttk.Button(t1, text="🔄 Hot-reload", command=self._reload).pack(pady=2)

        # ---- Приложения ----
        t2 = ttk.Frame(nb)
        nb.add(t2, text="Приложения")
        af = ttk.Frame(t2)
        af.pack(fill=tk.BOTH, expand=True, padx=12, pady=8)
        self.lst_apps = tk.Listbox(af, height=12)
        self.lst_apps.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb = ttk.Scrollbar(af, command=self.lst_apps.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.lst_apps.config(yscrollcommand=sb.set)
        self._refresh_apps()
        ef = ttk.Frame(t2)
        ef.pack(fill=tk.X, padx=12, pady=4)
        self.v_app_name = tk.StringVar()
        self.v_app_path = tk.StringVar()
        ttk.Entry(ef, textvariable=self.v_app_name, width=12).pack(side=tk.LEFT, padx=2)
        ttk.Entry(ef, textvariable=self.v_app_path, width=50).pack(side=tk.LEFT, padx=2, fill=tk.X, expand=True)
        ttk.Button(ef, text="+", command=self._app_add).pack(side=tk.LEFT, padx=2)
        ttk.Button(ef, text="−", command=self._app_del).pack(side=tk.LEFT)

        # ---- Telegram ----
        t3 = ttk.Frame(nb)
        nb.add(t3, text="Telegram")
        tg = ttk.Frame(t3)
        tg.pack(fill=tk.BOTH, expand=True, padx=12, pady=8)
        self.v_api_id = tk.StringVar(value=str(config.TELEGRAM_API_ID or ""))
        self.v_api_hash = tk.StringVar(value=config.TELEGRAM_API_HASH)
        self.v_bot_token = tk.StringVar(value=config.TELEGRAM_BOT_TOKEN)
        rows = [
            ("API ID (my.telegram.org):", self.v_api_id, False),
            ("API Hash:", self.v_api_hash, False),
            ("Bot Token (опц.):", self.v_bot_token, True),
        ]
        for i, (lbl, var, secret) in enumerate(rows):
            ttk.Label(tg, text=lbl).grid(row=i, column=0, sticky=tk.W, pady=3)
            ttk.Entry(tg, textvariable=var, width=48, show="*" if secret else "").grid(row=i, column=1, sticky=tk.EW)
        ttk.Label(
            tg,
            text="Telethon: python telegram_client.py  (первый вход — код из Telegram)",
            foreground="gray",
        ).grid(row=3, column=0, columnspan=2, sticky=tk.W, pady=6)
        tbf = ttk.Frame(tg)
        tbf.grid(row=4, column=0, columnspan=2, sticky=tk.W)
        ttk.Button(tbf, text="Сохранить TG", command=self._save_tg).pack(side=tk.LEFT, padx=3)
        ttk.Button(tbf, text="Тест read_last", command=self._test_tg_read).pack(side=tk.LEFT, padx=3)
        tg.columnconfigure(1, weight=1)

        # ---- Логи ----
        t4 = ttk.Frame(nb)
        nb.add(t4, text="Логи")
        self.txt_log = scrolledtext.ScrolledText(t4, height=30, state=tk.DISABLED, font=("Consolas", 9))
        self.txt_log.pack(fill=tk.BOTH, expand=True, padx=12, pady=8)
        ttk.Button(t4, text="Очистить", command=self._clear_log).pack(pady=4)

    def _wake_info(self) -> str:
        return "Wake-word: «Эй Винди» | «Hey Винди» | «Винди»"

    def _sys_info(self) -> str:
        return (
            f"Whisper: {config.WHISPER_MODEL} ({config.WHISPER_DEVICE}/{config.WHISPER_COMPUTE_TYPE})\n"
            f"Ollama: {config.OLLAMA_MODEL}\n"
            f"Tools: {len(self.assistant.tools.list_tools())}"
        )

    def _on_status(self, s: str) -> None:
        try:
            self.root.after(0, lambda: self.lbl_status.config(text=f"Статус: {s}"))
        except Exception:
            pass

    def _log(self, msg: str) -> None:
        def _do():
            self.txt_log.config(state=tk.NORMAL)
            self.txt_log.insert(tk.END, msg + "\n")
            self.txt_log.see(tk.END)
            self.txt_log.config(state=tk.DISABLED)
        try:
            self.root.after(0, _do)
        except Exception:
            pass

    def _clear_log(self) -> None:
        self.txt_log.config(state=tk.NORMAL)
        self.txt_log.delete("1.0", tk.END)
        self.txt_log.config(state=tk.DISABLED)

    def _refresh_apps(self) -> None:
        self.lst_apps.delete(0, tk.END)
        for n, p in sorted(config.APP_PATHS.items()):
            self.lst_apps.insert(tk.END, f"{n} → {p}")

    def _save(self) -> None:
        data = config.to_dict()
        for k, v in self.vars.items():
            data[k] = v.get()
        config.SETTINGS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        config.reload_settings()
        self.lbl_info.config(text=self._sys_info())
        messagebox.showinfo("", "Сохранено")

    def _save_tg(self) -> None:
        data = config.to_dict()
        try:
            data["telegram_api_id"] = int(self.v_api_id.get() or 0)
        except ValueError:
            messagebox.showerror("", "API ID — число")
            return
        data["telegram_api_hash"] = self.v_api_hash.get().strip()
        data["telegram_bot_token"] = self.v_bot_token.get().strip()
        config.SETTINGS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        config.reload_settings()
        messagebox.showinfo("", "Telegram сохранён")

    def _reload(self) -> None:
        self.assistant.reload_settings()
        self.lbl_info.config(text=self._sys_info())
        messagebox.showinfo("", "Hot-reload OK")

    def _app_add(self) -> None:
        n, p = self.v_app_name.get().strip().lower(), self.v_app_path.get().strip()
        if n and p:
            config.APP_PATHS[n] = p
            config.save_settings()
            self._refresh_apps()

    def _app_del(self) -> None:
        sel = self.lst_apps.curselection()
        if sel:
            name = self.lst_apps.get(sel[0]).split(" → ")[0]
            config.APP_PATHS.pop(name, None)
            config.save_settings()
            self._refresh_apps()

    def _start(self) -> None:
        if self.assistant.is_running:
            return
        self.btn_start.config(state=tk.DISABLED)
        self.btn_stop.config(state=tk.NORMAL)
        self.assistant.run_in_thread()
        self.root.after(600, self._poll)

    def _poll(self) -> None:
        if self.assistant.is_running:
            self.root.after(600, self._poll)
        else:
            self.btn_start.config(state=tk.NORMAL)
            self.btn_stop.config(state=tk.DISABLED)

    def _stop(self) -> None:
        self.assistant.stop()

    def _test_ollama(self) -> None:
        messagebox.showinfo("Ollama", "OK" if self.assistant.brain.check_connection() else "Недоступна")

    def _test_whisper(self) -> None:
        def _w():
            try:
                from voice import _get_whisper
                _get_whisper()
                self.root.after(0, lambda: messagebox.showinfo("Whisper", "OK"))
            except Exception as e:
                self.root.after(0, lambda: messagebox.showerror("Whisper", str(e)))
        threading.Thread(target=_w, daemon=True).start()

    def _test_tts(self) -> None:
        threading.Thread(target=lambda: self.assistant.voice.speak("Тест Винди"), daemon=True).start()

    def _test_tg_read(self) -> None:
        r = self.assistant.tools.execute("telegram_read_last", {"limit": 3})
        messagebox.showinfo("read_last", r)

    def _send_text(self) -> None:
        t = self.entry_cmd.get().strip()
        if t:
            threading.Thread(target=lambda: self.assistant.process_command(t), daemon=True).start()

    def _close(self) -> None:
        remove_log_callback(self._log_fn)
        self.assistant.stop()
        self.root.destroy()


def run_gui() -> None:
    root = tk.Tk()
    WindyGUI(root)
    root.mainloop()


if __name__ == "__main__":
    run_gui()