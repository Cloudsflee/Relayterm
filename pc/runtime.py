"""Windows lifecycle helpers for the RelayTerm launcher."""

from __future__ import annotations

import ctypes
import logging
import os
import re
import shutil
import socket
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable

from .config import atomic_write, data_directory


ERROR_ALREADY_EXISTS = 183
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_SYSKEYDOWN = 0x0104
WM_KEYUP = 0x0101
WM_SYSKEYUP = 0x0105
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_NOREPEAT = 0x4000


class SingleInstance:
    def __init__(self, name: str = "Local\\RelayTerm.Agent") -> None:
        self.handle = None
        self.acquired = True
        if os.name == "nt":
            kernel32 = ctypes.windll.kernel32
            self.handle = kernel32.CreateMutexW(None, False, name)
            if not self.handle:
                raise ctypes.WinError()
            self.acquired = kernel32.GetLastError() != ERROR_ALREADY_EXISTS

    def close(self) -> None:
        if self.handle and os.name == "nt":
            ctypes.windll.kernel32.CloseHandle(self.handle)
            self.handle = None


class HotKeyListener:
    def __init__(self, callback: Callable[[], None], key: str = "R") -> None:
        self.callback = callback
        value = str(key or "R").strip().upper()
        self.key = value if len(value) == 1 and "A" <= value <= "Z" else "R"
        self.thread: threading.Thread | None = None
        self.thread_id = 0
        self.error = ""
        self._hook = None
        self._hook_proc = None

    def start(self) -> None:
        if os.name != "nt" or self.thread is not None:
            return
        self.thread = threading.Thread(target=self._run, name="relayterm-hotkey", daemon=True)
        self.thread.start()

    def _run(self) -> None:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        self.thread_id = kernel32.GetCurrentThreadId()
        hotkey_id = 0x5254
        registered = bool(
            user32.RegisterHotKey(
                None,
                hotkey_id,
                MOD_CONTROL | MOD_ALT | MOD_SHIFT | MOD_NOREPEAT,
                ord(self.key),
            )
        )
        if not registered:
            # Some desktop utilities reserve common recording chords. A
            # low-level hook keeps the configured launcher chord usable.
            self._install_keyboard_fallback(user32, kernel32)
            if self._hook is None:
                self.error = "hotkey_registration_failed"
        try:
            message = wintypes_msg()
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                if message.message == WM_HOTKEY and message.wParam == hotkey_id:
                    self.callback()
        finally:
            if registered:
                user32.UnregisterHotKey(None, hotkey_id)
            if self._hook is not None:
                user32.UnhookWindowsHookEx(self._hook)
                self._hook = None

    def _install_keyboard_fallback(self, user32, kernel32) -> None:
        class KeyboardInput(ctypes.Structure):
            _fields_ = [
                ("vkCode", ctypes.c_uint32), ("scanCode", ctypes.c_uint32),
                ("flags", ctypes.c_uint32), ("time", ctypes.c_uint32),
                ("dwExtraInfo", ctypes.c_void_p),
            ]

        callback_type = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_int, ctypes.c_size_t, ctypes.c_size_t)
        ctrl_down = [False]
        alt_down = [False]
        shift_down = [False]
        triggered = [False]

        def hook_proc(code, w_param, l_param):
            if code >= 0:
                data = ctypes.cast(l_param, ctypes.POINTER(KeyboardInput)).contents
                vk = int(data.vkCode)
                if w_param in (WM_KEYDOWN, WM_SYSKEYDOWN):
                    if vk in (0xA2, 0xA3, 0x11):
                        ctrl_down[0] = True
                    elif vk in (0xA4, 0xA5, 0x12):
                        alt_down[0] = True
                    elif vk in (0xA0, 0xA1, 0x10):
                        shift_down[0] = True
                    elif vk == ord(self.key) and ctrl_down[0] and alt_down[0] and shift_down[0] and not triggered[0]:
                        triggered[0] = True
                        self.callback()
                elif w_param in (WM_KEYUP, WM_SYSKEYUP):
                    if vk in (0xA2, 0xA3, 0x11):
                        ctrl_down[0] = False
                    elif vk in (0xA4, 0xA5, 0x12):
                        alt_down[0] = False
                    elif vk in (0xA0, 0xA1, 0x10):
                        shift_down[0] = False
                    elif vk == ord(self.key):
                        triggered[0] = False
            return user32.CallNextHookEx(self._hook, code, w_param, l_param)

        self._hook_proc = callback_type(hook_proc)
        module = kernel32.GetModuleHandleW(None)
        self._hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._hook_proc, module, 0)
        if self._hook:
            self.error = ""

    def stop(self) -> None:
        if self.thread_id and os.name == "nt":
            ctypes.windll.user32.PostThreadMessageW(self.thread_id, WM_QUIT, 0, 0)
        if self.thread is not None:
            self.thread.join(timeout=1)
            self.thread = None


