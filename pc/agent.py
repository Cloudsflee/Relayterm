"""Hidden Windows PC launcher and Ctrl+Alt+Shift+R project launcher."""

from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import json
import os
import shlex
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from ctypes import wintypes
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from urllib.parse import quote, urlsplit

DEFAULT_TERMINAL_COLUMNS = 140
DEFAULT_TERMINAL_ROWS = 42

if __package__ in (None, ""):
    PROJECT_ROOT = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(PROJECT_ROOT))
    from pc.config import (
        Profile,
        ProfileStore,
        SettingsStore,
        TokenStore,
        data_directory,
        move_profile_up,
        parse_utc_timestamp,
        pin_profile,
        sort_profiles_by_recent,
        unpin_profile,
    )
    from pc.runtime import (
        HotKeyListener,
        SingleInstance,
        configure_logging,
        find_cloudflared,
        install_startup,
        port_available,
        write_process_record,
    )
    from pc.tunnel import QuickTunnel
    from pc.ui_theme import Tooltip, apply_theme, enable_dpi_awareness, glyph, icon_font
    from pc.drain_switch import (
        BRIDGE_GENERATION,
        DrainStateStore,
        SHADOW_PROFILE_NAME,
        find_available_port,
        merge_session_snapshots,
        port_is_free,
        prepare_shadow_catalog,
        probe_bridge,
        process_parent_id,
        terminate_drained_bridge,
        terminate_legacy_agent,
    )
else:
    PROJECT_ROOT = Path(__file__).resolve().parents[1]
    from .config import (
        Profile,
        ProfileStore,
        SettingsStore,
        TokenStore,
        data_directory,
        move_profile_up,
        parse_utc_timestamp,
        pin_profile,
        sort_profiles_by_recent,
        unpin_profile,
    )
    from .runtime import (
        HotKeyListener,
        SingleInstance,
        configure_logging,
        find_cloudflared,
        install_startup,
        port_available,
        write_process_record,
    )
    from .tunnel import QuickTunnel
    from .ui_theme import Tooltip, apply_theme, enable_dpi_awareness, glyph, icon_font
    from .drain_switch import (
        BRIDGE_GENERATION,
        DrainStateStore,
        SHADOW_PROFILE_NAME,
        find_available_port,
        merge_session_snapshots,
        port_is_free,
        prepare_shadow_catalog,
        probe_bridge,
        process_parent_id,
        terminate_drained_bridge,
        terminate_legacy_agent,
    )


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


def codex_binding_text(value: dict[str, object] | None) -> str:
    if not isinstance(value, dict):
        return "未读取"
    binding = value.get("binding") if isinstance(value.get("binding"), dict) else {}
    mode = str(binding.get("mode", value.get("mode", "auto")))
    if mode == "auto":
        selected = str(value.get("selectedThreadId") or "")
        return "自动选择" + (f" · {selected[:8]}" if selected else "")
    thread_id = str(binding.get("threadId") or "")
    status = str(binding.get("status", "valid"))
    labels = {"valid": "已锁定", "archived": "锁定已归档", "missing": "锁定已失效"}
    return f"{labels.get(status, '已锁定')} · {thread_id[:8]}"


def profile_display_state(
    profile: Profile,
    session: dict[str, object] | None,
    last_opened_at: object | None = None,
) -> tuple[str, str]:
    """Map a profile/session pair to the fixed user-facing state vocabulary."""
    activity = last_opened_at
    if activity in (None, "", "-") and session:
        activity = session.get("lastOpenedAt")
    if not profile.enabled:
        return "已禁用", format_activity(activity)
    if not session:
        return "未运行", format_activity(activity)
    if bool(session.get("draining")) and bool(session.get("running")):
        return "旧版排空中", format_activity(activity)
    if str(session.get("state", "")).lower() == "exited" or session.get("ended") is True:
        state = "已退出"
    elif bool(session.get("running")):
        if bool(session.get("desktopConnected")):
            state = "桌面已连接"
        elif str(session.get("desktopState", "")).lower() == "closed":
            state = "终端已关闭"
        else:
            state = "运行中"
    else:
        state = "已退出"
    return state, format_activity(activity)


def sort_profiles_recent(profiles: list[Profile], recent_activity: dict[str, object] | None = None) -> list[Profile]:
    """PC-facing alias for the shared stable recent-open ordering."""
    return sort_profiles_by_recent(profiles, recent_activity)


