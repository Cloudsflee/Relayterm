"""Reconnectable PTY sessions with independent desktop and Android attachments."""

from __future__ import annotations

import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

try:
    from .pty_backend import PtyBackend, PtyLaunchSpec, spawn_pty
except ImportError:  # direct script execution
    from pty_backend import PtyBackend, PtyLaunchSpec, spawn_pty  # type: ignore


MAX_OUTPUT_BYTES = 512 * 1024
MAX_FRAME_BYTES = 64 * 1024
MAX_SESSION_ID = 128
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
DESKTOP_ATTACHMENT_TIMEOUT_SECONDS = 15.0

_SIGNAL_ALIASES = {
    "INT": "INT",
    "SIGINT": "INT",
    "CTRL-C": "INT",
    "CTRL_C": "INT",
    "EOF": "EOF",
    "D": "EOF",
    "CTRL-D": "EOF",
    "CTRL_D": "EOF",
}

# These modes make a terminal synthesize input bytes. Replaying an old mode
# transition into a fresh Windows Terminal can therefore inject focus, mouse,
# or Win32 key records into the current foreground program.
_REPLAY_INPUT_MODES = {
    1000, 1001, 1002, 1003, 1004, 1005, 1006, 1007, 1015, 9001,
}

BinarySink = Callable[[bytes], object]
EventSink = Callable[[dict[str, object]], object]
CloseSink = Callable[[], None]


def validate_session_id(value: str) -> str:
    value = str(value or "").strip()
    if not value or len(value) > MAX_SESSION_ID or not SESSION_ID_RE.fullmatch(value):
        raise ValueError("session_id_invalid")
    return value


