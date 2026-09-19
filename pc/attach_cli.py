"""Raw Windows Terminal client for an existing RelayTerm PTY session."""

from __future__ import annotations

import argparse
import ctypes
import json
import msvcrt
import os
import sys
import threading
import time
import uuid
from ctypes import wintypes
from pathlib import Path
from urllib.parse import quote

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from pc.config import SettingsStore, TokenStore
else:
    from .config import SettingsStore, TokenStore

import websocket

STD_INPUT_HANDLE = -10
STD_OUTPUT_HANDLE = -11
ENABLE_PROCESSED_INPUT = 0x0001
ENABLE_LINE_INPUT = 0x0002
ENABLE_ECHO_INPUT = 0x0004
ENABLE_VIRTUAL_TERMINAL_INPUT = 0x0200
ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
DISABLE_NEWLINE_AUTO_RETURN = 0x0008

SPECIAL_KEYS = {
    "H": b"\x1b[A", "P": b"\x1b[B", "K": b"\x1b[D", "M": b"\x1b[C",
    "G": b"\x1b[H", "O": b"\x1b[F", "I": b"\x1b[5~", "Q": b"\x1b[6~",
    "R": b"\x1b[2~", "S": b"\x1b[3~",
}
INPUT_QUIET_SECONDS = 0.004
INPUT_MAX_BATCH_SECONDS = 0.025
MAX_FRAME_BYTES = 64 * 1024
HEARTBEAT_SECONDS = 5.0

# SetConsoleCtrlHandler event values.  CTRL_C/CTRL_BREAK remain input
# controls; closing/logoff/shutdown mean that the desktop attachment is gone.
CTRL_CLOSE_EVENT = 2
CTRL_LOGOFF_EVENT = 5
CTRL_SHUTDOWN_EVENT = 6
TERMINAL_CLOSED_REASON = "terminal_closed"


def terminal_size() -> tuple[int, int]:
    size = os.get_terminal_size(sys.stdout.fileno())
    return max(2, min(size.columns, 400)), max(2, min(size.lines, 200))


class ConsoleMode:
    def __init__(self) -> None:
        self.kernel32 = ctypes.windll.kernel32
        self.input = self.kernel32.GetStdHandle(STD_INPUT_HANDLE)
        self.output = self.kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
        self.input_mode = ctypes.c_uint()
        self.output_mode = ctypes.c_uint()

    def __enter__(self):
        if not self.kernel32.GetConsoleMode(self.input, ctypes.byref(self.input_mode)):
            raise OSError("console_input_unavailable")
        if not self.kernel32.GetConsoleMode(self.output, ctypes.byref(self.output_mode)):
            raise OSError("console_output_unavailable")
        raw = self.input_mode.value & ~(ENABLE_LINE_INPUT | ENABLE_ECHO_INPUT | ENABLE_PROCESSED_INPUT)
        self.kernel32.SetConsoleMode(self.input, raw | ENABLE_VIRTUAL_TERMINAL_INPUT)
        self.kernel32.SetConsoleMode(
            self.output, self.output_mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING | DISABLE_NEWLINE_AUTO_RETURN
        )
        return self

    def __exit__(self, *_args) -> None:
        self.kernel32.SetConsoleMode(self.input, self.input_mode.value)
        self.kernel32.SetConsoleMode(self.output, self.output_mode.value)


def _read_console_value() -> bytes:
    value = msvcrt.getwch()
    if value in ("\x00", "\xe0"):
        return SPECIAL_KEYS.get(msvcrt.getwch(), b"")
    if "\ud800" <= value <= "\udbff":
        deadline = time.monotonic() + 0.05
        while time.monotonic() < deadline and not msvcrt.kbhit():
            time.sleep(0.001)
        if msvcrt.kbhit():
            value += msvcrt.getwch()
    try:
        return value.encode("utf-8", "surrogatepass")
    except UnicodeEncodeError:
        return b""