class _Point(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class wintypes_msg(ctypes.Structure):
    _fields_ = [
        ("hwnd", ctypes.c_void_p), ("message", ctypes.c_uint),
        ("wParam", ctypes.c_size_t), ("lParam", ctypes.c_ssize_t),
        ("time", ctypes.c_uint32), ("pt", _Point), ("lPrivate", ctypes.c_uint32),
    ]


def startup_command(project_root: Path) -> str:
    executable = Path(sys.executable)
    pythonw = executable.with_name("pythonw.exe")
    if not pythonw.exists():
        pythonw = executable
    script = project_root / "pc" / "agent.py"
    return f'"{pythonw}" "{script}" --startup'


def install_startup(project_root: Path) -> None:
    if os.name != "nt":
        return
    import winreg
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
        winreg.SetValueEx(key, "RelayTerm", 0, winreg.REG_SZ, startup_command(project_root))


def remove_startup() -> None:
    if os.name != "nt":
        return
    import winreg
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0, winreg.KEY_SET_VALUE,
        ) as key:
            winreg.DeleteValue(key, "RelayTerm")
    except FileNotFoundError:
        pass


def port_available(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.25)
        try:
            if probe.connect_ex((host, int(port))) == 0:
                return False
        except OSError:
            pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as stream:
        try:
            stream.bind((host, int(port)))
            return True
        except OSError:
            return False


def find_cloudflared() -> str | None:
    configured = os.environ.get("RELAYTERM_CLOUDFLARED", "")
    candidates = [
        configured,
        shutil.which("cloudflared") or "",
        r"D:\02_dev-tools\auto_test_chrome\cloudflared.exe",
    ]
    return next((item for item in candidates if item and Path(item).is_file()), None)


class RedactingFilter(logging.Filter):
    TOKEN_PATTERN = re.compile(r"(?i)(authorization:\s*bearer\s+|token[=:\s]+)([^\s,;]+)")
    CHALLENGE_PATTERN = re.compile(r"(?i)(challenge[=:\s/]+)([A-Za-z0-9_-]{16,})")
    PAIR_PATH_PATTERN = re.compile(r"(?i)(/pair/)([A-Za-z0-9_-]{16,})")

    def __init__(self, secrets: list[str] | None = None) -> None:
        super().__init__()
        self.secrets = [item for item in (secrets or []) if item]

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        for value in self.secrets:
            message = message.replace(value, "[redacted]")
        message = self.TOKEN_PATTERN.sub(r"\1[redacted]", message)
        message = self.CHALLENGE_PATTERN.sub(r"\1[redacted]", message)
        message = self.PAIR_PATH_PATTERN.sub(r"\1[redacted]", message)
        record.msg, record.args = message, ()
        return True


def configure_logging(token: str = "") -> logging.Logger:
    directory = data_directory()
    directory.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("relayterm.agent")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = RotatingFileHandler(
            directory / "agent.log", maxBytes=1024 * 1024, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        handler.addFilter(RedactingFilter([token]))
        logger.addHandler(handler)
    return logger


def write_process_record(agent_pid: int, bridge_pid: int = 0, cloudflared_pid: int = 0) -> None:
    import json
    value = {"agentPid": agent_pid, "bridgePid": bridge_pid, "cloudflaredPid": cloudflared_pid}
    atomic_write(data_directory() / "processes.json", (json.dumps(value, indent=2) + "\n").encode("ascii"))