def validate_client_id(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        return "client-" + uuid.uuid4().hex
    if not CLIENT_ID_RE.fullmatch(value):
        raise ValueError("client_id_invalid")
    return value


def clamp_dimensions(cols: int | str | None, rows: int | str | None) -> tuple[int, int]:
    try:
        parsed_cols = int(cols or 100)
    except (TypeError, ValueError):
        parsed_cols = 100
    try:
        parsed_rows = int(rows or 32)
    except (TypeError, ValueError):
        parsed_rows = 32
    return max(2, min(parsed_cols, 400)), max(2, min(parsed_rows, 200))


def _normalise_signal(value: str) -> str:
    signal = str(value or "").upper()
    try:
        return _SIGNAL_ALIASES[signal]
    except KeyError as exc:
        raise ValueError("signal_invalid") from exc


def utc_timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _control_string_end(data: bytes, start: int) -> tuple[int, int] | None:
    """Return (payload_end, sequence_end) for an OSC/DCS control string."""
    index = start + 2
    while index < len(data):
        if data[index] == 0x07:
            return index, index + 1
        if data[index] == 0x1B and index + 1 < len(data) and data[index + 1] == 0x5C:
            return index, index + 2
        index += 1
    return None


def sanitize_desktop_replay(value: bytes) -> bytes:
    """Remove replayed controls that would make a terminal generate input."""
    data = bytes(value)
    visible = bytearray()
    index = 0
    while index < len(data):
        if data[index] != 0x1B or index + 1 >= len(data):
            visible.append(data[index])
            index += 1
            continue

        kind = data[index + 1]
        if kind == 0x5B:  # CSI
            end = index + 2
            while end < len(data) and not 0x40 <= data[end] <= 0x7E:
                end += 1
            if end >= len(data):
                visible.extend(data[index:])
                break
            sequence_end = end + 1
            body = data[index + 2:end]
            final = data[end]
            unsafe = final in b"cnt"
            unsafe = unsafe or (
                final == ord("x") and all(item.isdigit() for item in body.split(b";") if item)
                and b"$" not in body
            )
            unsafe = unsafe or (final == ord("p") and body.endswith(b"$"))
            unsafe = unsafe or (final == ord("y") and body.endswith(b"*"))
            unsafe = unsafe or (final == ord("u") and body[:1] in (b"?", b">", b"=", b"<"))
            if final in (ord("h"), ord("l")) and body.startswith(b"?"):
                modes = {
                    int(item) for item in body[1:].split(b";")
                    if item.isdigit()
                }
                unsafe = unsafe or bool(modes & _REPLAY_INPUT_MODES)
            if not unsafe:
                visible.extend(data[index:sequence_end])
            index = sequence_end
            continue

        if kind in (0x5D, 0x50):  # OSC or DCS
            bounds = _control_string_end(data, index)
            if bounds is None:
                visible.extend(data[index:])
                break
            payload_end, sequence_end = bounds
            payload = data[index + 2:payload_end]
            if kind == 0x5D:
                fields = payload.split(b";")
                query_commands = {
                    b"4", b"5", b"10", b"11", b"12", b"13", b"14", b"15",
                    b"16", b"17", b"18", b"19", b"52",
                }
                unsafe = bool(fields and fields[0] in query_commands and b"?" in fields[1:])
            else:
                unsafe = payload.startswith((b"$q", b"+q", b"=q"))
            if not unsafe:
                visible.extend(data[index:sequence_end])
            index = sequence_end
            continue

        if kind == ord("Z"):  # DECID, the 7-bit form of a device-attributes query.
            index += 2
            continue
        visible.extend(data[index:index + 2])
        index += 2
    return bytes(visible)


def replay_snapshot(value: bytes, client_type: str, resumed: bool) -> bytes:
    """Prepare historical output for an attachment without changing live PTY bytes."""
    if resumed and str(client_type).strip().lower() == "desktop":
        return sanitize_desktop_replay(value)
    return bytes(value)


@dataclass
class SessionAttachment:
    binary_sink: BinarySink
    event_sink: EventSink
    generation: int
    client_id: str
    client_type: str
    cols: int
    rows: int
    close_sink: Optional[CloseSink] = None
    output_deferred: bool = False
    attachment_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    closed: bool = False
    _pending_output: list[bytes] = field(default_factory=list, repr=False)
    _pending_output_bytes: int = field(default=0, repr=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    # Monotonic time is used for expiry comparisons; the wall-clock value is
    # exposed only for diagnostics/status responses.  These fields come after
    # the legacy optional fields so positional construction remains compatible.
    last_received_at: float = field(default_factory=time.monotonic)
    last_received_wall: float = field(default_factory=time.time)

    def touch(self, when: float | None = None) -> None:
        """Record an inbound client frame without changing PTY activity."""
        with self._lock:
            if self.closed:
                return
            if when is None:
                self.last_received_at = time.monotonic()
                self.last_received_wall = time.time()
            elif float(when) > 100_000_000:
                self.last_received_wall = float(when)
                self.last_received_at = time.monotonic() - max(0.0, time.time() - float(when))
            else:
                self.last_received_at = float(when)
                self.last_received_wall = time.time()

    def send_binary(self, value: bytes) -> bool:
        with self._lock:
            if self.closed:
                return False
            data = bytes(value)
            if self.output_deferred:
                if self._pending_output_bytes + len(data) > MAX_OUTPUT_BYTES:
                    self.closed = True
                    self._pending_output.clear()
                    self._pending_output_bytes = 0
                    return False
                self._pending_output.append(data)
                self._pending_output_bytes += len(data)
                return True
            try:
                result = self.binary_sink(data)
                return result is not False
            except Exception:
                self.closed = True
                return False

    def activate_output(self, snapshot: bytes = b"") -> bool:
        """Send snapshot then buffered live bytes without allowing interleaving."""
        with self._lock:
            if self.closed:
                return False
            pending = [bytes(snapshot), *self._pending_output]
            self._pending_output.clear()
            self._pending_output_bytes = 0
            try:
                for value in pending:
                    for offset in range(0, len(value), MAX_FRAME_BYTES):
                        result = self.binary_sink(value[offset: offset + MAX_FRAME_BYTES])
                        if result is False:
                            self.closed = True
                            return False
                self.output_deferred = False
                return True
            except Exception:
                self.closed = True
                return False

    def send_event(self, value: dict[str, object]) -> bool:
        with self._lock:
            if self.closed:
                return False
            try:
                result = self.event_sink(value)
                return result is not False
            except Exception:
                self.closed = True
                return False

    def close(self, invoke_sink: bool = True) -> None:
        with self._lock:
            if self.closed:
                return
            self.closed = True
            self._pending_output.clear()
            self._pending_output_bytes = 0
            close_sink = self.close_sink if invoke_sink else None
        if close_sink is not None:
            try:
                close_sink()
            except Exception:
                pass


class PtySession:
    def __init__(
        self,
        session_id: str,
        startup_command: str | PtyLaunchSpec,
        cwd: str,
        cols: int,
        rows: int,
        backend_factory: Callable[..., PtyBackend],
        output_limit: int = MAX_OUTPUT_BYTES,
        profile_id: str = "",
        start_immediately: bool = True,
    ) -> None:
        self.session_id = validate_session_id(session_id)
        if isinstance(startup_command, PtyLaunchSpec):
            self.launch_spec = startup_command
        else:
            if len(str(startup_command)) > 8192:
                raise ValueError("startup_command_too_long")
            self.launch_spec = PtyLaunchSpec.legacy(str(startup_command).strip() or "codex", cwd)
        self.startup_command = self.launch_spec.startup_command
        self.profile_id = str(profile_id or "")
        self.codex_thread_id = str(getattr(self.launch_spec, "codex_thread_id", "") or "")
        self.cwd = os.path.abspath(cwd) if cwd and os.path.isdir(cwd) else os.getcwd()
        self.cols, self.rows = clamp_dimensions(cols, rows)
        self.output_limit = max(MAX_FRAME_BYTES, int(output_limit))
        self.output = bytearray()
        self.created_wall = time.time()
        self.created_at = time.monotonic()
        self.last_activity_wall = self.created_wall
        self.last_activity = self.created_at
        self.ended = False
        self.exit_code: Optional[int] = None
        self.desktop_state = "never"
        self.desktop_closed_at: float | None = None
        self.last_opened_at: str | None = None
        self.attachments: dict[str, SessionAttachment] = {}
        self.controller_attachment_id: str | None = None
        self._attachment_generation = 0
        self._lock = threading.RLock()
        self._input_lock = threading.Lock()
        self._backend_factory = backend_factory
        self.backend: Optional[PtyBackend] = None
        if start_immediately:
            self.ensure_started()

    @property
    def pid(self) -> int:
        with self._lock:
            return int(self.backend.pid if self.backend is not None else 0)

    @property
    def attachment(self) -> SessionAttachment | None:
        """Compatibility view of the current controller."""
        with self._lock:
            return self.attachments.get(self.controller_attachment_id or "")

    def _touch(self) -> None:
        self.last_activity = time.monotonic()
        self.last_activity_wall = time.time()

    def _refresh_desktop_state_locked(self) -> None:
        active = any(
            item.client_type == "desktop" and not item.closed
            for item in self.attachments.values()
        )
        if active:
            self.desktop_state = "connected"
            self.desktop_closed_at = None
        elif self.desktop_state == "connected":
            self.desktop_state = "closed"
            self.desktop_closed_at = time.time()

    def set_last_opened_at(self, value: str | None) -> None:
        with self._lock:
            self.last_opened_at = str(value) if value else None

    def touch_attachment(self, attachment: SessionAttachment) -> bool:
        """Update an attachment heartbeat only while it is still registered."""
        with self._lock:
            current = self.attachments.get(attachment.attachment_id)
            if current is not attachment or attachment.closed:
                return False
        attachment.touch()
        return True

    def reap_stale_desktop_attachments(self, timeout: float = DESKTOP_ATTACHMENT_TIMEOUT_SECONDS) -> list[str]:
        """Detach desktop sockets that stopped sending frames.

        Android attachments intentionally do not participate in this expiry;
        they may remain observers while a phone is backgrounded for minutes.
        """
        cutoff = time.monotonic() - max(0.1, float(timeout))
        wall_cutoff = time.time() - max(0.1, float(timeout))
        def is_stale(item: SessionAttachment) -> bool:
            frame_value = item.last_received_at
            frame_stale = (
                frame_value < wall_cutoff
                if frame_value > 100_000_000 else frame_value < cutoff
            )
            return frame_stale or item.last_received_wall < wall_cutoff

        with self._lock:
            stale_items = [
                item for item in self.attachments.values()
                if not item.closed and item.client_type == "desktop"
                and is_stale(item)
            ]
        for item in stale_items:
            self.detach(item, invoke_close=True, reason="heartbeat_timeout")
        return [item.attachment_id for item in stale_items]

    def ensure_started(self) -> None:
        """Start the PTY once, after a transport has had a chance to attach."""
        with self._lock:
            if self.backend is not None or self.ended:
                return
            self.backend = self._backend_factory(
                self.launch_spec,
                self.cwd,
                self.cols,
                self.rows,
                self.on_output,
                self.on_exit,
            )

    def _active_attachments(self) -> list[SessionAttachment]:
        with self._lock:
            return [item for item in self.attachments.values() if not item.closed]

    def _broadcast_event(self, event: dict[str, object]) -> None:
        failed = [item for item in self._active_attachments() if not item.send_event(dict(event))]
        for attachment in failed:
            self.detach(attachment, invoke_close=False)

    def _control_event_locked(self) -> dict[str, object]:
        controller = self.attachments.get(self.controller_attachment_id or "")
        return {
            "type": "control_changed",
            "controllerClientId": controller.client_id if controller else "",
            "controllerClientType": controller.client_type if controller else "",
        }

    def role_for(self, attachment: SessionAttachment) -> str:
        with self._lock:
            return "controller" if self.controller_attachment_id == attachment.attachment_id else "observer"

    def on_output(self, chunk: bytes) -> None:
        if not chunk:
            return
        data = bytes(chunk)
        with self._lock:
            self._touch()
            self.output.extend(data)
            if len(self.output) > self.output_limit:
                del self.output[: len(self.output) - self.output_limit]
            attachments = list(self.attachments.values())
        failed: list[SessionAttachment] = []
        for attachment in attachments:
            for offset in range(0, len(data), MAX_FRAME_BYTES):
                if not attachment.send_binary(data[offset: offset + MAX_FRAME_BYTES]):
                    failed.append(attachment)
                    break
        for attachment in failed:
            self.detach(attachment, invoke_close=False)

    def on_exit(self, code: int) -> None:
        with self._lock:
            if self.ended:
                return
            backend_cwd = getattr(self.backend, "current_cwd", "") if self.backend is not None else ""
            if backend_cwd and os.path.isdir(backend_cwd):
                self.cwd = os.path.abspath(backend_cwd)
            self.ended = True
            self.exit_code = int(code)
            self._touch()
        startup_error = getattr(self.backend, "startup_error_code", None) if self.backend is not None else None
        if startup_error is not None or (self.codex_thread_id and code != 0):
            self._broadcast_event({
                "type": "error",
                "code": "codex_launch_failed",
                "message": f"Codex 终端已退出（{int(startup_error if startup_error is not None else code)}），"
                           "错误详情见上方输出。请重新选择会话；如在其他端运行，请先停止后重试。",
                "codexThreadId": self.codex_thread_id,
                "fatal": True,
            })
        self._broadcast_event({"type": "exit", "code": int(code), "cwd": self.cwd})

    def attach(
        self,
        binary_sink: BinarySink,
        event_sink: EventSink,
        close_sink: Optional[CloseSink] = None,
        *,
        client_id: str = "",
        client_type: str = "legacy",
        cols: int | str | None = None,
        rows: int | str | None = None,
        defer_output: bool = False,
    ) -> tuple[SessionAttachment, bytes, bool]:
        client_id = validate_client_id(client_id)
        client_type = str(client_type or "legacy").strip().lower()
        if client_type not in ("desktop", "android", "legacy"):
            raise ValueError("client_type_invalid")
        attachment_cols, attachment_rows = clamp_dimensions(cols or self.cols, rows or self.rows)
        replaced: list[SessionAttachment] = []
        apply_size = False
        event: dict[str, object] | None = None
        with self._lock:
            had_attachment_before = bool(self.attachments)
            for existing in list(self.attachments.values()):
                same_desktop_slot = client_type == "desktop" and existing.client_type == "desktop"
                same_legacy_slot = client_type == "legacy" and existing.client_type == "legacy"
                same_client = existing.client_id == client_id and existing.client_type == client_type
                if same_desktop_slot or same_legacy_slot or same_client:
                    replaced.append(existing)
                    del self.attachments[existing.attachment_id]
            previous_controller_replaced = any(
                item.attachment_id == self.controller_attachment_id for item in replaced
            )
            self._attachment_generation += 1
            attachment = SessionAttachment(
                binary_sink, event_sink, self._attachment_generation, client_id, client_type,
                attachment_cols, attachment_rows, close_sink, bool(defer_output),
            )
            self.attachments[attachment.attachment_id] = attachment
            if client_type == "desktop":
                self.desktop_state = "connected"
                self.desktop_closed_at = None
            if self.controller_attachment_id is None or previous_controller_replaced:
                self.controller_attachment_id = attachment.attachment_id
                self.cols, self.rows = attachment.cols, attachment.rows
                apply_size = True
                if had_attachment_before:
                    event = self._control_event_locked()
            self._touch()
            snapshot = bytes(self.output)
            ended = self.ended
        for old in replaced:
            old.close()
        if apply_size and self.backend is not None and not self.ended:
            self.backend.resize(attachment.rows, attachment.cols)
        if event is not None:
            self._broadcast_event(event)
        return attachment, snapshot, ended

    def _fallback_controller_locked(self) -> SessionAttachment | None:
        available = [item for item in self.attachments.values() if not item.closed]
        if not available:
            self.controller_attachment_id = None
            return None
        desktops = [item for item in available if item.client_type == "desktop"]
        selected = max(desktops or available, key=lambda item: item.generation)
        self.controller_attachment_id = selected.attachment_id
        self.cols, self.rows = selected.cols, selected.rows
        return selected

    def detach(
        self,
        attachment: Optional[SessionAttachment] = None,
        *,
        invoke_close: bool = True,
        reason: str = "",
    ) -> None:
        changed = False
        fallback: SessionAttachment | None = None
        with self._lock:
            targets = list(self.attachments.values()) if attachment is None else [attachment]
            removed_desktop = False
            for target in targets:
                current = self.attachments.pop(target.attachment_id, None)
                if current is None:
                    continue
                removed_desktop = removed_desktop or current.client_type == "desktop"
                current.close(invoke_close)
                if self.controller_attachment_id == current.attachment_id:
                    changed = True
                    self.controller_attachment_id = None
            if changed:
                fallback = self._fallback_controller_locked()
            if removed_desktop and not any(
                item.client_type == "desktop" and not item.closed for item in self.attachments.values()
            ):
                self.desktop_state = "closed"
                self.desktop_closed_at = time.time()
            self._touch()
            event = self._control_event_locked() if changed else None
        if fallback is not None and self.backend is not None and not self.ended:
            self.backend.resize(fallback.rows, fallback.cols)
        if event is not None:
            self._broadcast_event(event)

    def _claim_control(self, attachment: SessionAttachment) -> tuple[object, dict[str, object] | None]:
        with self._lock:
            current = self.attachments.get(attachment.attachment_id)
            if current is not attachment or attachment.closed:
                raise RuntimeError("attachment_inactive")
            if self.ended or self.backend is None:
                raise RuntimeError("session_ended")
            changed = self.controller_attachment_id != attachment.attachment_id
            self.controller_attachment_id = attachment.attachment_id
            self.cols, self.rows = attachment.cols, attachment.rows
            self._touch()
            backend = self.backend
            event = self._control_event_locked() if changed else None
        if changed:
            backend.resize(attachment.rows, attachment.cols)
        return backend, event

    def write_from(self, attachment: SessionAttachment, data: bytes) -> None:
        if len(data) > MAX_FRAME_BYTES:
            raise ValueError("input_too_large")
        attachment.touch()
        with self._input_lock:
            if self.ended:
                return  # Late terminal replies/input after exit are discarded.
            backend, event = self._claim_control(attachment)
            if event is not None:
                self._broadcast_event(event)
            try:
                backend.write(data)
            except EOFError:
                self.on_exit(1)

    def signal_from(self, attachment: SessionAttachment, name: str) -> None:
        # Validate before claiming control. A malformed observer message must
        # not be able to steal the controller role as a side effect.
        value = _normalise_signal(name)
        attachment.touch()
        with self._input_lock:
            if self.ended:
                return
            backend, event = self._claim_control(attachment)
            if event is not None:
                self._broadcast_event(event)
            if value == "INT":
                backend.send_interrupt()
            else:
                backend.send_eof()

    def resize_from(self, attachment: SessionAttachment, rows: int, cols: int) -> None:
        cols, rows = clamp_dimensions(cols, rows)
        attachment.touch()
        with self._lock:
            current = self.attachments.get(attachment.attachment_id)
            if current is not attachment:
                raise RuntimeError("attachment_inactive")
            attachment.cols, attachment.rows = cols, rows
            is_controller = self.controller_attachment_id == attachment.attachment_id
            if is_controller:
                self.cols, self.rows = cols, rows
            backend = self.backend
            self._touch()
        if is_controller and backend is not None and not self.ended:
            backend.resize(rows, cols)

    # Direct methods preserve the pre-0.2 manager API for local adapters.
    def write(self, data: bytes) -> None:
        if len(data) > MAX_FRAME_BYTES:
            raise ValueError("input_too_large")
        with self._lock:
            if self.ended or self.backend is None:
                raise RuntimeError("session_ended")
            backend = self.backend
            self._touch()
        backend.write(data)

    def resize(self, rows: int, cols: int) -> None:
        cols, rows = clamp_dimensions(cols, rows)
        with self._lock:
            self.cols, self.rows = cols, rows
            backend = self.backend
            self._touch()
        if backend is not None and not self.ended:
            backend.resize(rows, cols)

    def signal(self, name: str) -> None:
        value = _normalise_signal(name)
        with self._lock:
            backend = self.backend
            self._touch()
        if backend is None or self.ended:
            return
        if value == "INT":
            backend.send_interrupt()
        else:
            backend.send_eof()

    def terminate(self, force: bool = False) -> None:
        with self._lock:
            backend = self.backend
            self._touch()
        if backend is not None:
            backend.terminate(force)

    def status(self) -> dict[str, object]:
        with self._lock:
            self._refresh_desktop_state_locked()
            controller = self.attachments.get(self.controller_attachment_id or "")
            attachments = [
                {
                    "clientId": item.client_id,
                    "clientType": item.client_type,
                    "role": "controller" if item is controller else "observer",
                    "cols": item.cols,
                    "rows": item.rows,
                    "lastReceivedAt": utc_timestamp(item.last_received_wall),
                }
                for item in self.attachments.values() if not item.closed
            ]
            return {
                "sessionId": self.session_id,
                "profileId": self.profile_id,
                "codexThreadId": self.codex_thread_id or None,
                "pid": self.pid,
                "state": "exited" if self.ended else "running",
                "running": not self.ended,
                "exitCode": self.exit_code,
                "cwd": self.cwd,
                "createdAt": utc_timestamp(self.created_wall),
                "lastActivityAt": utc_timestamp(self.last_activity_wall),
                "lastOpenedAt": self.last_opened_at,
                "desktopConnected": any(item["clientType"] == "desktop" for item in attachments),
                "desktopState": self.desktop_state,
                "desktopClosedAt": (
                    utc_timestamp(self.desktop_closed_at) if self.desktop_closed_at is not None else None
                ),
                "controller": None if controller is None else {
                    "clientId": controller.client_id,
                    "clientType": controller.client_type,
                },
                "attachmentCount": len(attachments),
                "attachments": attachments,
            }

    def close(self) -> None:
        with self._lock:
            backend = self.backend
            attachments = list(self.attachments.values())
            had_desktop = any(item.client_type == "desktop" and not item.closed for item in attachments)
            self.attachments.clear()
            self.controller_attachment_id = None
            if had_desktop:
                self.desktop_state = "closed"
                self.desktop_closed_at = time.time()
        for attachment in attachments:
            attachment.close()
        if backend is not None:
            backend.close()


class SessionManager:
    """Owns all PTYs and implements bounded, reconnectable session state."""

    def __init__(
        self,
        max_sessions: int = 16,
        idle_seconds: int = 1800,
        backend_factory: Callable[..., PtyBackend] = spawn_pty,
        output_limit: int = MAX_OUTPUT_BYTES,
        desktop_timeout_seconds: float = DESKTOP_ATTACHMENT_TIMEOUT_SECONDS,
    ) -> None:
        self.max_sessions = max(1, int(max_sessions))
        self.idle_seconds = max(1, int(idle_seconds))
        self.backend_factory = backend_factory
        self.output_limit = output_limit
        self.desktop_timeout_seconds = max(0.1, float(desktop_timeout_seconds))
        self.sessions: dict[str, PtySession] = {}
        self._lock = threading.RLock()
        self._stop_reaper = threading.Event()
        self._reaper = threading.Thread(target=self._reap_loop, name="relayterm-reaper", daemon=True)
        self._reaper.start()

    def _reap_loop(self) -> None:
        interval = min(
            60.0,
            max(0.25, min(self.idle_seconds / 2.0, self.desktop_timeout_seconds / 3.0)),
        )
        while not self._stop_reaper.wait(interval):
            try:
                self.reap_idle()
            except Exception:
                pass

    def reap_idle(self) -> list[str]:
        now = time.monotonic()
        removed: list[tuple[str, PtySession]] = []
        with self._lock:
            for session_id, session in list(self.sessions.items()):
                session.reap_stale_desktop_attachments(self.desktop_timeout_seconds)
                with session._lock:
                    idle = not session.attachments and now - session.last_activity >= self.idle_seconds
                if idle:
                    removed.append((session_id, session))
                    del self.sessions[session_id]
        for _, session in removed:
            session.close()
        return [session_id for session_id, _ in removed]

    def open_session(
        self,
        session_id: str,
        startup_command: str | PtyLaunchSpec = "codex",
        cwd: str = "",
        cols: int = 100,
        rows: int = 32,
        resume: bool = True,
        *,
        profile_id: str = "",
        start_immediately: bool = True,
    ) -> tuple[PtySession, bool]:
        session_id = validate_session_id(session_id)
        self.reap_idle()
        with self._lock:
            existing = self.sessions.get(session_id)
            if existing is not None:
                if not resume:
                    existing.close()
                    del self.sessions[session_id]
                else:
                    return existing, True
            if len(self.sessions) >= self.max_sessions:
                raise RuntimeError("max_sessions")
            launch_cwd = startup_command.cwd if isinstance(startup_command, PtyLaunchSpec) else cwd
            session = PtySession(
                session_id, startup_command, launch_cwd, cols, rows,
                self.backend_factory, self.output_limit, profile_id, start_immediately,
            )
            self.sessions[session_id] = session
            return session, False

    def get(self, session_id: str) -> Optional[PtySession]:
        with self._lock:
            return self.sessions.get(session_id)

    def get_by_profile(self, profile_id: str) -> Optional[PtySession]:
        with self._lock:
            for session in self.sessions.values():
                if session.profile_id == profile_id:
                    return session
        return None

    def list_status(self, recent_activity: dict[str, object] | None = None) -> list[dict[str, object]]:
        with self._lock:
            sessions = list(self.sessions.values())
        values = [item.status() for item in sessions]
        if recent_activity is not None:
            for item in values:
                profile_id = str(item.get("profileId", ""))
                if profile_id:
                    item["lastOpenedAt"] = recent_activity.get(profile_id, item.get("lastOpenedAt"))
        return sorted(values, key=lambda item: str(item["lastActivityAt"]), reverse=True)

    def create_or_resume(self, *args, **kwargs) -> tuple[PtySession, bool]:
        return self.open_session(*args, **kwargs)

    def attach(self, session_id: str, binary_sink: BinarySink, event_sink: EventSink,
               close_sink: Optional[CloseSink] = None, **kwargs):
        session = self.get(validate_session_id(session_id))
        if session is None:
            raise KeyError("session_not_found")
        return session.attach(binary_sink, event_sink, close_sink, **kwargs)

    def write(self, session_id: str, data: bytes) -> None:
        session = self.get(validate_session_id(session_id))
        if session is None:
            raise KeyError("session_not_found")
        session.write(data)

    def resize(self, session_id: str, rows: int, cols: int) -> None:
        session = self.get(validate_session_id(session_id))
        if session is None:
            raise KeyError("session_not_found")
        session.resize(rows, cols)

    def signal(self, session_id: str, name: str) -> None:
        session = self.get(validate_session_id(session_id))
        if session is None:
            raise KeyError("session_not_found")
        session.signal(name)

    def terminate_profile(self, profile_id: str, force: bool = True) -> bool:
        session = self.get_by_profile(profile_id)
        if session is None:
            return False
        session.terminate(force)
        return True

    def remove_profile(self, profile_id: str, terminate: bool = True) -> bool:
        session = self.get_by_profile(profile_id)
        if session is None:
            return False
        return self.remove_instance(session, terminate=terminate)

    def remove(self, session_id: str, terminate: bool = True) -> bool:
        with self._lock:
            session = self.sessions.pop(session_id, None)
        if session is None:
            return False
        if terminate:
            session.close()
        return True

    def remove_instance(self, session: PtySession, terminate: bool = True) -> bool:
        """Remove only *session* when it is still the manager's current object.

        Open/attach failures can happen after a session has been inserted into
        the manager. Identity checking prevents cleanup from deleting a newer
        session that reused the same id in the meantime.
        """
        with self._lock:
            if self.sessions.get(session.session_id) is not session:
                return False
            del self.sessions[session.session_id]
        if terminate:
            session.close()
        return True

    def shutdown(self) -> None:
        self._stop_reaper.set()
        with self._lock:
            sessions = list(self.sessions.values())
            self.sessions.clear()
        for session in sessions:
            session.close()