def profile_menu_availability(profiles: list[Profile], profile_id: str) -> dict[str, bool]:
    """Return context-menu states without requiring a Tk display."""
    target = next((item for item in profiles if item.id == profile_id), None)
    first_pinned = next((item for item in profiles if item.pinned), None)
    return {
        "move_up": bool(target and (
            not target.pinned or first_pinned is None or target.id != first_pinned.id
        )),
        "pin": target is not None,
        "unpin": bool(target and target.pinned),
    }


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
    def __init__(
        self, root: tk.Tk, *, sidecar: bool = False,
        promote_callback: Callable[[], bool] | None = None,
    ) -> None:
        self.root = root
        self.sidecar = bool(sidecar)
        self.promote_callback = promote_callback
        self._promotion_pending = False
        self._promote_after_start = False
        self.root.withdraw()
        self.root.title("RelayTerm 启动器")
        self.settings_store = SettingsStore()
        self.settings = self.settings_store.load()
        self.token = TokenStore().load_or_create()
        self.logger = configure_logging(self.token)
        self.host = str(self.settings["host"])
        self.port = int(self.settings["port"])
        self.port_notice = ""
        self.standard_profile_path = data_directory() / "profiles.json"
        self.drain_store = DrainStateStore()
        self.drain_state = self.drain_store.load()
        self.drain_bridges: list[dict[str, object]] = []
        self._initial_drain_snapshots: list[tuple[str, list[dict[str, object]]]] = []
        profile_path = self._prepare_bridge_topology()
        self.settings_store.save(self.settings)
        self.store = ProfileStore(profile_path, str(PROJECT_ROOT))
        loaded_profiles = self.store.load()
        self.recent_activity = self.store.recent_activity.load()
        self.catalog_profiles = loaded_profiles
        self.profiles = sort_profiles_by_recent(loaded_profiles, self.recent_activity)
        self.bridge: subprocess.Popen[str] | None = None
        self.tunnel: QuickTunnel | None = None
        self.bridge_state = "starting"
        self.tunnel_state = "stopped"
        self.tunnel_url = ""
        self.pairing_url = ""
        self.qr_path: Path | None = None
        self.pairing_dialog: tk.Toplevel | None = None
        self._pairing_fetching = False
        self._status_fetching = False
        self.sessions, self.session_routes = merge_session_snapshots(
            [], self._initial_drain_snapshots,
        )
        self.codex_details: dict[str, dict[str, object]] = {}
        self._codex_detail_fetching: set[str] = set()
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
        self.profile_menu: tk.Menu | None = None
        self.icon_font = icon_font(root)
        try:
            style = ttk.Style(root)
            style.configure("Icon.TButton", font=self.icon_font)
            style.configure("Remote.TButton", font=self.icon_font)
            style.configure("Accent.TButton", font=(self.icon_font[0], self.icon_font[1], "bold"))
        except tk.TclError:
            pass
        self.hotkey = HotKeyListener(
            lambda: self.root.after(0, self.show_panel), "D" if self.sidecar else "R",
        )
        self.hotkey.start()
        if self.sidecar and self._promote_after_start:
            self.root.after(0, self._request_sidecar_promotion)
        if bool(self.settings.get("autoStart", True)) and not os.environ.get("RELAYTERM_NO_STARTUP"):
            try:
                install_startup(PROJECT_ROOT)
            except Exception as exc:
                self.logger.error("startup registration failed: %s", exc)
        self.start_bridge()
        self.root.after(700, self.poll_status)

    def _ready_label(self) -> str:
        key = "D" if self.sidecar else "R"
        return f"Bridge 就绪 · Ctrl+Alt+Shift+{key}"

    def _request_sidecar_promotion(self) -> None:
        if not self.sidecar or self._promotion_pending:
            return
        self._promotion_pending = True

        def attempt(remaining: int = 20) -> None:
            callback = self.promote_callback
            if callback is not None and callback():
                self.hotkey.stop()
                self.hotkey = HotKeyListener(lambda: self.root.after(0, self.show_panel), "R")
                self.hotkey.start()
                self.sidecar = False
                self.settings["host"], self.settings["port"] = self.host, self.port
                self.settings_store.save(self.settings)
                self._promotion_pending = False
                self.status_var.set(self._bridge_status_text(self._ready_label()))
                return
            if remaining > 0:
                self.root.after(250, lambda: attempt(remaining - 1))
            else:
                self._promotion_pending = False

        attempt()

    def _prepare_bridge_topology(self) -> Path:
        primary = self.drain_state.get("primary")
        if isinstance(primary, dict):
            self.host = str(primary.get("host", self.host))
            self.port = int(primary.get("port", self.port))
            profile_path = Path(str(primary.get("profilePath", "") or ""))
            if not profile_path.exists():
                prepare_shadow_catalog(self.standard_profile_path, profile_path)
            drains = self.drain_state.get("drains", [])
            self.drain_bridges = [dict(item) for item in drains if isinstance(item, dict)]
            if not self.drain_bridges:
                self.settings["host"], self.settings["port"] = self.host, self.port
            changed = False
            for item in self.drain_bridges:
                probe = probe_bridge(
                    str(item.get("host", self.host)), int(item.get("port", 0)), self.token,
                )
                if probe is None:
                    continue
                item["pid"] = probe.pid or int(item.get("pid", 0) or 0)
                profile_ids = [
                    str(session.get("profileId")) for session in probe.running_sessions
                    if session.get("profileId")
                ]
                if profile_ids != item.get("profileIds"):
                    item["profileIds"] = profile_ids
                    item["emptyPolls"] = 0 if profile_ids else int(item.get("emptyPolls", 0) or 0)
                    changed = True
                self._initial_drain_snapshots.append(
                    (probe.base_url, [dict(session) for session in probe.running_sessions]),
                )
            if changed:
                self._save_drain_state()
            self.port_notice = self._drain_notice()
            return profile_path

        configured_probe = None
        if not port_available(self.host, self.port):
            configured_probe = probe_bridge(self.host, self.port, self.token)
        if configured_probe is None or configured_probe.generation == BRIDGE_GENERATION:
            return self.standard_profile_path

        running = [dict(item) for item in configured_probe.running_sessions]
        if not running:
            legacy_agent_pid = process_parent_id(configured_probe.pid)
            if terminate_drained_bridge(
                configured_probe.host, configured_probe.port, configured_probe.pid,
            ):
                if legacy_agent_pid:
                    terminate_legacy_agent(legacy_agent_pid)
                self._promote_after_start = self.sidecar
                return self.standard_profile_path

        shadow_path = data_directory() / SHADOW_PROFILE_NAME
        prepare_shadow_catalog(
            self.standard_profile_path, shadow_path, configured_probe.catalog,
        )
        next_port = find_available_port(self.host, configured_probe.port + 1, span=1000)
        if next_port is None:
            raise RuntimeError("drain_port_unavailable")
        drain = {
            "host": configured_probe.host,
            "port": configured_probe.port,
            "pid": configured_probe.pid,
            "agentPid": process_parent_id(configured_probe.pid),
            "generation": configured_probe.generation,
            "profileIds": [
                str(item.get("profileId")) for item in running if item.get("profileId")
            ],
            "startedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
            "emptyPolls": 0,
        }
        self.host, self.port = configured_probe.host, next_port
        self.drain_bridges = [drain]
        self._initial_drain_snapshots = [(configured_probe.base_url, running)]
        self.drain_state = {
            "version": 1,
            "primary": {
                "host": self.host,
                "port": self.port,
                "pid": 0,
                "generation": BRIDGE_GENERATION,
                "profilePath": str(shadow_path),
            },
            "drains": self.drain_bridges,
        }
        self._save_drain_state()
        self.port_notice = self._drain_notice()
        self.logger.info(
            "bridge drain started old=%s new=%s active_profiles=%s",
            configured_probe.port, self.port, len(running),
        )
        return shadow_path

    def _save_drain_state(self) -> None:
        primary = self.drain_state.get("primary")
        if isinstance(primary, dict):
            primary = dict(primary)
            primary.update({"host": self.host, "port": self.port})
            self.drain_state["primary"] = primary
        self.drain_state["drains"] = [dict(item) for item in self.drain_bridges]
        self.drain_state = self.drain_store.save(self.drain_state)

    def _drain_notice(self) -> str:
        if not self.drain_bridges:
            return ""
        active = sum(len(item.get("profileIds", [])) for item in self.drain_bridges)
        ports = ",".join(str(item.get("port")) for item in self.drain_bridges)
        return f"旧版 {ports} 排空中（{active} 个会话）"

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def _profile_base_url(self, profile_id: str) -> str:
        route = getattr(self, "session_routes", {}).get(str(profile_id))
        if route:
            return route
        return f"http://{getattr(self, 'host', '127.0.0.1')}:{getattr(self, 'port', 18765)}"

    def _profile_host_port(self, profile_id: str) -> tuple[str, int]:
        parsed = urlsplit(self._profile_base_url(profile_id))
        return parsed.hostname or self.host, int(parsed.port or self.port)

    def api(self, path: str, method: str = "GET", body: dict[str, object] | None = None,
            authenticated: bool = True, timeout: float = 4,
            base_url: str | None = None) -> tuple[int, dict[str, object]]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request((base_url or self.base_url) + path, data=data, method=method)
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
            existing = probe_bridge(self.host, self.port, self.token)
            if existing is not None and existing.generation == BRIDGE_GENERATION:
                try:
                    status, value = self.api("/v1/profiles")
                    if status != 200:
                        raise RuntimeError("catalog_unavailable")
                    self._merge_catalog_activity(value)
                    self.bridge_state = "ready"
                    self.status_var.set(self._bridge_status_text("Bridge 已连接"))
                    self.endpoint_var.set(self._endpoint_text())
                    self._set_button_states()
                    return
                except Exception:
                    pass
            original_port = self.port
            for candidate in range(original_port + 1, min(original_port + 101, 65536)):
                if port_available(self.host, candidate):
                    self.port = candidate
                    if not self.drain_bridges:
                        self.settings["port"] = candidate
                        self.settings_store.save(self.settings)
                    fallback = f"端口 {original_port} 忙，使用备用端口 {candidate}"
                    self.port_notice = " · ".join(item for item in (self.port_notice, fallback) if item)
                    self._save_drain_state()
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
            "RELAYTERM_DRAIN_STATE_PATH": str(self.drain_store.path),
            "RELAYTERM_INITIAL_CWD": str(PROJECT_ROOT),
            "PYTHONPATH": str(PROJECT_ROOT) + os.pathsep + environment.get("PYTHONPATH", ""),
        })
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.bridge = subprocess.Popen(
            [sys.executable, "-m", "bridge.relay_bridge"], cwd=PROJECT_ROOT, env=environment,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            errors="replace", creationflags=flags,
        )
        primary = self.drain_state.get("primary")
        if isinstance(primary, dict):
            primary["pid"] = self.bridge.pid
            primary["agentPid"] = os.getpid()
            primary["generation"] = BRIDGE_GENERATION
            self._save_drain_state()
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
        self.status_var.set(self._bridge_status_text(self._ready_label()))
        self.endpoint_var.set(self._endpoint_text())
        self._set_button_states()
        self._refresh_recent_from_catalog()
        self.refresh_tree()

    def _merge_catalog_activity(self, value: dict[str, object] | None) -> None:
        """Merge the bridge-authoritative timestamps into the local cache."""
        if not isinstance(value, dict):
            return
        items = value.get("profiles")
        if not isinstance(items, list):
            return
        activity_map = getattr(self, "recent_activity", None)
        if not isinstance(activity_map, dict):
            activity_map = {}
            self.recent_activity = activity_map
        for item in items:
            if not isinstance(item, dict):
                continue
            profile_id = str(item.get("id", "")).strip()
            if "lastOpenedAt" not in item:
                continue
            timestamp = item.get("lastOpenedAt")
            if not profile_id:
                continue
            if parse_utc_timestamp(timestamp) is None:
                if profile_id in activity_map:
                    activity_map.pop(profile_id, None)
                continue
            current = activity_map.get(profile_id)
            current_dt = parse_utc_timestamp(current)
            incoming_dt = parse_utc_timestamp(timestamp)
            if current_dt is None or (incoming_dt is not None and incoming_dt != current_dt):
                activity_map[profile_id] = str(timestamp)

    def _refresh_recent_from_catalog(self) -> None:
        """Fetch catalog activity asynchronously after bridge startup."""
        if self.bridge_state != "ready":
            return

        def fetch() -> None:
            try:
                status, value = self.api("/v1/profiles")
                if status == 200:
                    def complete() -> None:
                        self._merge_catalog_activity(value)
                        self.refresh_tree()
                    self.root.after(0, complete)
            except Exception:
                pass

        threading.Thread(target=fetch, name="relayterm-catalog-activity", daemon=True).start()

    def _set_bridge_error(self, message: str) -> None:
        self.bridge_state = "error"
        self.status_var.set(message)
        self._set_button_states()
        self.refresh_tree()

    def _endpoint_text(self) -> str:
        return f"{self.host}:{self.port}" + (f" · {self.port_notice}" if self.port_notice else "")

    def _bridge_status_text(self, base: str) -> str:
        notice = self._drain_notice()
        return base + (f" · {notice}" if notice else "")

    def _show_transient_status(self, message: str) -> None:
        self.status_var.set(message)

        def restore() -> None:
            if self.status_var.get() == message and self.bridge_state == "ready":
                self.status_var.set(self._bridge_status_text(self._ready_label()))

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
        tree.heading("activity", text="最近打开")
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
        tree.bind("<Button-3>", self._show_profile_menu)
        profile_menu = tk.Menu(panel, tearoff=False)
        profile_menu.add_command(label="上移一格", command=lambda: self._reorder_selected("move_up"))
        profile_menu.add_command(label="置顶", command=lambda: self._reorder_selected("pin"))
        profile_menu.add_command(label="取消置顶", command=lambda: self._reorder_selected("unpin"))
        profile_menu.add_separator()
        profile_menu.add_command(label="Codex 会话…", command=self.show_codex_sessions)
        self.profile_menu = profile_menu

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
            for key in (
                "directory", "command", "shell", "enabled", "pid", "cwd", "controller",
                "connections", "state", "codex",
            )
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
        for column in (1, 3):
            meta.columnconfigure(column, weight=1)
        for column, (label, key) in enumerate((("Shell", "shell"), ("启用状态", "enabled"))):
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
        ttk.Label(frame, text="Codex 会话", style="Muted.TLabel").grid(
            row=6, column=0, sticky="nw", pady=(5, 0), padx=(0, 10),
        )
        self.detail_codex = ttk.Entry(frame, textvariable=self.detail_vars["codex"], state="readonly")
        self.detail_codex.grid(row=6, column=1, columnspan=3, sticky="ew", pady=(5, 0))

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
        loaded_profiles = self.store.load()
        recent_store = getattr(self.store, "recent_activity", None)
        self.recent_activity = recent_store.load() if recent_store is not None else {}
        self.catalog_profiles = loaded_profiles
        self.profiles = sort_profiles_by_recent(loaded_profiles, self.recent_activity)
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
        activity = getattr(self, "recent_activity", {})
        return profile_display_state(profile, self.sessions.get(profile.id), activity.get(profile.id))

    def refresh_tree(self) -> None:
        if self.tree is None:
            return
        selected = self.selected_profile_id()
        # Re-sort on every catalog/status/search refresh.  Selection is tracked
        # by profile ID below, so a reorder never starts a second connection.
        catalog_profiles = getattr(self, "catalog_profiles", self.profiles)
        self.profiles = sort_profiles_by_recent(catalog_profiles, getattr(self, "recent_activity", {}))
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
        if profile.launch_mode == "codex":
            args = " ".join(profile.codex_args)
            self.detail_vars["command"].set("Codex resume" + (" · " + args if args else ""))
            self.detail_vars["codex"].set(codex_binding_text(
                getattr(self, "codex_details", {}).get(profile.id),
            ))
        else:
            self.detail_vars["command"].set(profile.startup_command or "（无）")
            self.detail_vars["codex"].set("自定义命令")
        self.detail_vars["shell"].set(profile.shell)
        self.detail_vars["enabled"].set("已启用" if profile.enabled else "已禁用")
        self.detail_vars["state"].set(state)
        self.detail_vars["pid"].set(str(session.get("pid") or "-"))
        self.detail_vars["cwd"].set(str(session.get("cwd") or profile.working_directory))
        self.detail_vars["controller"].set(controller_type)
        self.detail_vars["connections"].set(str(session.get("attachmentCount", 0)))
        self._set_button_states()
        if (
            profile.launch_mode == "codex"
            and profile.id not in getattr(self, "codex_details", {})
            and self.bridge_state == "ready"
            and hasattr(self, "token")
        ):
            self._fetch_codex_detail(profile)

    def _fetch_codex_detail(self, profile: Profile) -> None:
        fetching = getattr(self, "_codex_detail_fetching", None)
        if fetching is None:
            fetching = set()
            self._codex_detail_fetching = fetching
        if profile.id in fetching:
            return
        fetching.add(profile.id)

        def fetch() -> None:
            value: dict[str, object] | None = None
            try:
                status, response = self.api(
                    self._codex_api_path(profile, "codex-sessions"), timeout=12,
                )
                if status == 200:
                    value = response
            except Exception:
                pass

            def complete() -> None:
                self._codex_detail_fetching.discard(profile.id)
                if value is not None:
                    self.codex_details[profile.id] = value
                    if self.detail_profile_id == profile.id:
                        self._selection_changed()

            self.root.after(0, complete)

        threading.Thread(target=fetch, name="relayterm-codex-detail", daemon=True).start()

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

    def _show_profile_menu(self, event: tk.Event):
        if self.tree is None or self.profile_menu is None:
            return "break"
        profile_id = self.tree.identify_row(event.y)
        if not profile_id:
            return "break"
        self.tree.selection_set(profile_id)
        self.tree.focus(profile_id)
        self._selection_changed()
        catalog = list(getattr(self, "catalog_profiles", self.profiles))
        states = profile_menu_availability(catalog, profile_id)
        for index, key in enumerate(("move_up", "pin", "unpin")):
            self.profile_menu.entryconfigure(index, state="normal" if states[key] else "disabled")
        profile = next((item for item in catalog if item.id == profile_id), None)
        self.profile_menu.entryconfigure(
            4,
            state="normal" if profile is not None and profile.launch_mode == "codex"
            and self.bridge_state == "ready" else "disabled",
        )
        try:
            self.profile_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.profile_menu.grab_release()
        return "break"

    def _reorder_selected(self, action: str) -> None:
        profile_id = self.selected_profile_id()
        if not profile_id:
            return
        catalog = self.store.load()
        operations = {
            "move_up": move_profile_up,
            "pin": pin_profile,
            "unpin": unpin_profile,
        }
        operation = operations.get(action)
        if operation is None:
            raise ValueError("profile_reorder_action_invalid")
        self.catalog_profiles = self.store.save(operation(catalog, profile_id))
        self.profiles = sort_profiles_by_recent(
            self.catalog_profiles, getattr(self, "recent_activity", {}),
        )
        self.search_placeholder_active = False
        self.search_var.set("")
        if self.search_entry is not None:
            self.search_entry.configure(style="Search.TEntry")
        self.refresh_tree()
        if self.tree is not None and self.tree.exists(profile_id):
            self.tree.selection_set(profile_id)
            self.tree.focus(profile_id)
            self._selection_changed()

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
            "launch_mode": tk.StringVar(value=existing.launch_mode if existing else "codex"),
            "codex_args": tk.StringVar(
                value=shlex.join(existing.codex_args) if existing and existing.codex_args else "",
            ),
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
        row_label(2, "启动方式")
        mode_frame = ttk.Frame(form)
        mode_frame.grid(row=2, column=1, sticky="w", pady=6)
        command_label = ttk.Label(form, text="自定义命令")
        command_entry = ttk.Entry(form, textvariable=values["command"])
        codex_label = ttk.Label(form, text="Codex 参数")
        codex_entry = ttk.Entry(form, textvariable=values["codex_args"])

        def mode_changed() -> None:
            for widget in (command_label, command_entry, codex_label, codex_entry):
                widget.grid_remove()
            if values["launch_mode"].get() == "codex":
                codex_label.grid(row=3, column=0, sticky="w", padx=(0, 12), pady=6)
                codex_entry.grid(row=3, column=1, sticky="ew", pady=6)
            else:
                command_label.grid(row=3, column=0, sticky="w", padx=(0, 12), pady=6)
                command_entry.grid(row=3, column=1, sticky="ew", pady=6)

        ttk.Radiobutton(
            mode_frame, text="Codex 会话", value="codex", variable=values["launch_mode"],
            command=mode_changed,
        ).pack(side="left")
        ttk.Radiobutton(
            mode_frame, text="自定义命令", value="command", variable=values["launch_mode"],
            command=mode_changed,
        ).pack(side="left", padx=(12, 0))
        row_label(4, "Shell")
        ttk.Combobox(
            form, textvariable=values["shell"], values=("pwsh", "powershell", "cmd", "wsl"), state="readonly",
        ).grid(row=4, column=1, sticky="w", pady=6)
        ttk.Checkbutton(form, text="启用", variable=values["enabled"]).grid(row=5, column=1, sticky="w", pady=(3, 6))
        actions = ttk.Frame(form)
        actions.grid(row=6, column=0, columnspan=2, sticky="e", pady=(10, 0))
        mode_changed()

        def save() -> None:
            try:
                profile = Profile.from_dict({
                    "id": existing.id if existing else uuid.uuid4().hex,
                    "name": values["name"].get(),
                    "workingDirectory": values["directory"].get(),
                    "shell": values["shell"].get(),
                    "startupCommand": (
                        values["command"].get() if values["launch_mode"].get() == "command" else ""
                    ),
                    "launchMode": values["launch_mode"].get(),
                    "codexArgs": (
                        shlex.split(values["codex_args"].get())
                        if values["launch_mode"].get() == "codex" and values["codex_args"].get().strip()
                        else []
                    ),
                    "pinned": existing.pinned if existing else False,
                    "enabled": values["enabled"].get(),
                })
                cached_profiles = getattr(self, "catalog_profiles", None)
                profiles = list(cached_profiles if cached_profiles is not None else self.store.load())
                index = next((i for i, item in enumerate(profiles) if item.id == profile.id), -1)
                if index >= 0:
                    profiles[index] = profile
                else:
                    profiles.append(profile)
                self.catalog_profiles = self.store.save(profiles)
                self.profiles = sort_profiles_by_recent(
                    self.catalog_profiles, getattr(self, "recent_activity", {}),
                )
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

    def _codex_api_path(self, profile: Profile, suffix: str) -> str:
        return f"/v1/profiles/{quote(profile.id, safe='')}/{suffix}"

    def _terminate_for_codex_switch(self, profile: Profile) -> bool:
        route = self._profile_base_url(profile.id)
        status, value = self.api(
            f"/v1/sessions/{quote(profile.id, safe='')}/terminate",
            "POST", {"force": True, "remove": True}, timeout=8, base_url=route,
        )
        if status not in (200, 404):
            messagebox.showerror(
                "Codex 会话", str(value.get("message", value.get("error", "停止会话失败"))),
                parent=self.panel,
            )
            return False
        self.sessions.pop(profile.id, None)
        return True

    def _confirm_codex_switch(self, profile: Profile, target_thread_id: str) -> bool:
        session = self.sessions.get(profile.id) or {}
        if not session.get("running"):
            return True
        current = str(session.get("codexThreadId") or "")
        if current == target_thread_id:
            return True
        return messagebox.askyesno(
            "切换 Codex 会话",
            "当前 RelayTerm 终端正在运行其他会话。确认后将终止旧终端并打开所选会话。",
            parent=self.panel,
        )

    def _apply_codex_binding(
        self, profile: Profile, mode: str, thread_id: str = "", *, launch_after_switch: bool = True,
    ) -> bool:
        session = self.sessions.get(profile.id) or {}
        switching = bool(
            mode == "locked" and session.get("running")
            and str(session.get("codexThreadId") or "") != thread_id
        )
        if switching and not self._confirm_codex_switch(profile, thread_id):
            return False
        body: dict[str, object] = {"mode": mode}
        if mode == "locked":
            body["threadId"] = thread_id
        status, value = self.api(
            self._codex_api_path(profile, "codex-binding"), "PUT", body, timeout=10,
        )
        if status != 200:
            messagebox.showerror(
                "Codex 会话", str(value.get("message", value.get("error", "绑定更新失败"))),
                parent=self.panel,
            )
            return False
        details = dict(getattr(self, "codex_details", {}).get(profile.id, {}))
        details["mode"] = mode
        details["binding"] = value.get("binding", body)
        getattr(self, "codex_details", {}).update({profile.id: details})
        if switching:
            if not self._terminate_for_codex_switch(profile):
                return False
            if launch_after_switch:
                self._launch_profile(profile, fresh=True, codex_thread_id=thread_id)
        self._selection_changed()
        return True

    def show_codex_sessions(self, profile: Profile | None = None) -> None:
        profile = profile or self.selected_profile()
        if profile is None or profile.launch_mode != "codex":
            return
        try:
            status, value = self.api(
                self._codex_api_path(profile, "codex-sessions"), timeout=12,
            )
        except Exception as exc:
            messagebox.showerror("Codex 会话", str(exc), parent=self.panel)
            return
        if status != 200:
            messagebox.showerror(
                "Codex 会话", str(value.get("message", value.get("error", "读取会话失败"))),
                parent=self.panel,
            )
            return
        if not hasattr(self, "codex_details"):
            self.codex_details = {}
        self.codex_details[profile.id] = value
        self._selection_changed()

        dialog = tk.Toplevel(self.panel or self.root)
        dialog.title(f"Codex 会话 · {profile.name}")
        dialog.transient(self.panel or self.root)
        dialog.grab_set()
        dialog.geometry("760x430")
        dialog.minsize(660, 360)
        body = ttk.Frame(dialog, padding=14)
        body.grid(row=0, column=0, sticky="nsew")
        dialog.rowconfigure(0, weight=1)
        dialog.columnconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(2, weight=1)
        ttk.Label(body, text=codex_binding_text(value), style="Section.TLabel").grid(
            row=0, column=0, sticky="w",
        )
        current = value.get("currentRelayThread") if isinstance(value.get("currentRelayThread"), dict) else None
        current_id = str(current.get("id") or "") if current else ""
        ttk.Label(
            body,
            text=("当前 RelayTerm · " + current_id[:8]) if current_id else "当前 RelayTerm · 未运行",
            style="Muted.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(3, 8))

        columns = ("title", "source", "directory", "updated", "uuid")
        tree = ttk.Treeview(body, columns=columns, show="headings", selectmode="browse")
        for key, title, width in (
            ("title", "标题", 210), ("source", "来源", 80), ("directory", "目录", 250),
            ("updated", "更新时间", 100), ("uuid", "UUID", 72),
        ):
            tree.heading(key, text=title, anchor="w")
            tree.column(key, width=width, minwidth=60, stretch=key in ("title", "directory"), anchor="w")
        tree.grid(row=2, column=0, sticky="nsew")
        candidates: dict[str, dict[str, object]] = {}
        tree.insert("", "end", iid="__auto__", values=("自动选择", "", profile.working_directory, "", ""))
        for group in ("exactCandidates", "repositoryCandidates"):
            items = value.get(group)
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                thread_id = str(item.get("id", ""))
                if not thread_id:
                    continue
                candidates[thread_id] = item
                tree.insert("", "end", iid=thread_id, values=(
                    str(item.get("title", "")), str(item.get("source", "")),
                    str(item.get("cwd", "")), format_activity(item.get("updatedAt")), thread_id[:8],
                ))
        binding = value.get("binding") if isinstance(value.get("binding"), dict) else {}
        selected = str(binding.get("threadId") or "") if binding.get("mode") == "locked" else "__auto__"
        if not tree.exists(selected):
            selected = "__auto__"
        tree.selection_set(selected)
        tree.focus(selected)

        actions = ttk.Frame(body)
        actions.grid(row=3, column=0, sticky="e", pady=(10, 0))

        def create_new() -> None:
            session = self.sessions.get(profile.id) or {}
            if session.get("running") and not messagebox.askyesno(
                "新建 Codex 会话",
                "当前 RelayTerm 终端正在运行。确认后将创建新会话、终止旧终端并重新连接。",
                parent=dialog,
            ):
                return
            status_code, result = self.api(
                self._codex_api_path(profile, "codex-sessions"), "POST", {"lock": True}, timeout=12,
            )
            if status_code != 201:
                messagebox.showerror(
                    "Codex 会话", str(result.get("message", result.get("error", "新建会话失败"))),
                    parent=dialog,
                )
                return
            thread = result.get("thread") if isinstance(result.get("thread"), dict) else {}
            thread_id = str(thread.get("id") or "")
            if session.get("running") and not self._terminate_for_codex_switch(profile):
                return
            dialog.destroy()
            self._launch_profile(profile, fresh=bool(session.get("running")), codex_thread_id=thread_id)

        def apply_selection() -> None:
            selected_ids = tree.selection()
            if not selected_ids:
                return
            thread_id = selected_ids[0]
            if thread_id == "__auto__":
                if self._apply_codex_binding(profile, "auto"):
                    dialog.destroy()
                return
            if self._apply_codex_binding(profile, "locked", thread_id):
                dialog.destroy()

        ttk.Button(actions, text="新建会话", command=create_new).pack(side="left", padx=(0, 8))
        ttk.Button(actions, text="取消", command=dialog.destroy).pack(side="left", padx=(0, 8))
        ttk.Button(actions, text="应用", style="Accent.TButton", command=apply_selection).pack(side="left")
        tree.bind("<Double-1>", lambda _event: apply_selection())
        dialog.bind("<Escape>", lambda _event: dialog.destroy())

    def delete_selected(self) -> None:
        profile = self.selected_profile()
        if profile is None:
            return
        if not messagebox.askyesno("删除项目", f"从 RelayTerm 删除“{profile.name}”？", parent=self.panel):
            return
        was_running = bool((self.sessions.get(profile.id) or {}).get("running"))
        route = self._profile_base_url(profile.id)
        catalog = getattr(self, "catalog_profiles", None)
        if catalog is None:
            catalog = self.store.load()
        self.catalog_profiles = self.store.save(item for item in catalog if item.id != profile.id)
        self.profiles = sort_profiles_by_recent(
            self.catalog_profiles, getattr(self, "recent_activity", {}),
        )
        if isinstance(getattr(self, "recent_activity", None), dict):
            self.recent_activity.pop(profile.id, None)
        self.refresh_tree()
        if was_running:

            def terminate_removed() -> None:
                try:
                    self.api(
                        f"/v1/sessions/{quote(profile.id, safe='')}/terminate",
                        "POST",
                        {"force": True},
                        base_url=route,
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
        if profile.launch_mode == "codex" and not session.get("running"):
            try:
                status, value = self.api(
                    self._codex_api_path(profile, "codex-sessions"), timeout=12,
                )
            except Exception as exc:
                messagebox.showerror("Codex 会话", str(exc), parent=self.panel)
                return
            if status != 200:
                messagebox.showerror(
                    "Codex 会话", str(value.get("message", value.get("error", "读取会话失败"))),
                    parent=self.panel,
                )
                return
            if not hasattr(self, "codex_details"):
                self.codex_details = {}
            self.codex_details[profile.id] = value
            if bool(value.get("requiresSelection")):
                self.show_codex_sessions(profile)
                return
        self._launch_profile(profile, fresh=fresh)

    def _launch_profile(
        self, profile: Profile, *, fresh: bool = False, codex_thread_id: str = "",
    ) -> None:
        executable = _console_python_executable()
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(PROJECT_ROOT) + os.pathsep + environment.get("PYTHONPATH", "")
        wt = os.environ.get("RELAYTERM_WT", "wt.exe")
        previous_windows = dict(_windows_terminal_windows())
        previous_foreground_hwnd = (
            ctypes.windll.user32.GetForegroundWindow() if os.name == "nt" else None
        )
        command = [
            wt, "-w", "new", "--size",
            f"{DEFAULT_TERMINAL_COLUMNS},{DEFAULT_TERMINAL_ROWS}",
            "new-tab", "--title", profile.name,
            "--suppressApplicationTitle",
            "--startingDirectory", profile.working_directory,
            executable, "-m", "pc.attach_cli", "--profile-id", profile.id,
        ]
        route_host, route_port = self._profile_host_port(profile.id)
        command.extend(("--host", route_host, "--port", str(route_port)))
        if codex_thread_id:
            command.extend(("--codex-thread-id", codex_thread_id))
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
                route = self._profile_base_url(profile.id)
                status, value = self.api(
                    f"/v1/sessions/{quote(profile.id, safe='')}/terminate", "POST", {"force": True},
                    base_url=route,
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
        primary_sessions: list[dict[str, object]] | None = None
        drain_snapshots: list[tuple[str, list[dict[str, object]]]] = []
        catalog: dict[str, object] | None = None
        try:
            status, value = self.api("/v1/sessions")
            if status == 200:
                values = value.get("sessions", [])
                primary_sessions = [dict(item) for item in values if isinstance(item, dict)]
        except Exception:
            pass
        updated_drains: list[dict[str, object]] = []
        for raw_entry in list(getattr(self, "drain_bridges", [])):
            entry = dict(raw_entry)
            host, port = str(entry.get("host", self.host)), int(entry.get("port", 0))
            base_url = f"http://{host}:{port}"
            values: list[dict[str, object]] | None = None
            try:
                status, response = self.api("/v1/sessions", timeout=2, base_url=base_url)
                if status == 200 and isinstance(response.get("sessions"), list):
                    values = [
                        dict(item) for item in response["sessions"] if isinstance(item, dict)
                    ]
            except Exception:
                pass
            if values is None:
                if port_is_free(host, port):
                    self.logger.info("drained bridge disappeared port=%s", port)
                    continue
                preserved = [
                    dict(item) for profile_id, item in self.sessions.items()
                    if self.session_routes.get(profile_id) == base_url and bool(item.get("running"))
                ]
                drain_snapshots.append((base_url, preserved))
                updated_drains.append(entry)
                continue
            running = [item for item in values if bool(item.get("running"))]
            entry["profileIds"] = [
                str(item.get("profileId")) for item in running if item.get("profileId")
            ]
            entry["emptyPolls"] = 0 if running else int(entry.get("emptyPolls", 0) or 0) + 1
            if not running and int(entry["emptyPolls"]) >= 3:
                if terminate_drained_bridge(host, port, int(entry.get("pid", 0) or 0)):
                    self.logger.info("bridge drain completed port=%s", port)
                    legacy_agent_pid = int(entry.get("agentPid", 0) or 0)
                    if legacy_agent_pid:
                        if terminate_legacy_agent(legacy_agent_pid):
                            self.logger.info("legacy launcher stopped pid=%s", legacy_agent_pid)
                        else:
                            self.logger.warning(
                                "legacy launcher identity changed pid=%s", legacy_agent_pid,
                            )
                    if self.sidecar:
                        self.root.after(0, self._request_sidecar_promotion)
                    continue
            drain_snapshots.append((base_url, values))
            updated_drains.append(entry)

        self.drain_bridges = updated_drains
        if not self.drain_bridges:
            self.settings["host"], self.settings["port"] = self.host, self.port
            self.settings_store.save(self.settings)
        if isinstance(self.drain_state.get("primary"), dict):
            try:
                self._save_drain_state()
            except Exception as exc:
                self.logger.warning("failed to save drain topology: %s", exc)
        try:
            status, value = self.api("/v1/profiles")
            if status == 200:
                catalog = value
        except Exception:
            pass

        if primary_sessions is None:
            primary_sessions = [
                dict(item) for profile_id, item in self.sessions.items()
                if profile_id not in self.session_routes
            ]
        sessions, routes = merge_session_snapshots(primary_sessions, drain_snapshots)

        def complete() -> None:
            self._status_fetching = False
            self.port_notice = self._drain_notice()
            self.endpoint_var.set(self._endpoint_text())
            changed = False
            if catalog is not None:
                self._merge_catalog_activity(catalog)
                changed = True
            if sessions is not None:
                activity_map = getattr(self, "recent_activity", None)
                if not isinstance(activity_map, dict):
                    activity_map = {}
                    self.recent_activity = activity_map
                for item in sessions.values():
                    profile_id = str(item.get("profileId", ""))
                    timestamp = item.get("lastOpenedAt")
                    if profile_id and parse_utc_timestamp(timestamp) is not None:
                        current = activity_map.get(profile_id)
                        current_dt = parse_utc_timestamp(current)
                        incoming_dt = parse_utc_timestamp(timestamp)
                        if current_dt is None or (incoming_dt is not None and incoming_dt > current_dt):
                            activity_map[profile_id] = str(timestamp)
                    elif profile_id and "lastOpenedAt" in item:
                        activity_map.pop(profile_id, None)
                self.sessions = sessions
                self.session_routes = routes
                changed = True
            if changed:
                self.refresh_tree()

        self.root.after(0, complete)

    def remote_access(self) -> None:
        if self.tunnel is not None and self.tunnel.running:
            if self.pairing_url:
                # Challenges are intentionally short-lived; reopening the action
                # must replace an old QR instead of showing an expired one.
                self._request_pairing(self.tunnel, self.tunnel_url)
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
            self._request_pairing(tunnel, detail)
        elif state == "error":
            self.remote_var.set(f"{glyph('remote')}  远程访问")
            self.tunnel_status_var.set(f"隧道异常 ({detail})")
            self._close_pairing_dialog()
            write_process_record(os.getpid(), self.bridge.pid if self.bridge else 0, 0)
        else:
            self.remote_var.set(f"{glyph('remote')}  远程访问")
            self.tunnel_status_var.set("隧道未启动")
            self._close_pairing_dialog()
            self.tunnel_url = self.pairing_url = ""
            write_process_record(os.getpid(), self.bridge.pid if self.bridge else 0, 0)

    def _request_pairing(self, tunnel: QuickTunnel, tunnel_url: str) -> None:
        if not tunnel_url or self._pairing_fetching:
            return
        self._pairing_fetching = True
        self.tunnel_status_var.set("配对码生成中")
        threading.Thread(target=self._create_pairing, args=(tunnel, tunnel_url), daemon=True).start()

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
                self._pairing_fetching = False
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
                self.tunnel_status_var.set("隧道已连接")
                self._close_pairing_dialog()
                self.show_pairing()

            self.root.after(0, complete)
        except Exception as exc:
            message = f"配对码生成失败: {exc}"
            def failed() -> None:
                self._pairing_fetching = False
                if tunnel is self.tunnel:
                    self.tunnel_status_var.set(message)
            self.root.after(0, failed)

    def _close_pairing_dialog(self) -> None:
        dialog = self.pairing_dialog
        self.pairing_dialog = None
        if dialog is not None:
            try:
                if dialog.winfo_exists():
                    dialog.destroy()
            except tk.TclError:
                pass

    def refresh_pairing(self, dialog: tk.Toplevel | None = None) -> None:
        if dialog is not None and dialog is not self.pairing_dialog:
            return
        tunnel = self.tunnel
        if tunnel is None or not tunnel.running or not self.tunnel_url:
            return
        self._request_pairing(tunnel, self.tunnel_url)

    def show_pairing(self) -> None:
        if not self.pairing_url:
            return
        self._close_pairing_dialog()
        dialog = tk.Toplevel(self.panel or self.root)
        dialog.title("RelayTerm 远程配对")
        dialog.transient(self.panel or self.root)
        dialog.resizable(False, False)
        self.pairing_dialog = dialog
        def close_dialog() -> None:
            if self.pairing_dialog is dialog:
                self._close_pairing_dialog()
            else:
                try:
                    dialog.destroy()
                except tk.TclError:
                    pass
        dialog.protocol("WM_DELETE_WINDOW", close_dialog)
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
        ttk.Label(frame, text="二维码有效期 2 分钟，每次刷新后只能使用一次", style="Muted.TLabel").pack(
            anchor="center", pady=(0, 2),
        )
        actions = ttk.Frame(frame)
        actions.pack(fill="x", pady=(10, 0))
        actions.columnconfigure(0, weight=1)
        copy_actions = ttk.Frame(actions)
        copy_actions.grid(row=0, column=0, sticky="w")
        ttk.Button(copy_actions, text="复制隧道地址", command=lambda: self.copy_text(self.tunnel_url)).pack(side="left")
        ttk.Button(copy_actions, text="复制配对页", command=lambda: self.copy_text(self.pairing_url)).pack(side="left", padx=(8, 0))
        ttk.Button(copy_actions, text="刷新二维码", command=lambda: self.refresh_pairing(dialog)).pack(side="left", padx=(8, 0))
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
        self._close_pairing_dialog()
        self.tunnel_url = self.pairing_url = ""
        self._pairing_fetching = False
        if self.qr_path is not None:
            try:
                self.qr_path.unlink()
            except OSError:
                pass
            self.qr_path = None
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
    parser.add_argument("--drain-sidecar", action="store_true")
    arguments, _unknown = parser.parse_known_args()
    standard_instance = SingleInstance()
    sidecar = bool(arguments.drain_sidecar)
    instance = standard_instance
    standard_holder: list[SingleInstance | None] = [standard_instance if standard_instance.acquired else None]
    if not standard_instance.acquired:
        standard_instance.close()
        standard_holder[0] = None
        settings = SettingsStore().load()
        token = TokenStore().load_or_create()
        existing = probe_bridge(
            str(settings.get("host", "127.0.0.1")), int(settings.get("port", 18765)), token,
        )
        if existing is not None and existing.generation == BRIDGE_GENERATION:
            return
        sidecar = True
        instance = SingleInstance("Local\\RelayTerm.Agent.DrainSwitchV1")
        if not instance.acquired:
            instance.close()
            return

    def promote() -> bool:
        if standard_holder[0] is not None:
            return True
        candidate = SingleInstance()
        if candidate.acquired:
            standard_holder[0] = candidate
            return True
        candidate.close()
        return False

    enable_dpi_awareness()
    root = tk.Tk()
    apply_theme(root)
    agent = Agent(root, sidecar=sidecar, promote_callback=promote)
    try:
        root.mainloop()
    finally:
        try:
            agent.close()
        except Exception:
            pass
        instance.close()
        held = standard_holder[0]
        if held is not None and held is not instance:
            held.close()


if __name__ == "__main__":
    main()