def read_key() -> bytes | None:
    """Read one console input burst so terminal replies stay in one PTY write."""
    if not msvcrt.kbhit():
        return None
    started = time.monotonic()
    quiet_deadline = started + INPUT_QUIET_SECONDS
    result = bytearray(_read_console_value())
    while True:
        while msvcrt.kbhit():
            result.extend(_read_console_value())
            quiet_deadline = time.monotonic() + INPUT_QUIET_SECONDS
        now = time.monotonic()
        if now >= quiet_deadline or now - started >= INPUT_MAX_BATCH_SECONDS:
            break
        time.sleep(min(0.0005, quiet_deadline - now))
    return bytes(result)


def client_id() -> str:
    settings = SettingsStore()
    value = settings.load()
    current = str(value.get("desktopClientId", ""))
    if not current:
        current = "desktop-" + uuid.uuid4().hex
        value["desktopClientId"] = current
        settings.save(value)
    return current


def websocket_url(host: str, port: int, profile_id: str) -> str:
    return f"ws://{host}:{port}/v1/pty?sessionId={quote(profile_id, safe='')}"


def connect_stream(url: str, token: str):
    """Bound the handshake, then keep an idle terminal session connected."""
    stream = websocket.create_connection(
        url, header=["Authorization: Bearer " + token], timeout=10,
    )
    stream.settimeout(None)
    return stream


def send_input_batch(stream, value: bytes) -> None:
    """Preserve byte order while translating control keys inside a batch."""
    def send_binary(data: bytes) -> None:
        for offset in range(0, len(data), MAX_FRAME_BYTES):
            stream.send_binary(data[offset: offset + MAX_FRAME_BYTES])

    start = 0
    for index, byte in enumerate(value):
        if byte not in (0x03, 0x04):
            continue
        if index > start:
            send_binary(value[start:index])
        name = "INT" if byte == 0x03 else "EOF"
        stream.send(json.dumps({"type": "signal", "name": name}))
        start = index + 1
    if start < len(value):
        send_binary(value[start:])


def send_terminal_closed(stream) -> bool:
    """Tell the bridge that only the desktop window is closing."""
    try:
        return bool(stream.send(json.dumps({
            "type": "close", "terminate": False, "reason": TERMINAL_CLOSED_REASON,
        })))
    except Exception:
        return False


def install_console_close_handler(stream, stop: threading.Event, notify=None):
    """Register a Windows console lifecycle callback and return an unregisterer."""
    if os.name != "nt":
        return lambda: None
    kernel32 = ctypes.windll.kernel32
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

    def handler(event):
        if int(event) in (CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT):
            (notify or (lambda: send_terminal_closed(stream)))()
            stop.set()
            return True
        return False

    # Keep the callback alive for as long as the handler is registered.
    callback = callback_type(handler)
    if not kernel32.SetConsoleCtrlHandler(callback, True):
        return lambda: None

    def unregister() -> None:
        try:
            kernel32.SetConsoleCtrlHandler(callback, False)
        except Exception:
            pass

    unregister.callback = callback  # type: ignore[attr-defined]
    return unregister


def start_heartbeat(stream, stop: threading.Event, interval: float = HEARTBEAT_SECONDS) -> threading.Thread:
    """Send a small JSON ping often enough for bridge liveness detection."""
    def loop() -> None:
        while not stop.wait(max(0.25, float(interval))):
            try:
                if not stream.send(json.dumps({"type": "ping"})):
                    stop.set()
                    return
            except Exception:
                stop.set()
                return

    thread = threading.Thread(target=loop, name="relayterm-attach-heartbeat", daemon=True)
    thread.start()
    return thread


def build_open_message(
    profile_id: str, desktop_client_id: str, cols: int, rows: int,
    *, fresh: bool = False, codex_thread_id: str = "",
) -> dict[str, object]:
    value: dict[str, object] = {
        "type": "open", "profileId": profile_id, "sessionId": profile_id,
        "clientId": desktop_client_id, "clientType": "desktop",
        "cols": cols, "rows": rows, "resume": not fresh,
    }
    if codex_thread_id:
        value["codexThreadId"] = codex_thread_id
    return value


def is_terminal_error(event: dict[str, object], ready: bool) -> bool:
    return bool(event.get("fatal")) or not ready or str(event.get("code", "")) in (
        "codex_launch_failed", "session_ended", "Pty is closed", "attachment_inactive",
    )


