#!/usr/bin/env python3
"""Local RTSP / YOLO / Docker compose control panel (tkinter).

Buttons:
  1. Start MediaMTX
  2. Start RTSP publish
  3. Stop RTSP publish
  4. Start YOLO service
  5. Rebuild app Docker stack (compose up --build -d)
  6. Stop all (stream + YOLO + MediaMTX)

Logs are split into separate tabs per service.

Run:
  python backend/tests/dev_stack_ui.py
"""

from __future__ import annotations

import os
import queue
import shutil
import socket
import subprocess
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk


TESTS_DIR = Path(__file__).resolve().parent
BACKEND_ROOT = TESTS_DIR.parent
REPO_ROOT = BACKEND_ROOT.parent

MEDIAMTX_NAME = "inspection-rtsp-mediamtx"
RTSP_PORT = 18554
YOLO_HOST = "127.0.0.1"
YOLO_PORT = 8001
GENERATE_SCRIPT = TESTS_DIR / "generate_rtsp_stream.py"
YOLO_SCRIPT = BACKEND_ROOT / "scripts" / "run_yolo_service.ps1"
ENV_EXAMPLE = REPO_ROOT / ".env.example"
ENV_FILE = REPO_ROOT / ".env"

LOG_CHANNELS = (
    ("system", "系统"),
    ("mediamtx", "MediaMTX"),
    ("stream", "推流"),
    ("yolo", "YOLO"),
    ("compose", "Docker"),
)
LABEL_TO_CHANNEL = {
    "mediamtx": "mediamtx",
    "stream": "stream",
    "yolo": "yolo",
    "compose": "compose",
}
ACTION_TO_CHANNEL = {
    "mediamtx": "mediamtx",
    "stream_on": "stream",
    "stream_off": "stream",
    "yolo": "yolo",
    "rebuild": "compose",
    "stop_all": "system",
}

# Dev-tool palette: slate + teal (avoid purple / cream tropes).
THEME = {
    "bg": "#0f1419",
    "surface": "#1a2332",
    "surface_alt": "#243044",
    "border": "#2e3d52",
    "text": "#e8eef6",
    "muted": "#8b9bb0",
    "accent": "#2dd4bf",
    "accent_dim": "#0d9488",
    "accent_hover": "#5eead4",
    "ok": "#34d399",
    "warn": "#fbbf24",
    "off": "#64748b",
    "danger": "#f87171",
    "danger_bg": "#7f1d1d",
    "log_bg": "#0b1220",
    "log_fg": "#c8d5e4",
    "btn_fg": "#0f1419",
}


