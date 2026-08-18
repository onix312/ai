#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Окно управления PrintFlow — то, что открывается двойным кликом.

Вместо чёрной консоли: индикатор состояния, одна большая кнопка, адреса для
телефона с QR-кодом, живой журнал и кнопки обслуживания. Только tkinter из
стандартной библиотеки — ничего ставить не нужно.

Модуль запускается через лаунчер:  python pf.py gui
"""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import messagebox, ttk

import pf

# Палитра совпадает с интерфейсом панели (site/assets/theme.css)
BG = "#0f1117"
CARD = "#171a23"
LINE = "#262b38"
TEXT = "#e7e9ee"
MUTED = "#8b93a7"
ACCENT = "#4f46e5"
ACCENT_HOVER = "#6366f1"
GREEN = "#22c55e"
RED = "#ef4444"
AMBER = "#f59e0b"


class LauncherWindow:
    POLL_MS = 1500

    def __init__(self, args) -> None:
        self.args = args
        self.port = int(getattr(args, "port", pf.DEFAULT_PORT) or pf.DEFAULT_PORT)
        self.process: subprocess.Popen | None = None
        self.lines: queue.Queue[str] = queue.Queue()
        self.state = "stopped"
        self.qr_url = ""

        self.root = tk.Tk()
        self.root.title(f"NOZZA · PrintFlow {pf.app_version()}")
        self.root.configure(bg=BG)
        self.root.minsize(720, 620)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._init_style()
        self._build()
        self.refresh_addresses()
        self.root.after(200, self.poll)

    # ------------------------------------------------------------- интерфейс
    def _init_style(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=CARD)
        style.configure("TLabel", background=BG, foreground=TEXT, font=("Segoe UI", 10))
        style.configure("Card.TLabel", background=CARD, foreground=TEXT)
        style.configure("Muted.TLabel", background=CARD, foreground=MUTED,
                        font=("Segoe UI", 9))
        style.configure("Title.TLabel", background=BG, foreground=TEXT,
                        font=("Segoe UI", 16, "bold"))
        style.configure("Status.TLabel", background=CARD, foreground=TEXT,
                        font=("Segoe UI", 12, "bold"))
        style.configure("Link.TLabel", background=CARD, foreground="#8ab4ff",
                        font=("Consolas", 11))
        style.configure("TCheckbutton", background=CARD, foreground=MUTED)
        style.map("TCheckbutton", background=[("active", CARD)])
        style.configure("Tool.TButton", padding=(10, 6), relief="flat",
                        background=LINE, foreground=TEXT, borderwidth=0)
        style.map("Tool.TButton", background=[("active", "#2f3646")])

    def _build(self) -> None:
        outer = ttk.Frame(self.root, padding=16)
        outer.pack(fill="both", expand=True)

        head = ttk.Frame(outer)
        head.pack(fill="x")
        ttk.Label(head, text="NOZZA · PrintFlow", style="Title.TLabel").pack(side="left")
        ttk.Label(head, text=f"версия {pf.app_version()}",
                  foreground=MUTED, background=BG).pack(side="left", padx=(10, 0), pady=(6, 0))

        # --- карточка состояния
        card = ttk.Frame(outer, style="Card.TFrame", padding=14)
        card.pack(fill="x", pady=(12, 0))

        row = ttk.Frame(card, style="Card.TFrame")
        row.pack(fill="x")
        self.dot = tk.Canvas(row, width=16, height=16, bg=CARD, highlightthickness=0)
        self.dot.pack(side="left", pady=2)
        self.dot_id = self.dot.create_oval(3, 3, 13, 13, fill=RED, outline="")
        self.status_label = ttk.Label(row, text="Остановлено", style="Status.TLabel")
        self.status_label.pack(side="left", padx=(8, 0))

        self.power = tk.Button(row, text="▶  Запустить", command=self.toggle,
                               bg=ACCENT, fg="white", activebackground=ACCENT_HOVER,
                               activeforeground="white", relief="flat", bd=0,
                               font=("Segoe UI", 11, "bold"), padx=22, pady=9,
                               cursor="hand2")
        self.power.pack(side="right")

        options = ttk.Frame(card, style="Card.TFrame")
        options.pack(fill="x", pady=(12, 0))
        ttk.Label(options, text="Порт", style="Muted.TLabel").pack(side="left")
        self.port_var = tk.StringVar(value=str(self.port))
        port_entry = tk.Entry(options, textvariable=self.port_var, width=6, bd=0,
                              bg=LINE, fg=TEXT, insertbackground=TEXT, justify="center")
        port_entry.pack(side="left", padx=(6, 16))
        self.lan_var = tk.BooleanVar(value=not getattr(self.args, "local", False))
        ttk.Checkbutton(options, text="Доступ с телефона и других устройств в сети",
                        variable=self.lan_var,
                        command=self.refresh_addresses).pack(side="left")

        # --- адреса и QR
        body = ttk.Frame(outer, style="Card.TFrame", padding=14)
        body.pack(fill="x", pady=(10, 0))
        left = ttk.Frame(body, style="Card.TFrame")
        left.pack(side="left", fill="both", expand=True)
        ttk.Label(left, text="АДРЕСА ПАНЕЛИ", style="Muted.TLabel").pack(anchor="w")
        self.links = ttk.Frame(left, style="Card.TFrame")
        self.links.pack(anchor="w", pady=(6, 0))
        self.hint = ttk.Label(left, text="", style="Muted.TLabel", wraplength=360,
                              justify="left")
        self.hint.pack(anchor="w", pady=(10, 0))

        self.qr = tk.Canvas(body, width=190, height=190, bg=CARD, highlightthickness=0)
        self.qr.pack(side="right", padx=(12, 0))

        # --- журнал
        log_card = ttk.Frame(outer, style="Card.TFrame", padding=(14, 10))
        log_card.pack(fill="both", expand=True, pady=(10, 0))
        ttk.Label(log_card, text="ЖУРНАЛ", style="Muted.TLabel").pack(anchor="w")
        wrap = ttk.Frame(log_card, style="Card.TFrame")
        wrap.pack(fill="both", expand=True, pady=(6, 0))
        self.log = tk.Text(wrap, height=10, bg="#0b0d13", fg="#c9cedb", bd=0,
                           insertbackground=TEXT, font=("Consolas", 9), wrap="word",
                           padx=10, pady=8, state="disabled")
        scroll = ttk.Scrollbar(wrap, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        # --- кнопки обслуживания
        tools = ttk.Frame(outer)
        tools.pack(fill="x", pady=(10, 0))
        buttons = [
            ("Открыть панель", self.open_panel),
            ("Папка данных", self.open_data_dir),
            ("Диагностика", lambda: self.run_task("doctor")),
            ("Копия базы", lambda: self.run_task("backup")),
            ("Обновление", lambda: self.run_task("update")),
            ("Ярлык и автозапуск", lambda: self.run_task("install")),
        ]
        for label, action in buttons:
            ttk.Button(tools, text=label, style="Tool.TButton",
                       command=action).pack(side="left", padx=(0, 8))

        self.write(f"Папка данных: {pf.DATA_DIR}")
        self.write("Нажмите «Запустить» — панель откроется в браузере.")

    # --------------------------------------------------------------- вывод
    def write(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text.rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def set_state(self, state: str, text: str) -> None:
        self.state = state
        colors = {"running": GREEN, "starting": AMBER, "stopped": RED}
        self.dot.itemconfigure(self.dot_id, fill=colors.get(state, RED))
        self.status_label.configure(text=text)
        self.power.configure(text="■  Остановить" if state == "running" else "▶  Запустить")

    # ------------------------------------------------------------- адреса
    def current_port(self) -> int:
        raw = self.port_var.get().strip()
        return int(raw) if raw.isdigit() and 0 < int(raw) < 65536 else pf.DEFAULT_PORT

    def refresh_addresses(self) -> None:
        for child in self.links.winfo_children():
            child.destroy()
        port = self.current_port()
        urls = [f"http://localhost:{port}/"]
        if self.lan_var.get():
            urls += [f"http://{ip}:{port}/" for ip in pf.local_ips()]
        for url in urls:
            link = ttk.Label(self.links, text=url, style="Link.TLabel", cursor="hand2")
            link.pack(anchor="w")
            link.bind("<Button-1>", lambda _event, target=url: webbrowser.open(target))
        if self.lan_var.get() and len(urls) == 1:
            self.hint.configure(text="Сетевой адрес не определился: проверьте Wi-Fi. "
                                     "Панель на этом компьютере всё равно работает.")
        elif self.lan_var.get():
            self.hint.configure(text="Наведите камеру телефона на код справа. "
                                     "Телефон должен быть в той же Wi-Fi сети.")
        else:
            self.hint.configure(text="Режим «только этот компьютер»: "
                                     "с телефона зайти нельзя.")
        self.draw_qr(urls[-1] if len(urls) > 1 else urls[0])

    def draw_qr(self, url: str) -> None:
        if url == self.qr_url:
            return
        self.qr_url = url
        self.qr.delete("all")
        try:
            sys.path.insert(0, str(pf.CONNECTOR))
            from printflow import qrgen

            matrix = qrgen.matrix(url)
        except Exception:
            self.qr.create_text(95, 95, text="QR недоступен", fill=MUTED)
            return
        border = 2
        size = len(matrix) + border * 2
        scale = max(2, int(186 / size))
        offset = (190 - size * scale) // 2
        self.qr.create_rectangle(offset, offset, offset + size * scale,
                                 offset + size * scale, fill="white", outline="white")
        for y, row in enumerate(matrix):
            for x, dark in enumerate(row):
                if dark:
                    left = offset + (x + border) * scale
                    top = offset + (y + border) * scale
                    self.qr.create_rectangle(left, top, left + scale, top + scale,
                                             fill="black", outline="black")

    # ------------------------------------------------------------ процессы
    def toggle(self) -> None:
        if self.state == "running":
            self.stop_server()
        else:
            self.start_server()

    def start_server(self) -> None:
        port = self.current_port()
        if pf.port_busy(port) and not pf.health(port):
            messagebox.showerror("Порт занят",
                                 f"Порт {port} занят другой программой.\n"
                                 f"Попробуйте {pf.free_port(port + 1)}.")
            return
        if pf.health(port):
            self.write(f"PrintFlow уже работает на порту {port}")
            self.set_state("running", f"Работает · порт {port}")
            return

        self.set_state("starting", "Запускается…")
        self.write("Готовлю окружение…")

        def worker() -> None:
            try:
                python = pf.interpreter(quiet=True)
            except SystemExit:
                self.lines.put("Не удалось создать окружение Python")
                return
            host = "0.0.0.0" if self.lan_var.get() else "127.0.0.1"
            command = [str(python), str(pf.ENTRYPOINT), "--host", host,
                       "--port", str(port), "--no-browser", "--no-banner"]
            self.process = subprocess.Popen(
                command, cwd=str(pf.ROOT), stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                text=True, encoding="utf-8", errors="replace", bufsize=1)
            pf.write_pid(self.process.pid)
            for line in self.process.stdout:  # type: ignore[union-attr]
                self.lines.put(line.rstrip())
            self.lines.put("Сервер остановлен")

        threading.Thread(target=worker, daemon=True).start()

    def stop_server(self) -> None:
        self.write("Останавливаю…")
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.process.kill()
        else:
            pf.cmd_stop(self.args)
        pf.clear_pid()
        self.process = None
        self.set_state("stopped", "Остановлено")

    def run_task(self, command: str) -> None:
        """Служебная команда лаунчера с выводом в журнал окна."""
        self.write(f"── {command} ──")

        def worker() -> None:
            process = subprocess.Popen(
                [sys.executable, str(pf.ROOT / "pf.py"), command],
                cwd=str(pf.ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
                errors="replace", bufsize=1)
            for line in process.stdout:  # type: ignore[union-attr]
                self.lines.put(line.rstrip())

        threading.Thread(target=worker, daemon=True).start()

    # --------------------------------------------------------------- прочее
    def open_panel(self) -> None:
        webbrowser.open(f"http://localhost:{self.current_port()}/")

    def open_data_dir(self) -> None:
        path = pf.DATA_DIR
        path.mkdir(parents=True, exist_ok=True)
        if pf.IS_WINDOWS:
            os.startfile(path)  # type: ignore[attr-defined]
        elif pf.IS_MACOS:
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])

    def poll(self) -> None:
        while True:
            try:
                self.write(self.lines.get_nowait())
            except queue.Empty:
                break
        port = self.current_port()
        info = pf.health(port, timeout=0.4)
        if info:
            uptime = int(info.get("uptime") or 0)
            self.set_state("running", f"Работает · порт {port} · "
                                      f"{uptime // 3600} ч {uptime % 3600 // 60} мин")
        elif self.state == "running":
            self.set_state("stopped", "Остановлено")
        self.root.after(self.POLL_MS, self.poll)

    def on_close(self) -> None:
        if self.process and self.process.poll() is None:
            keep = messagebox.askyesnocancel(
                "PrintFlow работает",
                "Оставить сервер работать в фоне?\n\n"
                "Да — панель останется доступной с телефона.\n"
                "Нет — остановить сервер и закрыть.")
            if keep is None:
                return
            if not keep:
                self.stop_server()
        self.root.destroy()

    def run(self) -> int:
        if getattr(self.args, "autostart_server", False):
            self.root.after(400, self.start_server)
        self.root.mainloop()
        return 0


def run_window(args) -> int:
    return LauncherWindow(args).run()


if __name__ == "__main__":
    raise SystemExit(pf.main(["gui"]))