def run(
    profile_id: str,
    fresh: bool = False,
    codex_thread_id: str = "",
    host_override: str = "",
    port_override: int = 0,
) -> int:
    settings = SettingsStore().load()
    host = str(host_override or settings.get("host", "127.0.0.1"))
    port = int(port_override or settings.get("port", 18765))
    token = TokenStore().load_or_create()
    os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
    try:
        stream = connect_stream(websocket_url(host, port, profile_id), token)
    except Exception as exc:
        sys.stderr.write(f"RelayTerm attach failed: {exc}\n")
        return 2
    cols, rows = terminal_size()
    open_message = build_open_message(
        profile_id, client_id(), cols, rows, fresh=fresh, codex_thread_id=codex_thread_id,
    )
    stream.send(json.dumps(open_message))
    stop = threading.Event()
    terminal_close_sent = threading.Event()
    shell_exited = threading.Event()
    ready = threading.Event()
    exit_code = [0]

    def notify_terminal_closed() -> bool:
        if terminal_close_sent.is_set():
            return True
        terminal_close_sent.set()
        return send_terminal_closed(stream)

    unregister_console_handler = install_console_close_handler(stream, stop, notify_terminal_closed)

    def receive() -> None:
        try:
            while not stop.is_set():
                value = stream.recv()
                if value is None or value == "":
                    break
                if isinstance(value, bytes):
                    sys.stdout.buffer.write(value)
                    sys.stdout.buffer.flush()
                    continue
                event = json.loads(value)
                kind = event.get("type", "")
                if kind == "ready":
                    ready.set()
                    role = event.get("role", "observer")
                    ctypes.windll.kernel32.SetConsoleTitleW(f"RelayTerm - {profile_id} [{role}]")
                elif kind == "control_changed":
                    role = "controller" if event.get("controllerClientId") == client_id() else "observer"
                    ctypes.windll.kernel32.SetConsoleTitleW(f"RelayTerm - {profile_id} [{role}]")
                elif kind == "exit":
                    exit_code[0] = int(event.get("code", 0))
                    shell_exited.set()
                    break
                elif kind == "error":
                    sys.stderr.write(f"\r\nRelayTerm: {event.get('message', event.get('code', 'error'))}\r\n")
                    if is_terminal_error(event, ready.is_set()):
                        ready.clear()
                        exit_code[0] = 1
                        break
        except Exception:
            pass
        finally:
            stop.set()

    reader = threading.Thread(target=receive, name="relayterm-attach-recv", daemon=True)
    reader.start()
    heartbeat = start_heartbeat(stream, stop)
    last_size = (cols, rows)
    try:
        with ConsoleMode():
            while not stop.wait(0.01):
                current_size = terminal_size()
                if current_size != last_size:
                    last_size = current_size
                    stream.send(json.dumps({
                        "type": "resize", "cols": current_size[0], "rows": current_size[1]
                    }))
                value = read_key()
                if value is None or value == b"":
                    continue
                if ready.is_set() and not stop.is_set():
                    send_input_batch(stream, value)
    except (KeyboardInterrupt, OSError, websocket.WebSocketException):
        pass
    finally:
        stop.set()
        # A clean client shutdown is still a desktop-window close.  The
        # bridge will prioritize a shell exit event when one was already seen.
        if not shell_exited.is_set() and reader.is_alive():
            notify_terminal_closed()
        try:
            unregister_console_handler()
        except Exception:
            pass
        try:
            stream.close()
        except Exception:
            pass
        reader.join(timeout=1)
        heartbeat.join(timeout=1)
    return exit_code[0]


def main() -> None:
    parser = argparse.ArgumentParser(description="Attach Windows Terminal to a RelayTerm project")
    parser.add_argument("--profile-id", required=True)
    parser.add_argument("--fresh", action="store_true")
    parser.add_argument("--codex-thread-id", default="")
    parser.add_argument("--host", default="")
    parser.add_argument("--port", type=int, default=0)
    arguments = parser.parse_args()
    raise SystemExit(run(
        arguments.profile_id, arguments.fresh, arguments.codex_thread_id,
        arguments.host, arguments.port,
    ))


if __name__ == "__main__":
    main()
