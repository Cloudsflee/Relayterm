"""Hidden Windows PC launcher and Ctrl+Alt+Shift+R project launcher."""

from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import json
import os
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
import uuid
from ctypes import wintypes
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from urllib.parse import quote

if __package__ in (None, ""):
    PROJECT_ROOT = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(PROJECT_ROOT))
    from pc.config import Profile, SettingsStore, TokenStore, data_directory, profile_store
    from pc.runtime import (
        HotKeyListener, SingleInstance, configure_logging, find_cloudflared,
        install_startup, port_available, write_process_record,
    )
    from pc.tunnel import QuickTunnel
    from pc.ui_theme import Tooltip, apply_theme, enable_dpi_awareness, glyph, icon_font
else:
    PROJECT_ROOT = Path(__file__).resolve().parents[1]
    from .config import Profile, SettingsStore, TokenStore, data_directory, profile_store
    from .runtime import (
        HotKeyListener, SingleInstance, configure_logging, find_cloudflared,
        install_startup, port_available, write_process_record,
    )
    from .tunnel import QuickTunnel
    from .ui_theme import Tooltip, apply_theme, enable_dpi_awareness, glyph, icon_font


def format_activity(value: object, now: dt.datetime | None = None) -> str:
    """Format bridge timestamps for quick scanning in the project list."""
    if value in (None, "", "-"):
        return "-"
    current = now or dt.datetime.now().astimezone()
    try:
        text = str(value).strip().replace("Z", "+00:00")
        parsed = dt.datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return "-"
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=current.tzinfo)
    parsed = parsed.astimezone(current.tzinfo)
    seconds = max(0.0, (current - parsed).total_seconds())
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return f"{max(1, int(seconds // 60))} 分钟前"
    if parsed.date() == current.date():
        return f"今天 {parsed:%H:%M}"
    return parsed.strftime("%m-%d %H:%M")


def profile_display_state(profile: Profile, session: dict[str, object] | None) -> tuple[str, str]:
    """Map a profile/session pair to the fixed user-facing state vocabulary."""
    if not profile.enabled:
        return "已禁用", "-"
    if not session:
        return "未运行", "-"
    if bool(session.get("running")):
        state = "桌面已连接" if bool(session.get("desktopConnected")) else "运行中"
    else:
        state = "已退出"
    return state, format_activity(session.get("lastActivityAt"))


def _terminal_window_titles(profile: Profile) -> tuple[str, ...]:
    """Return the titles Windows Terminal may expose while attach starts."""
    return tuple(dict.fromkeys((profile.name, f"RelayTerm - {profile.id}", f"RelayTerm - {profile.name}")))


def _console_python_executable(executable: str | os.PathLike[str] | None = None) -> str:
    """Use the console interpreter for attach_cli when the launcher runs via pythonw."""
    current = Path(executable or sys.executable)
    if os.name == "nt" and current.name.casefold() == "pythonw.exe":
        console = current.with_name("python.exe")
        if console.is_file():
            return str(console)
    return str(current)


def _terminal_title_matches(title: str, titles: tuple[str, ...]) -> bool:
    folded = title.casefold()
    for candidate in titles:
        needle = candidate.casefold().strip()
        if not needle:
            continue
        start = 0
        while True:
            index = folded.find(needle, start)
            if index < 0:
                break
            end = index + len(needle)
            left_boundary = index == 0 or not folded[index - 1].isalnum()
            right_boundary = end == len(folded) or not folded[end].isalnum()
            if left_boundary and right_boundary:
                return True
            start = index + 1
    return False


def _select_terminal_window(
    windows: list[tuple[int, str]],
    titles: tuple[str, ...],
    previous_windows: dict[int, str] | None = None,
    foreground_hwnd: int | None = None,
    previous_foreground_hwnd: int | None = None,
    *,
    allow_z_order_fallback: bool = True,
) -> int | None:
    """Choose the Terminal window most likely affected by the latest wt command."""
    if not windows:
        return None
    previous = previous_windows or {}
    current_hwnds = {hwnd for hwnd, _title in windows}

    new_windows = [hwnd for hwnd, _title in windows if hwnd not in previous]
    if new_windows:
        return new_windows[0]
    if foreground_hwnd in current_hwnds and foreground_hwnd != previous_foreground_hwnd:
        return foreground_hwnd
    for hwnd, title in windows:
        old_title = previous.get(hwnd)
        if _terminal_title_matches(title, titles) and (
            old_title is None or not _terminal_title_matches(old_title, titles)
        ):
            return hwnd
    for hwnd, title in windows:
        if _terminal_title_matches(title, titles):
            return hwnd
    if len(windows) == 1:
        return windows[0][0]
    return windows[0][0] if allow_z_order_fallback else None


def _windows_terminal_windows() -> list[tuple[int, str]]:
    """Enumerate visible Windows Terminal windows in desktop Z order."""
    if os.name != "nt":
        return []
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    enum_proc_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [enum_proc_type, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    process_query_limited = 0x1000
    result: list[tuple[int, str]] = []

    def window_title(hwnd: int) -> str:
        length = user32.GetWindowTextLengthW(hwnd)
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, len(buffer))
        return buffer.value

    def is_windows_terminal(hwnd: int) -> bool:
        class_buffer = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, class_buffer, len(class_buffer))
        terminal_class = class_buffer.value.casefold() == "cascadia_hosting_window_class"
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        handle = kernel32.OpenProcess(process_query_limited, False, pid.value)
        if not handle:
            return terminal_class
        try:
            buffer = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(buffer))
            if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return terminal_class
            return Path(buffer.value).name.casefold() == "windowsterminal.exe"
        finally:
            kernel32.CloseHandle(handle)

    @enum_proc_type
    def visit(hwnd: int, _lparam: int) -> bool:
        if user32.IsWindowVisible(hwnd) and is_windows_terminal(hwnd):
            result.append((hwnd, window_title(hwnd)))
        return True

    user32.EnumWindows(visit, 0)
    return result


