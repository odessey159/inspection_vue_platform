#!/usr/bin/env python3
"""Inspection Vue Platform local service control panel.

This file intentionally uses only Python's standard library so the panel can be
opened before the project dependencies are installed.  It starts the frontend,
backend API and standalone YOLO service as local child processes and keeps each
service's output in a separate log tab.
"""

from __future__ import annotations

import os
import queue
import shutil
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk


ROOT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = ROOT_DIR / "backend"
WEB_DIR = ROOT_DIR / "web"

FRONTEND_HOST = "127.0.0.1"
FRONTEND_PORT = 5173
BACKEND_HOST = "127.0.0.1"
BACKEND_PORT = 8010
YOLO_HOST = "127.0.0.1"
YOLO_PORT = 8001

FRONTEND_URL = f"http://{FRONTEND_HOST}:{FRONTEND_PORT}"
BACKEND_URL = f"http://{BACKEND_HOST}:{BACKEND_PORT}"
YOLO_URL = f"http://{YOLO_HOST}:{YOLO_PORT}"

LOG_CHANNELS = (
    ("system", "系统"),
    ("frontend", "前端"),
    ("backend", "后端 API"),
    ("yolo", "YOLO"),
)

THEME = {
    "bg": "#0f1419",
    "surface": "#1a2332",
    "surface_alt": "#243044",
    "border": "#2e3d52",
    "text": "#e8eef6",
    "muted": "#8b9bb0",
    "accent": "#2dd4bf",
    "accent_dim": "#0d9488",
    "ok": "#34d399",
    "warn": "#fbbf24",
    "off": "#64748b",
    "danger": "#f87171",
    "danger_bg": "#7f1d1d",
    "log_bg": "#0b1220",
    "log_fg": "#c8d5e4",
    "button_text": "#0f1419",
}


@dataclass(frozen=True)
class ServiceSpec:
    key: str
    name: str
    host: str
    port: int
    url: str
    cwd: Path


SERVICES = {
    "frontend": ServiceSpec(
        key="frontend",
        name="前端",
        host=FRONTEND_HOST,
        port=FRONTEND_PORT,
        url=FRONTEND_URL,
        cwd=WEB_DIR,
    ),
    "backend": ServiceSpec(
        key="backend",
        name="后端 API",
        host=BACKEND_HOST,
        port=BACKEND_PORT,
        url=f"{BACKEND_URL}/healthz",
        cwd=BACKEND_DIR,
    ),
    "yolo": ServiceSpec(
        key="yolo",
        name="YOLO",
        host=YOLO_HOST,
        port=YOLO_PORT,
        url=f"{YOLO_URL}/healthz",
        cwd=BACKEND_DIR,
    ),
}


def tcp_open(host: str, port: int, timeout: float = 0.25) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _unique(items: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        key = os.path.normcase(os.path.abspath(item)) if os.path.isabs(item) else item.lower()
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def _hidden_process_flags() -> int:
    if os.name != "nt":
        return 0
    return subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]