def tcp_open(host: str, port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def resolve_python() -> str:
    venv = BACKEND_ROOT / ".venv" / "Scripts" / "python.exe"
    if venv.is_file():
        return str(venv)
    return shutil.which("python") or shutil.which("py") or "python"


def resolve_powershell() -> str:
    return shutil.which("powershell") or "powershell.exe"


class DevStackUI:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Inspection Dev Stack")
        self.root.geometry("980x700")
        self.root.minsize(820, 600)
        self.root.configure(bg=THEME["bg"])

        self.log_queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self.log_widgets: dict[str, scrolledtext.ScrolledText] = {}
        self.status_dot_canvases: dict[str, tk.Canvas] = {}
        self.stream_proc: subprocess.Popen[str] | None = None
        self.yolo_proc: subprocess.Popen[str] | None = None
        self._action_locks: dict[str, threading.Lock] = {
            "mediamtx": threading.Lock(),
            "stream_on": threading.Lock(),
            "stream_off": threading.Lock(),
            "yolo": threading.Lock(),
            "rebuild": threading.Lock(),
            "stop_all": threading.Lock(),
        }

        self._apply_theme()
        self._build_ui()
        self._poll_log_queue()
        self._refresh_status()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _apply_theme(self) -> None:
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")

        bg = THEME["bg"]
        surface = THEME["surface"]
        surface_alt = THEME["surface_alt"]
        border = THEME["border"]
        text = THEME["text"]
        muted = THEME["muted"]
        accent = THEME["accent"]
        accent_dim = THEME["accent_dim"]
        danger = THEME["danger"]
        danger_bg = THEME["danger_bg"]

        style.configure(".", background=bg, foreground=text, font=("Segoe UI", 10))
        style.configure("TFrame", background=bg)
        style.configure("Card.TFrame", background=surface)
        style.configure("Inner.TFrame", background=surface)
        style.configure("TLabel", background=bg, foreground=text)
        style.configure("Card.TLabel", background=surface, foreground=text)
        style.configure("Muted.TLabel", background=bg, foreground=muted, font=("Segoe UI", 9))
        style.configure("CardMuted.TLabel", background=surface, foreground=muted, font=("Segoe UI", 9))
        style.configure("Title.TLabel", background=bg, foreground=text, font=("Segoe UI", 18, "bold"))
        style.configure("Subtitle.TLabel", background=bg, foreground=muted, font=("Segoe UI", 9))
        style.configure("Section.TLabel", background=bg, foreground=muted, font=("Segoe UI", 9, "bold"))
        style.configure("CardSection.TLabel", background=surface, foreground=muted, font=("Segoe UI", 9, "bold"))
        style.configure("StatusName.TLabel", background=surface, foreground=text, font=("Segoe UI", 11, "bold"))
        style.configure("StatusDetail.TLabel", background=surface, foreground=muted, font=("Segoe UI", 9))
        style.configure("UrlLabel.TLabel", background=surface_alt, foreground=muted, font=("Segoe UI", 9))
        style.configure("UrlValue.TLabel", background=surface_alt, foreground=accent, font=("Consolas", 9))

        style.configure(
            "Accent.TButton",
            background=accent_dim,
            foreground=THEME["btn_fg"],
            bordercolor=accent_dim,
            lightcolor=accent_dim,
            darkcolor=accent_dim,
            focuscolor=accent,
            padding=(14, 10),
            font=("Segoe UI", 10, "bold"),
        )
        style.map(
            "Accent.TButton",
            background=[("active", accent), ("disabled", border)],
            foreground=[("disabled", muted)],
        )

        style.configure(
            "Secondary.TButton",
            background=surface_alt,
            foreground=text,
            bordercolor=border,
            lightcolor=surface_alt,
            darkcolor=surface_alt,
            focuscolor=border,
            padding=(14, 10),
            font=("Segoe UI", 10),
        )
        style.map(
            "Secondary.TButton",
            background=[("active", border), ("disabled", surface)],
            foreground=[("disabled", muted)],
        )

        style.configure(
            "Danger.TButton",
            background=danger_bg,
            foreground=danger,
            bordercolor=danger_bg,
            lightcolor=danger_bg,
            darkcolor=danger_bg,
            focuscolor=danger,
            padding=(14, 10),
            font=("Segoe UI", 10, "bold"),
        )
        style.map(
            "Danger.TButton",
            background=[("active", "#991b1b"), ("disabled", surface)],
            foreground=[("disabled", muted)],
        )

        style.configure(
            "Ghost.TButton",
            background=surface,
            foreground=muted,
            bordercolor=border,
            lightcolor=surface,
            darkcolor=surface,
            focuscolor=border,
            padding=(10, 6),
            font=("Segoe UI", 9),
        )
        style.map("Ghost.TButton", background=[("active", surface_alt)], foreground=[("active", text)])

        style.configure("TNotebook", background=bg, borderwidth=0)
        style.configure(
            "TNotebook.Tab",
            background=surface,
            foreground=muted,
            padding=(14, 8),
            font=("Segoe UI", 9),
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", surface_alt)],
            foreground=[("selected", accent)],
        )
        style.configure("TScrollbar", background=surface_alt, troughcolor=bg, bordercolor=border, arrowcolor=muted)

    def _card(self, parent: tk.Misc) -> tuple[tk.Frame, ttk.Frame]:
        outer = tk.Frame(parent, bg=THEME["border"], bd=0, highlightthickness=0)
        inner = ttk.Frame(outer, style="Card.TFrame")
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        return outer, inner

    def _build_ui(self) -> None:
        shell = ttk.Frame(self.root)
        shell.pack(fill="both", expand=True, padx=18, pady=16)

        # —— Header ——
        header = ttk.Frame(shell)
        header.pack(fill="x", pady=(0, 14))
        left = ttk.Frame(header)
        left.pack(side="left", fill="x", expand=True)
        ttk.Label(left, text="Inspection Dev Stack", style="Title.TLabel").pack(anchor="w")
        ttk.Label(left, text="本地 RTSP / YOLO / Docker 测试控制台", style="Subtitle.TLabel").pack(
            anchor="w", pady=(2, 0)
        )
        path_text = str(REPO_ROOT)
        if len(path_text) > 56:
            path_text = "…" + path_text[-54:]
        ttk.Label(header, text=path_text, style="Muted.TLabel").pack(side="right", anchor="ne")

        # —— Status row ——
        ttk.Label(shell, text="服务状态", style="Section.TLabel").pack(anchor="w", pady=(0, 6))
        status_row = ttk.Frame(shell)
        status_row.pack(fill="x", pady=(0, 14))
        for col in range(3):
            status_row.columnconfigure(col, weight=1, uniform="status")

        self.status_vars = {
            "mediamtx": tk.StringVar(value="—"),
            "stream": tk.StringVar(value="—"),
            "yolo": tk.StringVar(value="—"),
        }
        status_meta = (
            ("mediamtx", "MediaMTX", f"port {RTSP_PORT}"),
            ("stream", "推流", "video + time"),
            ("yolo", "YOLO", f"port {YOLO_PORT}"),
        )
        for col, (key, name, hint) in enumerate(status_meta):
            outer, card = self._card(status_row)
            outer.grid(row=0, column=col, sticky="nsew", padx=(0 if col == 0 else 8, 0))
            body = ttk.Frame(card, style="Inner.TFrame")
            body.pack(fill="both", expand=True, padx=14, pady=12)

            top = ttk.Frame(body, style="Inner.TFrame")
            top.pack(fill="x")
            dot = tk.Canvas(
                top,
                width=12,
                height=12,
                bg=THEME["surface"],
                highlightthickness=0,
                bd=0,
            )
            dot.pack(side="left", padx=(0, 8), pady=2)
            self.status_dot_canvases[key] = dot
            self._set_status_dot(key, THEME["off"])
            ttk.Label(top, text=name, style="StatusName.TLabel").pack(side="left")
            ttk.Label(top, text=hint, style="CardMuted.TLabel").pack(side="right")
            ttk.Label(body, textvariable=self.status_vars[key], style="StatusDetail.TLabel").pack(
                anchor="w", pady=(8, 0)
            )

        # —— Actions ——
        ttk.Label(shell, text="操作", style="Section.TLabel").pack(anchor="w", pady=(0, 6))
        actions_outer, actions_card = self._card(shell)
        actions_outer.pack(fill="x", pady=(0, 14))
        grid = ttk.Frame(actions_card, style="Inner.TFrame")
        grid.pack(fill="x", padx=12, pady=12)
        for col in range(3):
            grid.columnconfigure(col, weight=1, uniform="actions")

        self.btn_mediamtx = ttk.Button(
            grid, text="挂起 MediaMTX", style="Accent.TButton", command=self.start_mediamtx
        )
        self.btn_stream_on = ttk.Button(
            grid, text="打开推流", style="Accent.TButton", command=self.start_stream
        )
        self.btn_stream_off = ttk.Button(
            grid, text="关闭推流", style="Secondary.TButton", command=self.stop_stream
        )
        self.btn_yolo = ttk.Button(
            grid, text="打开 YOLO", style="Accent.TButton", command=self.start_yolo
        )
        self.btn_rebuild = ttk.Button(
            grid, text="重建 Docker", style="Secondary.TButton", command=self.rebuild_docker
        )
        self.btn_stop_all = ttk.Button(
            grid, text="全部关闭", style="Danger.TButton", command=self.stop_all
        )

        self.btn_mediamtx.grid(row=0, column=0, sticky="ew", padx=4, pady=4)
        self.btn_stream_on.grid(row=0, column=1, sticky="ew", padx=4, pady=4)
        self.btn_stream_off.grid(row=0, column=2, sticky="ew", padx=4, pady=4)
        self.btn_yolo.grid(row=1, column=0, sticky="ew", padx=4, pady=4)
        self.btn_rebuild.grid(row=1, column=1, sticky="ew", padx=4, pady=4)
        self.btn_stop_all.grid(row=1, column=2, sticky="ew", padx=4, pady=4)

        # —— Endpoints ——
        ttk.Label(shell, text="地址", style="Section.TLabel").pack(anchor="w", pady=(0, 6))
        urls_outer, urls_card = self._card(shell)
        urls_outer.pack(fill="x", pady=(0, 14))
        urls_body = ttk.Frame(urls_card, style="Inner.TFrame")
        urls_body.pack(fill="x", padx=10, pady=10)
        endpoints = (
            ("视频", f"rtsp://127.0.0.1:{RTSP_PORT}/live"),
            ("时间", f"rtsp://127.0.0.1:{RTSP_PORT}/time"),
            ("YOLO", f"http://{YOLO_HOST}:{YOLO_PORT}/healthz"),
        )
        for row, (label, value) in enumerate(endpoints):
            chip = tk.Frame(urls_body, bg=THEME["surface_alt"], bd=0, highlightthickness=0)
            chip.pack(fill="x", pady=3)
            ttk.Label(chip, text=label, style="UrlLabel.TLabel", width=6).pack(
                side="left", padx=(10, 4), pady=7
            )
            ttk.Label(chip, text=value, style="UrlValue.TLabel").pack(side="left", padx=(0, 10), pady=7)

        # —— Logs ——
        log_header = ttk.Frame(shell)
        log_header.pack(fill="x", pady=(0, 6))
        ttk.Label(log_header, text="日志", style="Section.TLabel").pack(side="left")
        ttk.Button(log_header, text="清空当前栏", style="Ghost.TButton", command=self._clear_active_log).pack(
            side="right"
        )
        ttk.Button(log_header, text="清空全部", style="Ghost.TButton", command=self._clear_all_logs).pack(
            side="right", padx=(0, 6)
        )

        log_outer, log_card = self._card(shell)
        log_outer.pack(fill="both", expand=True)
        log_inner = ttk.Frame(log_card, style="Inner.TFrame")
        log_inner.pack(fill="both", expand=True, padx=8, pady=8)

        self.log_notebook = ttk.Notebook(log_inner)
        self.log_notebook.pack(fill="both", expand=True)

        for channel, title in LOG_CHANNELS:
            page = ttk.Frame(self.log_notebook, style="Card.TFrame")
            self.log_notebook.add(page, text=title)
            widget = scrolledtext.ScrolledText(
                page,
                height=16,
                wrap="word",
                font=("Consolas", 9),
                bg=THEME["log_bg"],
                fg=THEME["log_fg"],
                insertbackground=THEME["accent"],
                selectbackground=THEME["surface_alt"],
                selectforeground=THEME["text"],
                relief="flat",
                borderwidth=0,
                highlightthickness=0,
                padx=10,
                pady=8,
            )
            widget.pack(fill="both", expand=True)
            widget.configure(state="disabled")
            self.log_widgets[channel] = widget

    def _set_status_dot(self, key: str, color: str) -> None:
        canvas = self.status_dot_canvases.get(key)
        if canvas is None:
            return
        canvas.delete("all")
        canvas.create_oval(1, 1, 11, 11, fill=color, outline=color)

    def log(self, message: str, *, channel: str = "system") -> None:
        stamp = time.strftime("%H:%M:%S")
        target = channel if channel in self.log_widgets else "system"
        self.log_queue.put((target, f"[{stamp}] {message}"))

    def _poll_log_queue(self) -> None:
        try:
            while True:
                channel, line = self.log_queue.get_nowait()
                widget = self.log_widgets.get(channel) or self.log_widgets["system"]
                widget.configure(state="normal")
                widget.insert("end", line + "\n")
                widget.see("end")
                widget.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(120, self._poll_log_queue)

    def _clear_active_log(self) -> None:
        try:
            tab_id = self.log_notebook.index("current")
            channel = LOG_CHANNELS[tab_id][0]
        except Exception:  # noqa: BLE001
            channel = "system"
        self._clear_log_channel(channel)

    def _clear_all_logs(self) -> None:
        for channel, _title in LOG_CHANNELS:
            self._clear_log_channel(channel)

    def _clear_log_channel(self, channel: str) -> None:
        widget = self.log_widgets.get(channel)
        if widget is None:
            return
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.configure(state="disabled")

    def _focus_log_channel(self, channel: str) -> None:
        for index, (key, _title) in enumerate(LOG_CHANNELS):
            if key == channel:
                self.log_notebook.select(index)
                return

    def _run_async(
        self,
        action: str,
        title: str,
        fn,
        *,
        button: ttk.Button | None = None,
        channel: str | None = None,
    ) -> None:
        lock = self._action_locks[action]
        log_channel = channel or ACTION_TO_CHANNEL.get(action, "system")

        def worker() -> None:
            if not lock.acquire(blocking=False):
                self.log(f"忙碌中，忽略: {title}", channel=log_channel)
                return
            try:
                if button is not None:
                    self.root.after(0, lambda: button.configure(state="disabled"))
                self.root.after(0, lambda: self._focus_log_channel(log_channel))
                self.log(f"开始: {title}", channel=log_channel)
                fn()
                self.log(f"完成: {title}", channel=log_channel)
            except Exception as exc:  # noqa: BLE001 - show in UI
                self.log(f"失败: {title} — {exc}", channel=log_channel)
                self.root.after(0, lambda: messagebox.showerror(title, str(exc)))
            finally:
                lock.release()
                if button is not None:
                    self.root.after(0, lambda: button.configure(state="normal"))
                self.root.after(0, self._update_status_labels)

        threading.Thread(target=worker, name=f"dev-ui-{action}", daemon=True).start()

    def _update_status_labels(self) -> None:
        mtx_up = tcp_open("127.0.0.1", RTSP_PORT)
        mtx = "运行中" if mtx_up else "未启动"
        stream_alive = self.stream_proc is not None and self.stream_proc.poll() is None
        stream = "推流中" if stream_alive else "已停止"
        yolo_alive = self.yolo_proc is not None and self.yolo_proc.poll() is None
        yolo_port = tcp_open(YOLO_HOST, YOLO_PORT)
        if yolo_alive or yolo_port:
            yolo = "运行中" if yolo_port else "启动中"
            yolo_color = THEME["ok"] if yolo_port else THEME["warn"]
        else:
            yolo = "未启动"
            yolo_color = THEME["off"]

        self.status_vars["mediamtx"].set(mtx)
        self.status_vars["stream"].set(stream)
        self.status_vars["yolo"].set(yolo)
        self._set_status_dot("mediamtx", THEME["ok"] if mtx_up else THEME["off"])
        self._set_status_dot("stream", THEME["ok"] if stream_alive else THEME["off"])
        self._set_status_dot("yolo", yolo_color)

    def _refresh_status(self) -> None:
        self._update_status_labels()
        self.root.after(2000, self._refresh_status)

    def _run_command(
        self,
        command: list[str],
        *,
        cwd: Path,
        label: str,
        env: dict[str, str] | None = None,
    ) -> int:
        channel = LABEL_TO_CHANNEL.get(label, "system")
        self.log(f"$ {' '.join(command)}", channel=channel)
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert process.stdout is not None
        for line in process.stdout:
            text = line.rstrip()
            if text:
                self.log(text, channel=channel)
        return process.wait()

    def start_mediamtx(self) -> None:
        self._run_async("mediamtx", "挂起 MediaMTX", self._start_mediamtx, button=self.btn_mediamtx)

    def _start_mediamtx(self) -> None:
        if shutil.which("docker") is None:
            raise RuntimeError("未找到 docker，请先安装并启动 Docker Desktop")

        if tcp_open("127.0.0.1", RTSP_PORT):
            self.log(f"端口 {RTSP_PORT} 已监听，跳过启动", channel="mediamtx")
            return

        existing = subprocess.run(
            ["docker", "ps", "-aq", "--filter", f"name={MEDIAMTX_NAME}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if existing.stdout.strip():
            self.log(f"清理旧容器 {MEDIAMTX_NAME}", channel="mediamtx")
            subprocess.run(["docker", "rm", "-f", MEDIAMTX_NAME], capture_output=True, text=True, check=False)

        code = self._run_command(
            [
                "docker",
                "run",
                "-d",
                "--rm",
                "--name",
                MEDIAMTX_NAME,
                "-p",
                f"{RTSP_PORT}:8554",
                "bluenviron/mediamtx:latest",
            ],
            cwd=REPO_ROOT,
            label="mediamtx",
        )
        if code != 0:
            raise RuntimeError(f"docker run MediaMTX 失败 (exit {code})")

        deadline = time.time() + 120
        while time.time() < deadline:
            if tcp_open("127.0.0.1", RTSP_PORT):
                self.log(f"MediaMTX 已就绪: rtsp://127.0.0.1:{RTSP_PORT}/live", channel="mediamtx")
                return
            time.sleep(0.4)
        raise RuntimeError(f"等待 MediaMTX 端口 {RTSP_PORT} 超时")

    def start_stream(self) -> None:
        self._run_async("stream_on", "打开推流", self._start_stream, button=self.btn_stream_on)

    def _start_stream(self) -> None:
        if self.stream_proc is not None and self.stream_proc.poll() is None:
            self.log("推流已在运行", channel="stream")
            return
        if not tcp_open("127.0.0.1", RTSP_PORT):
            raise RuntimeError("MediaMTX 未就绪，请先点击「挂起 MediaMTX」")
        if not GENERATE_SCRIPT.is_file():
            raise RuntimeError(f"找不到推流脚本: {GENERATE_SCRIPT}")

        python = resolve_python()
        command = [
            python,
            str(GENERATE_SCRIPT),
            "--port",
            str(RTSP_PORT),
            "--skip-server-check",
        ]
        self.log(f"$ {' '.join(command)}", channel="stream")
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]

        self.stream_proc = subprocess.Popen(
            command,
            cwd=str(REPO_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
        )
        threading.Thread(
            target=self._pump_process_output,
            args=(self.stream_proc, "stream"),
            daemon=True,
        ).start()
        # FFmpeg may take a few seconds to fail on bad filters / unreachable RTSP.
        deadline = time.time() + 4.0
        while time.time() < deadline:
            if self.stream_proc.poll() is not None:
                raise RuntimeError("推流进程已退出，请查看「推流」日志中的 ffmpeg 错误")
            time.sleep(0.25)
        self.log("推流已启动 (video=/live, time=/time)", channel="stream")

    def stop_stream(self) -> None:
        self._run_async("stream_off", "关闭推流", self._stop_stream, button=self.btn_stream_off)

    def _stop_stream(self) -> None:
        proc = self.stream_proc
        if proc is None or proc.poll() is not None:
            self.stream_proc = None
            self.log("当前没有推流进程", channel="stream")
            return
        self.log(f"停止推流 PID {proc.pid}", channel="stream")
        self._terminate_process(proc)
        self.stream_proc = None
        self.log("推流已关闭", channel="stream")

    def start_yolo(self) -> None:
        self._run_async("yolo", "打开 YOLO", self._start_yolo, button=self.btn_yolo)

    def _start_yolo(self) -> None:
        if tcp_open(YOLO_HOST, YOLO_PORT):
            self.log(f"YOLO 已在 {YOLO_HOST}:{YOLO_PORT} 监听，跳过启动", channel="yolo")
            return
        if self.yolo_proc is not None and self.yolo_proc.poll() is None:
            self.log("YOLO 进程已在运行", channel="yolo")
            return
        if not YOLO_SCRIPT.is_file():
            raise RuntimeError(f"找不到 YOLO 脚本: {YOLO_SCRIPT}")

        command = [
            resolve_powershell(),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(YOLO_SCRIPT),
            "-HostAddress",
            YOLO_HOST,
            "-Port",
            str(YOLO_PORT),
        ]
        self.log(f"$ {' '.join(command)}", channel="yolo")
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]

        self.yolo_proc = subprocess.Popen(
            command,
            cwd=str(BACKEND_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
        )
        threading.Thread(
            target=self._pump_process_output,
            args=(self.yolo_proc, "yolo"),
            daemon=True,
        ).start()
        self.log("YOLO 启动中（首次会装依赖，可能较慢）…", channel="yolo")

    def rebuild_docker(self) -> None:
        self._run_async("rebuild", "重建 Docker", self._rebuild_docker, button=self.btn_rebuild)

    def _rebuild_docker(self) -> None:
        if shutil.which("docker") is None:
            raise RuntimeError("未找到 docker")

        if not ENV_FILE.exists():
            if not ENV_EXAMPLE.exists():
                raise RuntimeError(f"缺少 {ENV_EXAMPLE}")
            shutil.copyfile(ENV_EXAMPLE, ENV_FILE)
            self.log(f"已复制 {ENV_EXAMPLE.name} -> {ENV_FILE.name}", channel="compose")
        else:
            self.log(f"已存在 {ENV_FILE.name}，跳过覆盖（避免清掉本地密钥）", channel="compose")

        code = self._run_command(
            ["docker", "compose", "up", "--build", "-d"],
            cwd=REPO_ROOT,
            label="compose",
        )
        if code != 0:
            raise RuntimeError(f"docker compose up --build -d 失败 (exit {code})")
        self.log("应用 Docker 栈已重建并后台运行", channel="compose")

    def stop_all(self) -> None:
        self._run_async("stop_all", "全部关闭", self._stop_all, button=self.btn_stop_all)

    def _stop_all(self) -> None:
        self.log("正在关闭推流 / YOLO / MediaMTX …", channel="system")
        self._stop_stream()
        self._stop_yolo()
        self._stop_mediamtx()
        self.log("全部关闭完成（不含应用 docker compose 栈）", channel="system")

    def _stop_yolo(self) -> None:
        proc = self.yolo_proc
        if proc is not None and proc.poll() is None:
            self.log(f"停止 YOLO PID {proc.pid}", channel="yolo")
            self._terminate_process(proc)
        self.yolo_proc = None

        for pid in self._pids_listening_on_port(YOLO_PORT):
            self.log(f"结束占用 YOLO 端口的进程 PID {pid}", channel="yolo")
            self._kill_pid_tree(pid)

        if tcp_open(YOLO_HOST, YOLO_PORT):
            self.log("警告: YOLO 端口仍在监听，可能需手动结束", channel="yolo")
        else:
            self.log("YOLO 已关闭", channel="yolo")

    def _stop_mediamtx(self) -> None:
        if shutil.which("docker") is None:
            self.log("未找到 docker，跳过 MediaMTX 清理", channel="mediamtx")
            return

        existing = subprocess.run(
            ["docker", "ps", "-aq", "--filter", f"name={MEDIAMTX_NAME}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if existing.stdout.strip():
            self.log(f"移除 MediaMTX 容器 {MEDIAMTX_NAME}", channel="mediamtx")
            subprocess.run(
                ["docker", "rm", "-f", MEDIAMTX_NAME],
                capture_output=True,
                text=True,
                check=False,
            )
        else:
            self.log("没有 MediaMTX 容器需要清理", channel="mediamtx")

        if tcp_open("127.0.0.1", RTSP_PORT):
            self.log(f"警告: 端口 {RTSP_PORT} 仍在监听（可能不是本面板启动的）", channel="mediamtx")
        else:
            self.log("MediaMTX 已关闭", channel="mediamtx")

    def _pids_listening_on_port(self, port: int) -> list[int]:
        if os.name != "nt":
            return []
        result = subprocess.run(
            [
                resolve_powershell(),
                "-NoProfile",
                "-Command",
                (
                    f"Get-NetTCPConnection -LocalPort {port} -State Listen "
                    "-ErrorAction SilentlyContinue | "
                    "Select-Object -ExpandProperty OwningProcess -Unique"
                ),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        pids: list[int] = []
        for line in result.stdout.splitlines():
            text = line.strip()
            if text.isdigit():
                pids.append(int(text))
        return pids

    def _kill_pid_tree(self, pid: int) -> None:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
                check=False,
            )
            return
        try:
            os.kill(pid, 15)
        except OSError:
            pass

    def _pump_process_output(self, process: subprocess.Popen[str], label: str) -> None:
        channel = LABEL_TO_CHANNEL.get(label, "system")
        if process.stdout is None:
            return
        for line in process.stdout:
            text = line.rstrip()
            if text:
                self.log(text, channel=channel)
        code = process.wait()
        self.log(f"进程退出 (exit {code})", channel=channel)

    def _terminate_process(self, process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name == "nt":
                # Kill the whole tree (ffmpeg child of the publisher).
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                try:
                    process.wait(timeout=5)
                    return
                except subprocess.TimeoutExpired:
                    pass
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        except Exception as exc:  # noqa: BLE001
            self.log(f"终止进程异常: {exc}", channel="system")
            try:
                process.kill()
            except Exception:  # noqa: BLE001
                pass

    def _on_close(self) -> None:
        if self.stream_proc is not None and self.stream_proc.poll() is None:
            if messagebox.askyesno("退出", "推流仍在运行，是否先关闭推流并退出？"):
                try:
                    self._stop_stream()
                except Exception:  # noqa: BLE001
                    pass
            else:
                return
        self.root.destroy()


def main() -> int:
    root = tk.Tk()
    try:
        root.call("tk", "scaling", 1.2)
    except tk.TclError:
        pass
    DevStackUI(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