def _find_terminal_window(
    titles: tuple[str, ...],
    previous_windows: dict[int, str] | None = None,
    previous_foreground_hwnd: int | None = None,
    *,
    allow_z_order_fallback: bool = True,
) -> int | None:
    windows = _windows_terminal_windows()
    foreground = ctypes.windll.user32.GetForegroundWindow() if os.name == "nt" else None
    return _select_terminal_window(
        windows, titles, previous_windows, foreground, previous_foreground_hwnd,
        allow_z_order_fallback=allow_z_order_fallback,
    )


def _focus_window(hwnd: int) -> bool:
    """Restore and foreground a window even when Tk was launched hidden."""
    if os.name != "nt" or not hwnd:
        return False
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.AttachThreadInput.restype = wintypes.BOOL
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.BringWindowToTop.restype = wintypes.BOOL
    user32.SetWindowPos.argtypes = [
        wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT,
    ]
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.AllowSetForegroundWindow.argtypes = [wintypes.DWORD]
    user32.AllowSetForegroundWindow.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    foreground = user32.GetForegroundWindow()
    ignored = wintypes.DWORD()
    target_thread = user32.GetWindowThreadProcessId(hwnd, ctypes.byref(ignored))
    foreground_thread = user32.GetWindowThreadProcessId(foreground, ctypes.byref(ignored)) if foreground else 0
    current_thread = kernel32.GetCurrentThreadId()
    attached: list[int] = []
    for thread_id in (foreground_thread, target_thread):
        if thread_id and thread_id != current_thread and thread_id not in attached:
            if user32.AttachThreadInput(current_thread, thread_id, True):
                attached.append(thread_id)
    try:
        user32.ShowWindow(hwnd, 9 if user32.IsIconic(hwnd) else 5)
        user32.SetWindowPos(hwnd, wintypes.HWND(-1), 0, 0, 0, 0, 0x0043)
        user32.BringWindowToTop(hwnd)
        user32.AllowSetForegroundWindow(0xFFFFFFFF)
        user32.SetForegroundWindow(hwnd)
        # SetWindowPos can succeed while Windows still rejects foreground
        # activation. Report success only after the foreground HWND confirms it
        # so the caller keeps retrying instead of hiding the launcher early.
        return int(user32.GetForegroundWindow() or 0) == int(hwnd)
    finally:
        for thread_id in reversed(attached):
            user32.AttachThreadInput(current_thread, thread_id, False)


def _release_window_topmost(hwnd: int) -> None:
    if os.name != "nt" or not hwnd:
        return
    user32 = ctypes.windll.user32
    user32.SetWindowPos.argtypes = [
        wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT,
    ]
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.SetWindowPos(hwnd, wintypes.HWND(-2), 0, 0, 0, 0, 0x0043)


def button_availability(
    profile: Profile | None, session: dict[str, object] | None, bridge_ready: bool,
) -> dict[str, bool]:
    """Return button enablement without requiring a Tk display."""
    selected = profile is not None
    return {
        "edit": selected,
        "delete": selected,
        "stop": bool(selected and session and session.get("running")),
        "open": bool(selected and profile.enabled and bridge_ready),
    }