def resolve_python() -> str | None:
    """Find a working Python, preferring the backend virtual environment."""
    candidates: list[str] = []
    venv_python = BACKEND_DIR / ".venv" / "Scripts" / "python.exe"
    if venv_python.is_file():
        candidates.append(str(venv_python))

    current = Path(sys.executable)
    if current.name.lower() == "pythonw.exe":
        console_python = current.with_name("python.exe")
        if console_python.is_file():
            candidates.append(str(console_python))
    if current.is_file():
        candidates.append(str(current))

    for executable in ("python", "python3", "py"):
        found = shutil.which(executable)
        if found:
            candidates.append(found)

    for candidate in _unique(candidates):
        try:
            result = subprocess.run(
                [candidate, "-c", "import sys; raise SystemExit(0)"],
                capture_output=True,
                timeout=8,
                check=False,
                creationflags=_hidden_process_flags(),
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0:
            return candidate
    return None


def resolve_npm() -> str | None:
    if os.name == "nt":
        return shutil.which("npm.cmd") or shutil.which("npm")
    return shutil.which("npm")


def resolve_powershell() -> str:
    return shutil.which("powershell.exe") or shutil.which("powershell") or "powershell.exe"


def command_text(command: list[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(command)
    try:
        import shlex

        return shlex.join(command)
    except AttributeError:
        return " ".join(command)


def run_probe(command: list[str], *, cwd: Path = ROOT_DIR, timeout: int = 20) -> tuple[bool, str]:
    probe_env = os.environ.copy()
    probe_env["PYTHONIOENCODING"] = "utf-8"
    try:
        result = subprocess.run(
            command,
            cwd=str(cwd),
            env=probe_env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            creationflags=_hidden_process_flags(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    output = (result.stdout or result.stderr).strip()
    return result.returncode == 0, output


def environment_report() -> list[tuple[bool, str]]:
    report: list[tuple[bool, str]] = []

    python = resolve_python()
    if python:
        ok, version = run_probe([python, "--version"])
        report.append((ok, f"Python: {version or python}"))
        ok, details = run_probe(
            [
                python,
                "-c",
                "import fastapi, uvicorn, sqlmodel; print('后端依赖已安装')",
            ],
            cwd=BACKEND_DIR,
        )
        report.append((ok, details or "后端依赖缺失，请安装 backend/requirements.txt"))
    else:
        report.append((False, "未找到可用的 Python"))

    npm = resolve_npm()
    if npm:
        ok, version = run_probe([npm, "--version"], cwd=WEB_DIR)
        report.append((ok, f"npm: {version or npm}"))
    else:
        report.append((False, "未找到 npm，请安装 Node.js"))

    node_modules = WEB_DIR / "node_modules"
    report.append(
        (
            node_modules.is_dir(),
            "前端依赖已安装" if node_modules.is_dir() else "前端依赖缺失，请在 web 目录运行 npm install",
        )
    )
    yolo_script = BACKEND_DIR / "scripts" / "run_yolo_service.ps1"
    report.append(
        (
            yolo_script.is_file(),
            "YOLO 将沿用原启动脚本" if yolo_script.is_file() else f"缺少 YOLO 启动脚本: {yolo_script}",
        )
    )
    return report


class ServiceConsole:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Inspection Vue Platform · 服务控制台")
        self.root.geometry("1040x720")
        self.root.minsize(900, 620)
        self.root.configure(bg=THEME["bg"])

        self.processes: dict[str, subprocess.Popen[str] | None] = {
            key: None for key in SERVICES
        }
        self.process_lock = threading.RLock()
        self.action_lock = threading.Lock()
        self.log_queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self.log_widgets: dict[str, scrolledtext.ScrolledText] = {}
        self.status_vars: dict[str, tk.StringVar] = {}
        self.status_dots: dict[str, tk.Canvas] = {}
        self.start_buttons: dict[str, ttk.Button] = {}
        self.stop_buttons: dict[str, ttk.Button] = {}

        self._apply_theme()
        self._build_ui()
        self._poll_log_queue()
        self._refresh_status()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.log(f"项目目录：{ROOT_DIR}")
        self.log("控制台已就绪。可点击“全部启动”，首次加载 YOLO 模型可能需要一些时间。")

    def _apply_theme(self) -> None:
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")

        style.configure(".", background=THEME["bg"], foreground=THEME["text"], font=("Microsoft YaHei UI", 10))
        style.configure("TFrame", background=THEME["bg"])
        style.configure("Card.TFrame", background=THEME["surface"])
        style.configure("Inner.TFrame", background=THEME["surface"])
        style.configure("TLabel", background=THEME["bg"], foreground=THEME["text"])
        style.configure("Card.TLabel", background=THEME["surface"], foreground=THEME["text"])
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 18, "bold"))
        style.configure("Muted.TLabel", foreground=THEME["muted"], font=("Microsoft YaHei UI", 9))
        style.configure("CardMuted.TLabel", background=THEME["surface"], foreground=THEME["muted"], font=("Microsoft YaHei UI", 9))
        style.configure("Service.TLabel", background=THEME["surface"], foreground=THEME["text"], font=("Microsoft YaHei UI", 11, "bold"))
        style.configure("Status.TLabel", background=THEME["surface"], foreground=THEME["muted"], font=("Microsoft YaHei UI", 9))
        style.configure("Url.TLabel", background=THEME["surface"], foreground=THEME["accent"], font=("Consolas", 9))

        self._button_style("Accent.TButton", THEME["accent_dim"], THEME["button_text"], bold=True)
        self._button_style("Secondary.TButton", THEME["surface_alt"], THEME["text"])
        self._button_style("Danger.TButton", THEME["danger_bg"], THEME["danger"], bold=True)
        style.map("Accent.TButton", background=[("active", THEME["accent"]), ("disabled", THEME["border"])])
        style.map("Secondary.TButton", background=[("active", THEME["border"]), ("disabled", THEME["surface"])])
        style.map("Danger.TButton", background=[("active", "#991b1b"), ("disabled", THEME["surface"])])

        style.configure("TNotebook", background=THEME["bg"], borderwidth=0)
        style.configure("TNotebook.Tab", background=THEME["surface"], foreground=THEME["muted"], padding=(14, 8))
        style.map("TNotebook.Tab", background=[("selected", THEME["surface_alt"])], foreground=[("selected", THEME["accent"])])

    def _button_style(self, name: str, background: str, foreground: str, *, bold: bool = False) -> None:
        style = ttk.Style(self.root)
        style.configure(
            name,
            background=background,
            foreground=foreground,
            bordercolor=background,
            lightcolor=background,
            darkcolor=background,
            padding=(12, 9),
            font=("Microsoft YaHei UI", 9, "bold" if bold else "normal"),
        )

    def _card(self, parent: tk.Misc) -> tuple[tk.Frame, ttk.Frame]:
        outer = tk.Frame(parent, bg=THEME["border"], bd=0, highlightthickness=0)
        inner = ttk.Frame(outer, style="Card.TFrame")
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        return outer, inner

    def _build_ui(self) -> None:
        shell = ttk.Frame(self.root)
        shell.pack(fill="both", expand=True, padx=18, pady=16)

        header = ttk.Frame(shell)
        header.pack(fill="x", pady=(0, 14))
        heading = ttk.Frame(header)
        heading.pack(side="left", fill="x", expand=True)
        ttk.Label(heading, text="Inspection Vue Platform", style="Title.TLabel").pack(anchor="w")
        ttk.Label(heading, text="本地前端、后端 API 与 YOLO 服务控制台", style="Muted.TLabel").pack(anchor="w", pady=(2, 0))

        header_actions = ttk.Frame(header)
        header_actions.pack(side="right")
        self.btn_start_all = ttk.Button(header_actions, text="全部启动", style="Accent.TButton", command=self.start_all)
        self.btn_start_all.pack(side="left", padx=(0, 8))
        self.btn_stop_all = ttk.Button(header_actions, text="全部停止", style="Danger.TButton", command=self.stop_all)
        self.btn_stop_all.pack(side="left")

        cards = ttk.Frame(shell)
        cards.pack(fill="x", pady=(0, 12))
        for column in range(3):
            cards.columnconfigure(column, weight=1, uniform="services")

        for column, key in enumerate(("frontend", "backend", "yolo")):
            spec = SERVICES[key]
            outer, card = self._card(cards)
            outer.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 8, 0))
            body = ttk.Frame(card, style="Inner.TFrame")
            body.pack(fill="both", expand=True, padx=14, pady=13)

            title_row = ttk.Frame(body, style="Inner.TFrame")
            title_row.pack(fill="x")
            dot = tk.Canvas(title_row, width=12, height=12, bg=THEME["surface"], highlightthickness=0, bd=0)
            dot.pack(side="left", padx=(0, 8), pady=2)
            self.status_dots[key] = dot
            self._set_status_dot(key, THEME["off"])
            ttk.Label(title_row, text=spec.name, style="Service.TLabel").pack(side="left")
            ttk.Label(title_row, text=f"端口 {spec.port}", style="CardMuted.TLabel").pack(side="right")

            status_var = tk.StringVar(value="未启动")
            self.status_vars[key] = status_var
            ttk.Label(body, textvariable=status_var, style="Status.TLabel").pack(anchor="w", pady=(9, 2))
            ttk.Label(body, text=spec.url, style="Url.TLabel").pack(anchor="w", pady=(0, 11))

            buttons = ttk.Frame(body, style="Inner.TFrame")
            buttons.pack(fill="x")
            buttons.columnconfigure(0, weight=1)
            buttons.columnconfigure(1, weight=1)
            start_button = ttk.Button(
                buttons,
                text="启动",
                style="Accent.TButton",
                command=lambda service_key=key: self.start_service(service_key),
            )
            stop_button = ttk.Button(
                buttons,
                text="停止",
                style="Secondary.TButton",
                command=lambda service_key=key: self.stop_service(service_key),
            )
            start_button.grid(row=0, column=0, sticky="ew", padx=(0, 4))
            stop_button.grid(row=0, column=1, sticky="ew", padx=(4, 0))
            self.start_buttons[key] = start_button
            self.stop_buttons[key] = stop_button

        tools_outer, tools_card = self._card(shell)
        tools_outer.pack(fill="x", pady=(0, 12))
        tools = ttk.Frame(tools_card, style="Inner.TFrame")
        tools.pack(fill="x", padx=12, pady=10)
        ttk.Label(tools, text="快捷操作", style="Card.TLabel").pack(side="left", padx=(2, 12))
        ttk.Button(tools, text="打开前端页面", style="Secondary.TButton", command=self.open_frontend).pack(side="left", padx=(0, 8))
        ttk.Button(tools, text="环境检查", style="Secondary.TButton", command=self.check_environment).pack(side="left")
        path_text = str(ROOT_DIR)
        if len(path_text) > 62:
            path_text = "…" + path_text[-61:]
        ttk.Label(tools, text=path_text, style="CardMuted.TLabel").pack(side="right", padx=4)

        log_header = ttk.Frame(shell)
        log_header.pack(fill="x", pady=(0, 6))
        ttk.Label(log_header, text="运行日志", style="Muted.TLabel").pack(side="left")
        ttk.Button(log_header, text="清空当前日志", style="Secondary.TButton", command=self._clear_active_log).pack(side="right")

        log_outer, log_card = self._card(shell)
        log_outer.pack(fill="both", expand=True)
        log_body = ttk.Frame(log_card, style="Inner.TFrame")
        log_body.pack(fill="both", expand=True, padx=8, pady=8)
        self.log_notebook = ttk.Notebook(log_body)
        self.log_notebook.pack(fill="both", expand=True)

        for channel, title in LOG_CHANNELS:
            page = ttk.Frame(self.log_notebook, style="Card.TFrame")
            self.log_notebook.add(page, text=title)
            widget = scrolledtext.ScrolledText(
                page,
                height=15,
                wrap="word",
                font=("Consolas", 9),
                bg=THEME["log_bg"],
                fg=THEME["log_fg"],
                insertbackground=THEME["accent"],
                selectbackground=THEME["surface_alt"],
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
        canvas = self.status_dots.get(key)
        if canvas is None:
            return
        canvas.delete("all")
        canvas.create_oval(1, 1, 11, 11, fill=color, outline=color)

    def log(self, message: str, *, channel: str = "system") -> None:
        target = channel if channel in dict(LOG_CHANNELS) else "system"
        for line in str(message).splitlines() or [""]:
            stamp = time.strftime("%H:%M:%S")
            self.log_queue.put((target, f"[{stamp}] {line}"))

    def _poll_log_queue(self) -> None:
        try:
            while True:
                channel, line = self.log_queue.get_nowait()
                widget = self.log_widgets[channel]
                widget.configure(state="normal")
                widget.insert("end", line + "\n")
                widget.see("end")
                widget.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(100, self._poll_log_queue)

    def _focus_log(self, channel: str) -> None:
        for index, (key, _title) in enumerate(LOG_CHANNELS):
            if key == channel:
                self.log_notebook.select(index)
                break

    def _clear_active_log(self) -> None:
        index = self.log_notebook.index("current")
        channel = LOG_CHANNELS[index][0]
        widget = self.log_widgets[channel]
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.configure(state="disabled")

    def _refresh_status(self) -> None:
        with self.process_lock:
            snapshot = dict(self.processes)
        for key, spec in SERVICES.items():
            process = snapshot[key]
            owned_alive = process is not None and process.poll() is None
            port_open = tcp_open(spec.host, spec.port)
            if owned_alive and port_open:
                label, color = "运行中", THEME["ok"]
            elif owned_alive:
                label, color = "启动中…", THEME["warn"]
            elif port_open:
                label, color = "运行中（外部进程）", THEME["ok"]
            elif process is not None and process.returncode not in (None, 0):
                label, color = f"已退出（代码 {process.returncode}）", THEME["danger"]
            else:
                label, color = "未启动", THEME["off"]
            self.status_vars[key].set(label)
            self._set_status_dot(key, color)
        self.root.after(1500, self._refresh_status)

    def _run_async(self, title: str, callback, *, channel: str = "system", button: ttk.Button | None = None) -> None:
        def worker() -> None:
            if not self.action_lock.acquire(blocking=False):
                self.log("已有操作正在执行，请稍候。", channel=channel)
                return
            try:
                if button is not None:
                    self.root.after(0, lambda: button.configure(state="disabled"))
                self.root.after(0, lambda: self._focus_log(channel))
                self.log(f"开始：{title}", channel=channel)
                callback()
            except Exception as exc:  # noqa: BLE001 - errors must be visible in the panel
                self.log(f"失败：{exc}", channel=channel)
                self.root.after(0, lambda error=str(exc): messagebox.showerror(title, error))
            finally:
                if button is not None:
                    self.root.after(0, lambda: button.configure(state="normal"))
                self.action_lock.release()

        threading.Thread(target=worker, name=f"service-console-{channel}", daemon=True).start()

    def _service_command(self, key: str) -> tuple[list[str], dict[str, str]]:
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"

        if key == "frontend":
            npm = resolve_npm()
            if not npm:
                raise RuntimeError("未找到 npm，请先安装 Node.js。")
            if not (WEB_DIR / "node_modules").is_dir():
                raise RuntimeError("前端依赖尚未安装，请先在 web 目录运行 npm install。")
            env["VITE_API_PROXY"] = BACKEND_URL
            return [npm, "run", "dev", "--", "--host", FRONTEND_HOST, "--port", str(FRONTEND_PORT)], env

        if key == "yolo":
            yolo_script = BACKEND_DIR / "scripts" / "run_yolo_service.ps1"
            if not yolo_script.is_file():
                raise RuntimeError(f"找不到原 YOLO 启动脚本：{yolo_script}")
            return [
                resolve_powershell(),
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(yolo_script),
                "-HostAddress",
                YOLO_HOST,
                "-Port",
                str(YOLO_PORT),
            ], env

        python = resolve_python()
        if not python:
            raise RuntimeError("未找到可用的 Python。请安装 Python，或创建 backend/.venv。")

        if key == "backend":
            ok, details = run_probe(
                [python, "-c", "import fastapi, uvicorn, sqlmodel"],
                cwd=BACKEND_DIR,
            )
            if not ok:
                raise RuntimeError(
                    "后端依赖尚未安装，请运行：\n"
                    f'  "{python}" -m pip install -r backend/requirements.txt\n\n{details}'
                )
            env["YOLO_API_URL"] = YOLO_URL
            return [
                python,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                BACKEND_HOST,
                "--port",
                str(BACKEND_PORT),
            ], env

        raise KeyError(key)

    def start_service(self, key: str) -> None:
        spec = SERVICES[key]
        self._run_async(
            f"启动{spec.name}",
            lambda: self._start_service(key),
            channel=key,
            button=self.start_buttons[key],
        )

    def _start_service(self, key: str) -> None:
        spec = SERVICES[key]
        with self.process_lock:
            current = self.processes[key]
        if current is not None and current.poll() is None:
            self.log(f"{spec.name} 已由本控制台启动，无需重复启动。", channel=key)
            return
        if tcp_open(spec.host, spec.port):
            self.log(f"{spec.name} 端口 {spec.port} 已被其他进程占用，跳过启动。", channel=key)
            return

        command, env = self._service_command(key)
        self.log(f"$ {command_text(command)}", channel=key)
        creationflags = 0
        if os.name == "nt":
            creationflags = (
                subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
                | subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
            )
        process = subprocess.Popen(
            command,
            cwd=str(spec.cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
        )
        with self.process_lock:
            self.processes[key] = process
        threading.Thread(
            target=self._pump_process_output,
            args=(key, process),
            name=f"service-log-{key}",
            daemon=True,
        ).start()
        self.log(f"{spec.name} 进程已启动（PID {process.pid}），正在等待端口就绪。", channel=key)

    def stop_service(self, key: str) -> None:
        spec = SERVICES[key]
        self._run_async(
            f"停止{spec.name}",
            lambda: self._stop_service(key),
            channel=key,
            button=self.stop_buttons[key],
        )

    def _stop_service(self, key: str) -> None:
        spec = SERVICES[key]
        with self.process_lock:
            process = self.processes[key]
        if process is None or process.poll() is not None:
            with self.process_lock:
                self.processes[key] = None
            if tcp_open(spec.host, spec.port):
                self.log(
                    f"{spec.name} 由外部进程运行，本控制台不会强制结束它。",
                    channel=key,
                )
            else:
                self.log(f"{spec.name} 当前未运行。", channel=key)
            return

        self.log(f"正在停止 {spec.name}（PID {process.pid}）…", channel=key)
        self._terminate_process_tree(process)
        with self.process_lock:
            self.processes[key] = None
        self.log(f"{spec.name} 已停止。", channel=key)

    def start_all(self) -> None:
        def action() -> None:
            for key in ("backend", "frontend", "yolo"):
                self._start_service(key)
            self.log("三项服务的启动命令均已提交；状态卡片变绿后即可使用。")

        self._run_async("全部启动", action, button=self.btn_start_all)

    def stop_all(self) -> None:
        def action() -> None:
            for key in ("frontend", "backend", "yolo"):
                self._stop_service(key)
            self.log("本控制台启动的服务已全部停止。")

        self._run_async("全部停止", action, button=self.btn_stop_all)

    def _pump_process_output(self, key: str, process: subprocess.Popen[str]) -> None:
        if process.stdout is not None:
            for line in process.stdout:
                text = line.rstrip()
                if text:
                    self.log(text, channel=key)
        code = process.wait()
        self.log(f"进程已退出（代码 {code}）。", channel=key)

    def _terminate_process_tree(self, process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                text=True,
                check=False,
                creationflags=_hidden_process_flags(),
            )
            try:
                process.wait(timeout=8)
                return
            except subprocess.TimeoutExpired:
                pass
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    def open_frontend(self) -> None:
        webbrowser.open(FRONTEND_URL)
        self.log(f"已请求浏览器打开：{FRONTEND_URL}")

    def check_environment(self) -> None:
        def action() -> None:
            rows = environment_report()
            for ok, text in rows:
                self.log(f"{'通过' if ok else '缺失'}｜{text}")
            passed = sum(1 for ok, _text in rows if ok)
            self.log(f"环境检查完成：{passed}/{len(rows)} 项通过。")

        self._run_async("环境检查", action)

    def _on_close(self) -> None:
        with self.process_lock:
            running = [key for key, process in self.processes.items() if process is not None and process.poll() is None]
        if running:
            names = "、".join(SERVICES[key].name for key in running)
            if not messagebox.askyesno("退出控制台", f"{names} 仍在运行。是否停止这些服务并退出？"):
                return
            for key in ("frontend", "backend", "yolo"):
                if key in running:
                    try:
                        self._stop_service(key)
                    except Exception as exc:  # noqa: BLE001
                        self.log(f"退出时停止 {SERVICES[key].name} 失败：{exc}")
        self.root.destroy()


def print_environment_report() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    rows = environment_report()
    for ok, description in rows:
        print(f"[{'OK' if ok else 'FAIL'}] {description}")
    return 0 if all(ok for ok, _description in rows) else 1


def main() -> int:
    if "--check" in sys.argv:
        return print_environment_report()

    root = tk.Tk()
    try:
        root.call("tk", "scaling", 1.15)
    except tk.TclError:
        pass
    ServiceConsole(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