class Agent:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.withdraw()
        self.root.title("RelayTerm 启动器")
        self.settings_store = SettingsStore()
        self.settings = self.settings_store.load()
        self.settings_store.save(self.settings)
        self.token = TokenStore().load_or_create()
        self.store = profile_store(str(PROJECT_ROOT))
        self.profiles = self.store.load()
        self.logger = configure_logging(self.token)
        self.host = str(self.settings["host"])
        self.port = int(self.settings["port"])
        self.bridge: subprocess.Popen[str] | None = None
        self.tunnel: QuickTunnel | None = None
        self.bridge_state = "starting"
        self.port_notice = ""
        self.tunnel_state = "stopped"
        self.tunnel_url = ""
        self.pairing_url = ""
        self.qr_path: Path | None = None
        self._status_fetching = False
        self.sessions: dict[str, dict[str, object]] = {}
        self.panel: tk.Toplevel | None = None
        self.search_var = tk.StringVar()
        self.search_entry: ttk.Entry | None = None
        self.search_placeholder_active = False
        self.status_var = tk.StringVar(value="Bridge 正在启动")
        self.endpoint_var = tk.StringVar(value=f"{self.host}:{self.port}")
        self.tunnel_status_var = tk.StringVar(value="隧道未启动")
        self.remote_var = tk.StringVar(value=f"{glyph('remote')}  远程访问")
        self.tree: ttk.Treeview | None = None
        self.detail_frame: ttk.Frame | None = None
        self.detail_profile_id = ""
        self.edit_button: ttk.Button | None = None
        self.delete_button: ttk.Button | None = None
        self.stop_button: ttk.Button | None = None
        self.open_button: ttk.Button | None = None
        self.icon_font = icon_font(root)
        try:
            style = ttk.Style(root)
            style.configure("Icon.TButton", font=self.icon_font)
            style.configure("Remote.TButton", font=self.icon_font)
            style.configure("Accent.TButton", font=(self.icon_font[0], self.icon_font[1], "bold"))
        except tk.TclError:
            pass
        self.hotkey = HotKeyListener(lambda: self.root.after(0, self.show_panel))
        self.hotkey.start()
        if bool(self.settings.get("autoStart", True)) and not os.environ.get("RELAYTERM_NO_STARTUP"):
            try:
                install_startup(PROJECT_ROOT)
            except Exception as exc:
                self.logger.error("startup registration failed: %s", exc)
        self.start_bridge()
        self.root.after(700, self.poll_status)

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def api(self, path: str, method: str = "GET", body: dict[str, object] | None = None,
            authenticated: bool = True, timeout: float = 4) -> tuple[int, dict[str, object]]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(self.base_url + path, data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        if authenticated:
            request.add_header("Authorization", "Bearer " + self.token)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            try:
                value = json.loads(exc.read())
            except Exception:
                value = {"error": str(exc)}
            return exc.code, value

    def start_bridge(self) -> None:
        if not port_available(self.host, self.port):
            try:
                status, _ = self.api("/v1/profiles")
                if status == 200:
                    self.bridge_state = "ready"
                    self.status_var.set("Bridge 已连接")
                    self.endpoint_var.set(self._endpoint_text())
                    self._set_button_states()
                    return
            except Exception:
                pass
            original_port = self.port
            for candidate in range(original_port + 1, min(original_port + 101, 65536)):
                if port_available(self.host, candidate):
                    self.port = candidate
                    self.settings["port"] = candidate
                    self.settings_store.save(self.settings)
                    self.port_notice = f"端口 {original_port} 忙，使用备用端口 {candidate}"
                    self.endpoint_var.set(self._endpoint_text())
                    self.status_var.set(self.port_notice)
                    break
            else:
                self.bridge_state = "conflict"
                self.status_var.set(f"端口 {original_port} 被其他进程占用")
                self._set_button_states()
                return
        environment = os.environ.copy()
        environment.update({
            "RELAYTERM_HOST": self.host,
            "RELAYTERM_PORT": str(self.port),
            "RELAYTERM_TOKEN": self.token,
            "RELAYTERM_PROFILE_PATH": str(self.store.path),
            "RELAYTERM_INITIAL_CWD": str(PROJECT_ROOT),
            "PYTHONPATH": str(PROJECT_ROOT) + os.pathsep + environment.get("PYTHONPATH", ""),
        })
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.bridge = subprocess.Popen(
            [sys.executable, "-m", "bridge.relay_bridge"], cwd=PROJECT_ROOT, env=environment,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            errors="replace", creationflags=flags,
        )
        write_process_record(os.getpid(), self.bridge.pid)
        threading.Thread(target=self._read_bridge_log, name="relayterm-bridge-log", daemon=True).start()
        threading.Thread(target=self._wait_for_bridge, name="relayterm-bridge-health", daemon=True).start()

    def _read_bridge_log(self) -> None:
        process = self.bridge
        if process is None or process.stdout is None:
            return
        for line in process.stdout:
            self.logger.info("bridge: %s", line.rstrip())
        code = process.wait()
        if self.bridge is process:
            self.root.after(0, lambda: self._set_bridge_error(f"Bridge 已退出 ({code})"))

    def _wait_for_bridge(self) -> None:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            try:
                status, value = self.api("/health", authenticated=False, timeout=1)
                if status == 200 and value.get("ok") is True and value.get("service") == "relayterm":
                    self.root.after(0, self._bridge_ready)
                    return
            except Exception:
                time.sleep(0.15)
        self.root.after(0, lambda: self._set_bridge_error("Bridge 健康检查超时"))

    def _bridge_ready(self) -> None:
        self.bridge_state = "ready"
        self.status_var.set("Bridge 就绪 · Ctrl+Alt+Shift+R")
        self.endpoint_var.set(self._endpoint_text())
        self._set_button_states()
        self.refresh_tree()

    def _set_bridge_error(self, message: str) -> None:
        self.bridge_state = "error"
        self.status_var.set(message)
        self._set_button_states()
        self.refresh_tree()

    def _endpoint_text(self) -> str:
        suffix = " · 备用端口" if self.port_notice else ""
        return f"{self.host}:{self.port}{suffix}"

    def _show_transient_status(self, message: str) -> None:
        self.status_var.set(message)

        def restore() -> None:
            if self.status_var.get() == message and self.bridge_state == "ready":
                self.status_var.set("Bridge 就绪 · Ctrl+Alt+Shift+R")

        self.root.after(2500, restore)

    def build_panel(self) -> None:
        try:
            style = ttk.Style(self.root)
            style.configure("Icon.TButton", font=self.icon_font)
            style.configure("Remote.TButton", font=self.icon_font)
            style.configure("Accent.TButton", font=(self.icon_font[0], self.icon_font[1], "bold"))
        except tk.TclError:
            pass
        panel = tk.Toplevel(self.root)
        self.panel = panel
        panel.title("RelayTerm 项目")
        panel.geometry("840x540")
        panel.minsize(720, 480)
        panel.protocol("WM_DELETE_WINDOW", self.hide_panel)
        panel.bind("<Escape>", lambda _event: self.hide_panel())
        panel.bind("<Return>", self._start_selected_event)
        panel.bind("<Control-n>", lambda _event: self.edit_profile(None))
        panel.bind("<Control-e>", lambda _event: self.edit_selected())

        body = ttk.Frame(panel, padding=(16, 12, 16, 8))
        body.grid(row=0, column=0, sticky="nsew")
        panel.rowconfigure(0, weight=1)
        panel.columnconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(1, weight=1)

        toolbar = ttk.Frame(body)
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        toolbar.columnconfigure(0, weight=1)
        search = ttk.Entry(toolbar, textvariable=self.search_var, style="Search.TEntry")
        self.search_entry = search
        search.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        search.bind("<FocusIn>", self._search_focus_in)
        search.bind("<FocusOut>", self._search_focus_out)
        search.bind("<KeyRelease>", self._search_changed)
        search.bind("<Down>", self._focus_tree)
        search.bind("<Return>", self._start_selected_event)

        def icon_button(name: str, command, tip: str) -> ttk.Button:
            button = ttk.Button(toolbar, text=glyph(name), style="Icon.TButton", command=command, width=3)
            Tooltip(button, tip)
            return button

        add_button = icon_button("add", lambda: self.edit_profile(None), "新增项目")
        add_button.grid(row=0, column=1, padx=2)
        self.edit_button = icon_button("edit", self.edit_selected, "编辑项目")
        self.edit_button.grid(row=0, column=2, padx=2)
        self.delete_button = icon_button("delete", self.delete_selected, "删除项目")
        self.delete_button.grid(row=0, column=3, padx=2)
        self.stop_button = icon_button("stop", self.stop_selected, "停止会话")
        self.stop_button.grid(row=0, column=4, padx=2)
        remote = ttk.Button(toolbar, textvariable=self.remote_var, style="Remote.TButton", command=self.remote_access)
        remote.grid(row=0, column=5, padx=(8, 2))
        self.open_button = ttk.Button(
            toolbar, text=f"{glyph('open')}  打开", style="Accent.TButton", command=self.start_selected,
        )
        self.open_button.grid(row=0, column=6, padx=(2, 0))

        tree_frame = ttk.Frame(body)
        tree_frame.grid(row=1, column=0, sticky="nsew")
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)
        columns = ("name", "state", "activity")
        tree = ttk.Treeview(tree_frame, columns=columns, show="headings", selectmode="browse")
        self.tree = tree
        tree.heading("name", text="项目名称", anchor="w")
        tree.heading("state", text="状态")
        tree.heading("activity", text="最近活动")
        tree.column("name", width=440, minwidth=220, anchor="w", stretch=True)
        tree.column("state", width=116, minwidth=108, anchor="center", stretch=False)
        tree.column("activity", width=138, minwidth=126, anchor="center", stretch=False)
        scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        tree.bind("<<TreeviewSelect>>", self._selection_changed)
        tree.bind("<Return>", self._start_selected_event)
        tree.bind("<Double-1>", lambda _event: self.start_selected())

        ttk.Separator(body, orient="horizontal").grid(row=2, column=0, sticky="ew", pady=(10, 0))
        self.detail_frame = ttk.Frame(body, padding=(0, 9, 0, 2))
        self.detail_frame.grid(row=3, column=0, sticky="ew")
        self.detail_frame.columnconfigure(1, weight=1)
        self.detail_frame.grid_remove()
        self._build_detail(self.detail_frame)

        footer = ttk.Frame(panel, padding=(16, 4, 16, 12))
        footer.grid(row=1, column=0, sticky="ew")
        footer.columnconfigure(0, weight=1)
        ttk.Label(footer, textvariable=self.status_var, style="Status.TLabel", anchor="w").grid(
            row=0, column=0, sticky="ew",
        )
        ttk.Label(footer, textvariable=self.endpoint_var, style="Status.TLabel", anchor="center").grid(
            row=0, column=1, padx=(10, 0),
        )
        ttk.Label(footer, textvariable=self.tunnel_status_var, style="Status.TLabel", anchor="e").grid(
            row=0, column=2, padx=(10, 0),
        )
        self._set_button_states()
        self._set_search_placeholder()

    def _build_detail(self, frame: ttk.Frame) -> None:
        self.detail_vars: dict[str, tk.StringVar] = {
            key: tk.StringVar(value="-")
            for key in ("directory", "command", "shell", "enabled", "order", "pid", "cwd", "controller", "connections", "state")
        }
        ttk.Label(frame, text="选中项目详情", style="Section.TLabel").grid(
            row=0, column=0, columnspan=4, sticky="w", pady=(0, 5),
        )
        ttk.Label(frame, text="目录", style="Muted.TLabel").grid(row=1, column=0, sticky="nw", padx=(0, 10))
        self.detail_directory = ttk.Entry(frame, textvariable=self.detail_vars["directory"], state="readonly")
        self.detail_directory.grid(row=1, column=1, columnspan=3, sticky="ew")
        ttk.Label(frame, text="启动命令", style="Muted.TLabel").grid(row=2, column=0, sticky="nw", padx=(0, 10), pady=(4, 0))
        self.detail_command = ttk.Entry(frame, textvariable=self.detail_vars["command"], state="readonly")
        self.detail_command.grid(row=2, column=1, columnspan=3, sticky="ew", pady=(4, 0))
        meta = ttk.Frame(frame)
        meta.grid(row=3, column=0, columnspan=4, sticky="ew", pady=(7, 0))
        for column in (1, 3, 5):
            meta.columnconfigure(column, weight=1)
        for column, (label, key) in enumerate((("Shell", "shell"), ("启用状态", "enabled"), ("排序", "order"))):
            base = column * 2
            ttk.Label(meta, text=label, style="Muted.TLabel").grid(row=0, column=base, sticky="w", padx=(0, 6))
            ttk.Label(meta, textvariable=self.detail_vars[key]).grid(row=0, column=base + 1, sticky="w", padx=(0, 18))
        ttk.Label(frame, text="会话", style="Muted.TLabel").grid(row=4, column=0, sticky="nw", pady=(7, 0), padx=(0, 10))
        session_line = ttk.Frame(frame)
        session_line.grid(row=4, column=1, columnspan=3, sticky="ew", pady=(7, 0))
        for column in (1, 3, 5, 7):
            session_line.columnconfigure(column, weight=1)
        for column, (label, key) in enumerate((("状态", "state"), ("PID", "pid"), ("控制端", "controller"), ("连接数", "connections"))):
            base = column * 2
            ttk.Label(session_line, text=label, style="Muted.TLabel").grid(row=0, column=base, sticky="w", padx=(0, 6))
            ttk.Label(session_line, textvariable=self.detail_vars[key]).grid(row=0, column=base + 1, sticky="w", padx=(0, 14))
        ttk.Label(frame, text="当前 cwd", style="Muted.TLabel").grid(row=5, column=0, sticky="nw", pady=(5, 0), padx=(0, 10))
        self.detail_cwd = ttk.Entry(frame, textvariable=self.detail_vars["cwd"], state="readonly")
        self.detail_cwd.grid(row=5, column=1, columnspan=3, sticky="ew", pady=(5, 0))

    def _set_search_placeholder(self) -> None:
        if self.search_entry is None or self.search_var.get():
            return
        self.search_placeholder_active = True
        self.search_var.set("搜索名称或目录")
        try:
            self.search_entry.configure(style="Placeholder.TEntry")
        except tk.TclError:
            pass

    def _clear_search_placeholder(self) -> None:
        if self.search_placeholder_active:
            self.search_placeholder_active = False
            self.search_var.set("")
            if self.search_entry is not None:
                self.search_entry.configure(style="Search.TEntry")

    def _search_focus_in(self, _event: tk.Event | None = None) -> None:
        self._clear_search_placeholder()

    def _search_focus_out(self, _event: tk.Event | None = None) -> None:
        if not self.search_var.get().strip():
            self._set_search_placeholder()

    def _search_changed(self, _event: tk.Event | None = None) -> None:
        if self.search_placeholder_active:
            return
        self.refresh_tree()

    def _search_query(self) -> str:
        return "" if self.search_placeholder_active else self.search_var.get().strip()

    def _focus_tree(self, _event: tk.Event | None = None):
        if self.tree is not None:
            children = self.tree.get_children()
            if children:
                self.tree.focus_set()
                selected = self.selected_profile_id()
                self.tree.selection_set(selected if selected in children else children[0])
        return "break"

    def show_panel(self) -> None:
        if self.panel is None or not self.panel.winfo_exists():
            self.build_panel()
        assert self.panel is not None
        self.profiles = self.store.load()
        self.refresh_tree()
        self.panel.deiconify()
        self.panel.lift()
        self.panel.attributes("-topmost", True)
        self.panel.after(120, lambda: self.panel.attributes("-topmost", False))
        self.panel.focus_force()
        if self.search_entry is not None:
            self.search_entry.focus_set()
            if self.search_placeholder_active:
                self._clear_search_placeholder()
            else:
                self.search_entry.select_range(0, "end")

    def hide_panel(self) -> None:
        if self.panel is not None:
            if self.search_var.get().strip() == "":
                self._set_search_placeholder()
            self.panel.withdraw()

    def profile_state(self, profile: Profile) -> tuple[str, str]:
        return profile_display_state(profile, self.sessions.get(profile.id))

    def refresh_tree(self) -> None:
        if self.tree is None:
            return
        selected = self.selected_profile_id()
        self.tree.delete(*self.tree.get_children())
        query = self._search_query().casefold()
        for profile in self.profiles:
            if query and query not in (profile.name + " " + profile.working_directory).casefold():
                continue
            state, activity = self.profile_state(profile)
            self.tree.insert("", "end", iid=profile.id, values=(profile.name, state, activity))
        children = self.tree.get_children()
        if selected in children:
            self.tree.selection_set(selected)
        else:
            self.tree.selection_remove(*self.tree.selection())
        self._selection_changed()

    def _selection_changed(self, _event: tk.Event | None = None) -> None:
        profile = self.selected_profile()
        if profile is None:
            self.detail_profile_id = ""
            if self.detail_frame is not None:
                self.detail_frame.grid_remove()
            self._set_button_states()
            return
        self.detail_profile_id = profile.id
        if self.detail_frame is not None:
            self.detail_frame.grid()
        session = self.sessions.get(profile.id) or {}
        state, _ = self.profile_state(profile)
        controller = session.get("controller") if isinstance(session.get("controller"), dict) else {}
        controller_type = str(controller.get("clientType", "")) if controller else "无"
        controller_type = {"desktop": "桌面", "android": "Android"}.get(controller_type, controller_type or "无")
        self.detail_vars["directory"].set(profile.working_directory)
        self.detail_vars["command"].set(profile.startup_command or "（无）")
        self.detail_vars["shell"].set(profile.shell)
        self.detail_vars["enabled"].set("已启用" if profile.enabled else "已禁用")
        self.detail_vars["order"].set(str(profile.order))
        self.detail_vars["state"].set(state)
        self.detail_vars["pid"].set(str(session.get("pid") or "-"))
        self.detail_vars["cwd"].set(str(session.get("cwd") or profile.working_directory))
        self.detail_vars["controller"].set(controller_type)
        self.detail_vars["connections"].set(str(session.get("attachmentCount", 0)))
        self._set_button_states()

    def _set_button_states(self) -> None:
        profile = self.selected_profile() if self.tree is not None else None
        session = self.sessions.get(profile.id) if profile is not None else None
        states = button_availability(profile, session, self.bridge_state == "ready")
        for name, button in (("edit", self.edit_button), ("delete", self.delete_button), ("stop", self.stop_button), ("open", self.open_button)):
            if button is not None:
                button.configure(state="normal" if states[name] else "disabled")

    def selected_profile_id(self) -> str:
        if self.tree is None:
            return ""
        selected = self.tree.selection()
        return selected[0] if selected else ""

    def selected_profile(self) -> Profile | None:
        profile_id = self.selected_profile_id()
        return next((item for item in self.profiles if item.id == profile_id), None)

    def edit_selected(self) -> None:
        profile = self.selected_profile()
        if profile is not None:
            self.edit_profile(profile)

    def edit_profile(self, existing: Profile | None) -> None:
        dialog = tk.Toplevel(self.panel or self.root)
        dialog.title("新增项目" if existing is None else "编辑项目")
        dialog.transient(self.panel or self.root)
        dialog.grab_set()
        dialog.resizable(True, False)
        dialog.minsize(620, 0)
        values = {
            "name": tk.StringVar(value=existing.name if existing else ""),
            "directory": tk.StringVar(value=existing.working_directory if existing else str(PROJECT_ROOT)),
            "shell": tk.StringVar(value=existing.shell if existing else "pwsh"),
            "command": tk.StringVar(value=existing.startup_command if existing else ""),
            "order": tk.IntVar(value=existing.order if existing else len(self.profiles)),
            "enabled": tk.BooleanVar(value=existing.enabled if existing else True),
        }
        form = ttk.Frame(dialog, padding=(18, 16, 18, 10))
        form.grid(row=0, column=0, sticky="nsew")
        dialog.columnconfigure(0, weight=1)
        form.columnconfigure(1, weight=1)

        def row_label(row: int, text: str) -> None:
            ttk.Label(form, text=text).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=6)

        row_label(0, "名称")
        name_entry = ttk.Entry(form, textvariable=values["name"])
        name_entry.grid(row=0, column=1, sticky="ew", pady=6)
        row_label(1, "工作目录")
        directory_frame = ttk.Frame(form)
        directory_frame.grid(row=1, column=1, sticky="ew", pady=6)
        directory_frame.columnconfigure(0, weight=1)
        ttk.Entry(directory_frame, textvariable=values["directory"]).grid(row=0, column=0, sticky="ew")
        ttk.Button(directory_frame, text="浏览...", command=lambda: self.choose_directory(values["directory"], dialog)).grid(
            row=0, column=1, padx=(8, 0),
        )
        row_label(2, "启动命令")
        ttk.Entry(form, textvariable=values["command"]).grid(row=2, column=1, sticky="ew", pady=6)
        row_label(3, "Shell")
        ttk.Combobox(
            form, textvariable=values["shell"], values=("pwsh", "powershell", "cmd", "wsl"), state="readonly",
        ).grid(row=3, column=1, sticky="w", pady=6)
        row_label(4, "排序")
        ttk.Spinbox(form, from_=-1000000, to=1000000, textvariable=values["order"], width=12).grid(
            row=4, column=1, sticky="w", pady=6,
        )
        ttk.Checkbutton(form, text="启用", variable=values["enabled"]).grid(row=5, column=1, sticky="w", pady=(3, 6))
        actions = ttk.Frame(form)
        actions.grid(row=6, column=0, columnspan=2, sticky="e", pady=(10, 0))

        def save() -> None:
            try:
                profile = Profile.from_dict({
                    "id": existing.id if existing else uuid.uuid4().hex,
                    "name": values["name"].get(),
                    "workingDirectory": values["directory"].get(),
                    "shell": values["shell"].get(),
                    "startupCommand": values["command"].get(),
                    "order": values["order"].get(),
                    "enabled": values["enabled"].get(),
                })
                profiles = [item for item in self.profiles if item.id != profile.id] + [profile]
                self.profiles = self.store.save(profiles)
                dialog.destroy()
                self.refresh_tree()
                if self.tree is not None and self.tree.exists(profile.id):
                    self.tree.selection_set(profile.id)
            except Exception as exc:
                messagebox.showerror("项目配置", str(exc), parent=dialog)

        ttk.Button(actions, text="取消", command=dialog.destroy).pack(side="left", padx=(0, 8))
        ttk.Button(actions, text="保存", style="Accent.TButton", command=save).pack(side="left")
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        dialog.bind("<Return>", lambda _event: save())
        dialog.wait_visibility()
        name_entry.focus_set()
        name_entry.select_range(0, "end")

    def choose_directory(self, variable: tk.StringVar, parent: tk.Misc | None = None) -> None:
        selected = filedialog.askdirectory(
            initialdir=variable.get() or str(PROJECT_ROOT), parent=parent or self.panel,
        )
        if selected:
            variable.set(selected)

    def delete_selected(self) -> None:
        profile = self.selected_profile()
        if profile is None:
            return
        if not messagebox.askyesno("删除项目", f"从 RelayTerm 删除“{profile.name}”？", parent=self.panel):
            return
        was_running = bool((self.sessions.get(profile.id) or {}).get("running"))
        self.profiles = self.store.save(item for item in self.profiles if item.id != profile.id)
        self.refresh_tree()
        if was_running:

            def terminate_removed() -> None:
                try:
                    self.api(
                        f"/v1/sessions/{quote(profile.id, safe='')}/terminate",
                        "POST",
                        {"force": True},
                    )
                except Exception as exc:
                    self.logger.warning("failed to terminate deleted profile %s: %s", profile.id, exc)

            threading.Thread(target=terminate_removed, daemon=True).start()

    def _activate_terminal_window(
        self, profile: Profile, previous_windows: dict[int, str] | None = None,
        previous_foreground_hwnd: int | None = None, attempt: int = 0,
    ) -> None:
        hwnd = _find_terminal_window(
            _terminal_window_titles(profile), previous_windows, previous_foreground_hwnd,
            allow_z_order_fallback=attempt >= 4,
        )
        if hwnd is not None and _focus_window(hwnd):
            self.hide_panel()
            self.root.after(2000, lambda: _release_window_topmost(hwnd))
            return
        if hwnd is not None:
            _release_window_topmost(hwnd)
        if attempt < 40:
            self.root.after(
                250, lambda: self._activate_terminal_window(
                    profile, previous_windows, previous_foreground_hwnd, attempt + 1,
                ),
            )
        else:
            self.status_var.set("Windows Terminal 已启动，但未能置前")
            if hasattr(self, "logger"):
                self.logger.warning("Windows Terminal window not focused for profile %s", profile.id)

    def _start_selected_event(self, _event: tk.Event | None = None) -> str:
        self.start_selected()
        return "break"

    def start_selected(self) -> None:
        profile = self.selected_profile()
        if profile is None or not profile.enabled:
            return
        if self.bridge_state != "ready":
            messagebox.showerror("RelayTerm", self.status_var.get(), parent=self.panel)
            return
        session = self.sessions.get(profile.id, {})
        fresh = session.get("state") == "exited"
        executable = _console_python_executable()
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(PROJECT_ROOT) + os.pathsep + environment.get("PYTHONPATH", "")
        wt = os.environ.get("RELAYTERM_WT", "wt.exe")
        previous_windows = dict(_windows_terminal_windows())
        previous_foreground_hwnd = (
            ctypes.windll.user32.GetForegroundWindow() if os.name == "nt" else None
        )
        command = [
            wt, "-w", "0", "new-tab", "--title", profile.name,
            "--suppressApplicationTitle",
            "--startingDirectory", profile.working_directory,
            executable, "-m", "pc.attach_cli", "--profile-id", profile.id,
        ]
        if fresh:
            command.append("--fresh")
        try:
            subprocess.Popen(command, cwd=PROJECT_ROOT, env=environment)
            self.root.after(200, lambda: self._activate_terminal_window(
                profile, previous_windows, previous_foreground_hwnd,
            ))
        except OSError as exc:
            messagebox.showerror("Windows Terminal", str(exc), parent=self.panel)

    def stop_selected(self) -> None:
        profile = self.selected_profile()
        if profile is None:
            return

        def stop() -> None:
            try:
                status, value = self.api(
                    f"/v1/sessions/{quote(profile.id, safe='')}/terminate", "POST", {"force": True}
                )
                message = "项目已停止" if status == 200 else str(value.get("error", "项目未运行"))
            except Exception as exc:
                message = str(exc)
            self.root.after(0, lambda: self._show_transient_status(message))

        threading.Thread(target=stop, daemon=True).start()

    def poll_status(self) -> None:
        if self.bridge_state == "ready" and not self._status_fetching:
            self._status_fetching = True
            threading.Thread(target=self._fetch_status, daemon=True).start()
        self.root.after(2500, self.poll_status)

    def _fetch_status(self) -> None:
        sessions: dict[str, dict[str, object]] | None = None
        try:
            status, value = self.api("/v1/sessions")
            if status == 200:
                values = value.get("sessions", [])
                sessions = {
                    str(item.get("profileId")): item for item in values
                    if isinstance(item, dict) and item.get("profileId")
                }
        except Exception:
            pass

        def complete() -> None:
            self._status_fetching = False
            if sessions is not None:
                self.sessions = sessions
                self.refresh_tree()

        self.root.after(0, complete)

    def remote_access(self) -> None:
        if self.tunnel is not None and self.tunnel.running:
            if self.pairing_url:
                self.show_pairing()
            else:
                self.stop_tunnel()
            return
        executable = find_cloudflared()
        if executable is None:
            messagebox.showerror("Quick Tunnel", "未找到 cloudflared.exe", parent=self.panel)
            return
        tunnel = QuickTunnel(executable, self.base_url)
        tunnel.callback = lambda state, detail, current=tunnel: self._tunnel_callback(
            current, state, detail,
        )
        self.tunnel = tunnel
        try:
            tunnel.start()
            write_process_record(os.getpid(), self.bridge.pid if self.bridge else 0,
                                 tunnel.process.pid if tunnel.process else 0)
        except Exception as exc:
            if self.tunnel is tunnel:
                self.tunnel = None
            messagebox.showerror("Quick Tunnel", str(exc), parent=self.panel)

    def _tunnel_callback(self, tunnel: QuickTunnel, state: str, detail: str) -> None:
        self.root.after(0, lambda: self._apply_tunnel_state(tunnel, state, detail))

    def _apply_tunnel_state(self, tunnel: QuickTunnel, state: str, detail: str) -> None:
        if tunnel is not self.tunnel:
            return
        self.tunnel_state = state
        if state == "starting":
            self.remote_var.set(f"{glyph('remote')}  隧道启动中")
            self.tunnel_status_var.set("隧道启动中")
        elif state == "ready":
            self.tunnel_url = detail
            self.remote_var.set(f"{glyph('remote')}  配对")
            self.tunnel_status_var.set("隧道已连接")
            threading.Thread(target=self._create_pairing, args=(tunnel, detail), daemon=True).start()
        elif state == "error":
            self.remote_var.set(f"{glyph('remote')}  远程访问")
            self.tunnel_status_var.set(f"隧道异常 ({detail})")
            write_process_record(os.getpid(), self.bridge.pid if self.bridge else 0, 0)
        else:
            self.remote_var.set(f"{glyph('remote')}  远程访问")
            self.tunnel_status_var.set("隧道未启动")
            self.tunnel_url = self.pairing_url = ""
            write_process_record(os.getpid(), self.bridge.pid if self.bridge else 0, 0)

    def _create_pairing(self, tunnel: QuickTunnel, tunnel_url: str) -> None:
        try:
            status, value = self.api("/v1/pairing/challenges", "POST", {"endpoint": tunnel_url})
            if status != 201:
                raise RuntimeError(str(value.get("error", "pairing_failed")))
            challenge = str(value["challenge"])
            pairing_url = tunnel_url + "/pair/" + challenge
            import qrcode
            image = qrcode.make(pairing_url)
            path = data_directory() / f"pairing-{uuid.uuid4().hex}.png"
            image.save(path)

            def complete() -> None:
                if tunnel is not self.tunnel or self.tunnel_url != tunnel_url:
                    try:
                        path.unlink()
                    except OSError:
                        pass
                    return
                previous = self.qr_path
                self.pairing_url, self.qr_path = pairing_url, path
                if previous is not None and previous != path:
                    try:
                        previous.unlink()
                    except OSError:
                        pass
                self.show_pairing()

            self.root.after(0, complete)
        except Exception as exc:
            message = f"配对码生成失败: {exc}"
            self.root.after(0, lambda: (
                self.tunnel_status_var.set(message) if tunnel is self.tunnel else None
            ))

    def show_pairing(self) -> None:
        if not self.pairing_url:
            return
        dialog = tk.Toplevel(self.panel or self.root)
        dialog.title("RelayTerm 远程配对")
        dialog.transient(self.panel or self.root)
        dialog.resizable(False, False)
        frame = ttk.Frame(dialog, padding=(18, 16, 18, 14))
        frame.grid(row=0, column=0, sticky="nsew")
        dialog.columnconfigure(0, weight=1)
        ttk.Label(frame, text="Quick Tunnel", style="Section.TLabel").pack(anchor="w")
        ttk.Label(frame, text=self.tunnel_url, wraplength=520, justify="left").pack(anchor="w", pady=(5, 10))
        qr_frame = tk.Frame(frame, width=260, height=260, bg="#ffffff", highlightthickness=1, highlightbackground="#d0d0d0")
        qr_frame.pack(anchor="center", pady=(0, 4))
        qr_frame.pack_propagate(False)
        if self.qr_path and self.qr_path.exists():
            try:
                from PIL import Image, ImageTk
                bitmap = Image.open(self.qr_path).convert("RGB").resize((260, 260), Image.Resampling.NEAREST)
                photo = ImageTk.PhotoImage(bitmap)
                label = tk.Label(qr_frame, image=photo, bg="#ffffff", borderwidth=0)
                label.image = photo
                label.place(relx=0.5, rely=0.5, anchor="center", width=260, height=260)
            except Exception:
                pass
        actions = ttk.Frame(frame)
        actions.pack(fill="x", pady=(10, 0))
        actions.columnconfigure(0, weight=1)
        copy_actions = ttk.Frame(actions)
        copy_actions.grid(row=0, column=0, sticky="w")
        ttk.Button(copy_actions, text="复制隧道地址", command=lambda: self.copy_text(self.tunnel_url)).pack(side="left")
        ttk.Button(copy_actions, text="复制配对页", command=lambda: self.copy_text(self.pairing_url)).pack(side="left", padx=(8, 0))
        ttk.Separator(actions, orient="vertical").grid(row=0, column=1, sticky="ns", padx=12)
        ttk.Button(actions, text="停止隧道", style="Danger.TButton", command=lambda: (dialog.destroy(), self.stop_tunnel())).grid(
            row=0, column=2, sticky="e",
        )

    def copy_text(self, value: str) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(value)
        self.root.update_idletasks()

    def stop_tunnel(self) -> None:
        tunnel, self.tunnel = self.tunnel, None
        if tunnel is not None:
            tunnel.stop()
        self.tunnel_url = self.pairing_url = ""
        self.remote_var.set(f"{glyph('remote')}  远程访问")
        self.tunnel_status_var.set("隧道未启动")
        write_process_record(os.getpid(), self.bridge.pid if self.bridge else 0, 0)

    def close(self) -> None:
        self.hotkey.stop()
        tunnel, self.tunnel = self.tunnel, None
        if tunnel is not None:
            tunnel.stop()
        if self.bridge is not None and self.bridge.poll() is None:
            self.bridge.terminate()
            try:
                self.bridge.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.bridge.kill()
        write_process_record(0, 0, 0)
        self.root.destroy()


def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--startup", action="store_true")
    parser.parse_known_args()
    instance = SingleInstance()
    if not instance.acquired:
        instance.close()
        return
    enable_dpi_awareness()
    root = tk.Tk()
    apply_theme(root)
    agent = Agent(root)
    try:
        root.mainloop()
    finally:
        try:
            agent.close()
        except Exception:
            pass
        instance.close()


if __name__ == "__main__":
    main()
