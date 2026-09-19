#!/usr/bin/env python3
"""RelayTerm HTTP catalog, pairing, legacy exec and shared PTY bridge."""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import socket
import subprocess
import threading
import time
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

try:
    from .codex_sessions import (
        CodexAppServerClient,
        CodexBindingStore,
        CodexSessionError,
        CodexSessionService,
        build_codex_resume_command,
    )
    from .profile_catalog import Profile, ProfileStore
    from .pty_backend import PtyLaunchSpec
    from .session_manager import (
        MAX_FRAME_BYTES,
        PtySession,
        SessionAttachment,
        SessionManager,
        replay_snapshot,
        validate_session_id,
    )
except ImportError:  # direct ``python bridge/relay_bridge.py`` invocation
    from codex_sessions import (  # type: ignore
        CodexAppServerClient,
        CodexBindingStore,
        CodexSessionError,
        CodexSessionService,
        build_codex_resume_command,
    )
    from profile_catalog import Profile, ProfileStore  # type: ignore
    from pty_backend import PtyLaunchSpec  # type: ignore
    from session_manager import (  # type: ignore
        MAX_FRAME_BYTES,
        PtySession,
        SessionAttachment,
        SessionManager,
        replay_snapshot,
        validate_session_id,
    )


HOST = os.environ.get("RELAYTERM_HOST", "127.0.0.1")
PORT = int(os.environ.get("RELAYTERM_PORT", "18765"))
TOKEN = os.environ.get("RELAYTERM_TOKEN", "")
MAX_SESSIONS = int(os.environ.get("RELAYTERM_MAX_SESSIONS", "16"))
IDLE_SECONDS = int(os.environ.get("RELAYTERM_IDLE_SECONDS", "1800"))
TIMEOUT_SECONDS = 30
BRIDGE_GENERATION = os.environ.get("RELAYTERM_BRIDGE_GENERATION", "codex-resume-v3")
DRAIN_STATE_PATH = os.environ.get("RELAYTERM_DRAIN_STATE_PATH", "")
try:
    DESKTOP_TIMEOUT_SECONDS = max(
        0.1,
        float(os.environ.get(
            "RELAYTERM_DESKTOP_TIMEOUT_SECONDS",
            os.environ.get("RELAYTERM_DESKTOP_HEARTBEAT_TIMEOUT", "15"),
        )),
    )
except (TypeError, ValueError):
    DESKTOP_TIMEOUT_SECONDS = 15.0


def _default_profile_path() -> Path:
    configured = os.environ.get("RELAYTERM_PROFILE_PATH")
    if configured:
        return Path(configured)
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
        return root / "RelayTerm" / "profiles.json"
    return Path.home() / ".relayterm" / "profiles.json"


PROFILE_STORE = ProfileStore(_default_profile_path(), os.environ.get("RELAYTERM_INITIAL_CWD", os.getcwd()))
SESSION_MANAGER = SessionManager(
    MAX_SESSIONS, IDLE_SECONDS, desktop_timeout_seconds=DESKTOP_TIMEOUT_SECONDS,
)
CODEX_APP_SERVER = CodexAppServerClient()
_CODEX_SERVICE: CodexSessionService | None = None
_CODEX_SERVICE_KEY: tuple[int, int, int, str] | None = None
_CODEX_SERVICE_LOCK = threading.Lock()

# The legacy endpoint intentionally keeps its independent cwd model.
SESSION_CWDS: dict[str, str] = {}
SESSION_LOCKS: dict[str, threading.Lock] = {}
SESSION_GUARD = threading.Lock()


def recent_activity_store():
    """Resolve the store lazily so tests/adapters can replace PROFILE_STORE."""
    store = getattr(PROFILE_STORE, "recent_activity", None)
    if store is None:
        return None
    return store


def draining_profile_ids() -> set[str]:
    if not DRAIN_STATE_PATH:
        return set()
    try:
        value = json.loads(Path(DRAIN_STATE_PATH).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        return set()
    drains = value.get("drains", []) if isinstance(value, dict) else []
    result: set[str] = set()
    if not isinstance(drains, list):
        return result
    for item in drains:
        ids = item.get("profileIds", []) if isinstance(item, dict) else []
        if isinstance(ids, list):
            result.update(str(profile_id) for profile_id in ids)
    return result


def codex_session_service(manager: SessionManager | None = None) -> CodexSessionService:
    """Resolve globals lazily so tests and adapters can replace their stores."""
    active = manager or SESSION_MANAGER
    binding_path = str(Path(PROFILE_STORE.path).with_name("codex_bindings.json"))
    key = (id(PROFILE_STORE), id(active), id(CODEX_APP_SERVER), binding_path)
    global _CODEX_SERVICE, _CODEX_SERVICE_KEY
    with _CODEX_SERVICE_LOCK:
        if _CODEX_SERVICE is None or _CODEX_SERVICE_KEY != key:
            _CODEX_SERVICE = CodexSessionService(
                CODEX_APP_SERVER, CodexBindingStore(binding_path), PROFILE_STORE, active,
            )
            _CODEX_SERVICE_KEY = key
        return _CODEX_SERVICE


def record_profile_open(session: PtySession | None) -> str | None:
    """Record a successful profile open and mirror it onto the live session."""
    activity_store = recent_activity_store()
    if activity_store is None:
        return None
    if session is None or not session.profile_id or session.ended:
        value = activity_store.get(session.profile_id) if session and session.profile_id else None
        if session is not None:
            session.set_last_opened_at(value)
        return value
    try:
        value = activity_store.record(session.profile_id)
    except Exception:
        # Activity is advisory; a read-only/corrupt activity file must not
        # prevent a terminal from opening.
        try:
            value = activity_store.get(session.profile_id)
        except Exception:
            value = None
    session.set_last_opened_at(value)
    return value


def session_statuses() -> list[dict[str, object]]:
    """Return session status enriched from the shared activity file."""
    activity_store = recent_activity_store()
    activity = activity_store.load() if activity_store is not None else {}
    values = SESSION_MANAGER.list_status()
    for item in values:
        profile_id = str(item.get("profileId", ""))
        if profile_id:
            item["lastOpenedAt"] = activity.get(profile_id, item.get("lastOpenedAt"))
    return values


def shell_command(command: str) -> list[str]:
    if os.name == "nt":
        return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", command]
    return ["/bin/sh", "-c", command]


def session_lock(session_id: str) -> threading.Lock:
    with SESSION_GUARD:
        return SESSION_LOCKS.setdefault(session_id, threading.Lock())


def wrapped_command(command: str, marker: str) -> str:
    if os.name == "nt":
        return (
            f"{command}\r\nset \"__relayterm_code=%ERRORLEVEL%\"\r\ncd\r\n"
            f"echo {marker}%CD%\r\nexit /b %__relayterm_code%\r\n"
        )
    return (
        f"{command}\n__relayterm_code=$?\n"
        f"printf '\\n{marker}%s\\n' \"$PWD\"\nexit $__relayterm_code\n"
    )


def strip_cwd_marker(stdout: str, marker: str, fallback: str) -> tuple[str, str]:
    lines = stdout.splitlines(keepends=True)
    kept: list[str] = []
    cwd = fallback
    for line in lines:
        candidate = line.strip("\r\n")
        if candidate.startswith(marker):
            value = candidate[len(marker):].strip()
            if value:
                cwd = os.path.abspath(value)
            continue
        kept.append(line)
    return "".join(kept), cwd


def direct_cd(command: str, cwd: str) -> tuple[str, str, int, str] | None:
    pattern = r"\s*cd(?:\s+/d)?(?:\s+(.*?))?\s*" if os.name == "nt" else r"\s*cd(?:\s+(.*?))?\s*"
    match = re.fullmatch(pattern, command, flags=re.IGNORECASE)
    if not match:
        return None
    raw = (match.group(1) or "").strip().strip('"')
    if not raw:
        return "", "", 0, cwd
    target = os.path.abspath(os.path.join(cwd, os.path.expanduser(raw)))
    if not os.path.isdir(target):
        return "", f"目录不存在: {raw}\n", 1, cwd
    return "", "", 0, target


def execute_legacy(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    command = payload.get("command", "")
    if not isinstance(command, str) or not command.strip():
        return 400, {"error": "command_required"}
    if len(command) > 8192:
        return 413, {"error": "command_too_long"}
    session_id = str(payload.get("sessionId", "default"))[:128] or "default"
    try:
        with session_lock(session_id):
            cwd = SESSION_CWDS.get(session_id, os.getcwd())
            if not os.path.isdir(cwd):
                cwd = os.getcwd()
            changed = direct_cd(command, cwd)
            if changed is not None:
                stdout, stderr, exit_code, cwd = changed
                SESSION_CWDS[session_id] = cwd
                return 200, {"stdout": stdout, "stderr": stderr, "exitCode": exit_code, "cwd": cwd}
            marker = "__RELAYTERM_CWD_" + uuid.uuid4().hex + "__"
            completed = subprocess.run(
                shell_command(wrapped_command(command, marker)), cwd=cwd, capture_output=True,
                text=True, timeout=TIMEOUT_SECONDS, check=False,
            )
            stdout, cwd = strip_cwd_marker(completed.stdout, marker, cwd)
            SESSION_CWDS[session_id] = cwd
        return 200, {
            "stdout": stdout, "stderr": completed.stderr,
            "exitCode": completed.returncode, "cwd": cwd,
        }
    except subprocess.TimeoutExpired:
        return 408, {"error": "command_timeout"}


def json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def is_authorized(value: str, token: str | None = None) -> bool:
    expected_token = TOKEN if token is None else token
    return not expected_token or hmac.compare_digest(value, "Bearer " + expected_token)


class PairingManager:
    """Memory-only, one-use pairing challenges."""

    def __init__(self, lifetime_seconds: int = 120) -> None:
        self.lifetime_seconds = max(1, int(lifetime_seconds))
        self._items: dict[str, tuple[float, str]] = {}
        self._lock = threading.Lock()

    def _purge_locked(self) -> None:
        now = time.monotonic()
        for code, (expires, _) in list(self._items.items()):
            if expires <= now:
                del self._items[code]

    def create(self, endpoint: str) -> tuple[str, int]:
        endpoint = str(endpoint or "").strip().rstrip("/")
        parsed = urlsplit(endpoint)
        if parsed.scheme not in ("https", "http") or not parsed.netloc:
            raise ValueError("pairing_endpoint_invalid")
        code = base64.urlsafe_b64encode(secrets.token_bytes(16)).rstrip(b"=").decode("ascii")
        with self._lock:
            self._purge_locked()
            self._items[code] = (time.monotonic() + self.lifetime_seconds, endpoint)
        return code, self.lifetime_seconds

    def inspect(self, code: str) -> str | None:
        with self._lock:
            self._purge_locked()
            item = self._items.get(str(code))
            return item[1] if item else None

    def exchange(self, code: str) -> str | None:
        with self._lock:
            self._purge_locked()
            item = self._items.pop(str(code), None)
            return item[1] if item else None


PAIRING_MANAGER = PairingManager()


def profile_for_open(profile_id: str) -> Profile:
    profile = PROFILE_STORE.get(profile_id)
    if profile is None or not profile.enabled:
        raise ValueError("profile_not_found")
    if not os.path.isdir(profile.working_directory):
        raise ValueError("profile_directory_missing")
    return profile


def open_spec(
    message: dict[str, Any], query_session_id: str = "", manager: SessionManager | None = None,
) -> tuple[str, str, str | PtyLaunchSpec, str]:
    profile_id = str(message.get("profileId", "")).strip()
    if profile_id:
        active = manager or SESSION_MANAGER
        if active.get_by_profile(profile_id) is None and profile_id in draining_profile_ids():
            raise CodexSessionError(
                "profile_draining",
                "该项目仍在旧版 bridge 中运行，请连接现有终端，排空后会自动转入新版",
                status=409,
            )
        profile = profile_for_open(profile_id)
        if profile.launch_mode == "codex":
            thread_id = codex_session_service(active).resolve(profile, message.get("codexThreadId"))
            current = active.get_by_profile(profile.id)
            if current is not None and not current.ended:
                return profile.id, profile.id, current.launch_spec, current.cwd
            command, marker = build_codex_resume_command(profile.shell, thread_id, profile.codex_args)
            spec = PtyLaunchSpec.profile(
                profile.shell, profile.working_directory, command,
                codex_thread_id=thread_id, failure_marker=marker,
            )
        else:
            if message.get("codexThreadId") not in (None, ""):
                raise CodexSessionError("profile_not_codex", status=409)
            spec = PtyLaunchSpec.profile(profile.shell, profile.working_directory, profile.startup_command)
        return profile.id, profile.id, spec, profile.working_directory
    session_id = str(message.get("sessionId", "") or query_session_id)
    return session_id, "", str(message.get("startupCommand", "codex")), str(message.get("cwd", ""))


def websocket_error(exc: BaseException) -> dict[str, object]:
    if isinstance(exc, CodexSessionError):
        return {"type": "error", "code": exc.code, "message": exc.message, **exc.details}
    return {"type": "error", "code": str(exc), "message": str(exc)}


def pairing_page(code: str, endpoint: str) -> bytes:
    deep_link = "relayterm://pair?" + urlencode({"endpoint": endpoint, "challenge": code})
    document = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>RelayTerm 配对</title><style>
body{{font-family:system-ui,sans-serif;margin:0;background:#101418;color:#eef2f3;display:grid;place-items:center;min-height:100vh}}
main{{width:min(32rem,calc(100% - 2rem))}}h1{{font-size:1.5rem}}p{{color:#aeb8bd;line-height:1.6}}
a{{display:inline-block;background:#39b894;color:#07110e;padding:.8rem 1rem;text-decoration:none;border-radius:6px;font-weight:650}}
</style></head><body><main><h1>RelayTerm</h1><p>在 Android 设备上打开项目目录。</p>
<a href="{html.escape(deep_link, quote=True)}">打开 RelayTerm</a></main></body></html>"""
    return document.encode("utf-8")


class WebSocketProtocolError(Exception):
    pass


def _cleanup_open_failure(
    manager: SessionManager,
    session: PtySession | None,
    attachment: SessionAttachment | None,
    resumed: bool,
) -> None:
    """Rollback a session inserted before its PTY transport finished opening."""
    if session is None:
        return
    if attachment is not None:
        session.detach(attachment, invoke_close=False)
    # A newly-created deferred session has no useful state when attach/start
    # fails. Remove it instead of exposing a running/pid=0 ghost in /sessions.
    if not resumed or session.backend is None:
        manager.remove_instance(session)


def _read_exact(stream: Any, size: int) -> bytes:
    result = bytearray()
    while len(result) < size:
        chunk = stream.read(size - len(result))
        if not chunk:
            raise EOFError()
        result.extend(chunk)
    return bytes(result)


def _send_ws_frame(stream: Any, opcode: int, payload: bytes) -> None:
    payload = bytes(payload)
    length = len(payload)
    if length > MAX_FRAME_BYTES:
        raise WebSocketProtocolError("frame_too_large")
    first = 0x80 | (opcode & 0x0F)
    if length < 126:
        header = bytes((first, length))
    elif length <= 0xFFFF:
        header = bytes((first, 126)) + length.to_bytes(2, "big")
    else:
        header = bytes((first, 127)) + length.to_bytes(8, "big")
    stream.write(header + payload)
    stream.flush()


class _WsWriter:
    MAX_QUEUE_ITEMS = 128
    MAX_QUEUE_BYTES = 1024 * 1024

    def __init__(self, stream: Any) -> None:
        self.stream = stream
        self.queue: deque[tuple[int, bytes]] = deque()
        self.queue_bytes = 0
        self.condition = threading.Condition()
        self.closed = False
        self.thread = threading.Thread(target=self._run, name="relayterm-ws-writer", daemon=True)
        self.thread.start()

    def _enqueue(self, opcode: int, payload: bytes, priority: bool = False) -> bool:
        payload = bytes(payload)
        with self.condition:
            if self.closed:
                return False
            if len(self.queue) >= self.MAX_QUEUE_ITEMS or self.queue_bytes + len(payload) > self.MAX_QUEUE_BYTES:
                self.queue.clear()
                self.queue_bytes = 0
                marker = json_bytes({"type": "resync_required"})
                self.queue.append((0x1, marker))
                self.queue.append((0x8, b""))
                self.queue_bytes = len(marker)
                self.closed = True
                self.condition.notify_all()
                return False
            if priority:
                self.queue.appendleft((opcode, payload))
            else:
                self.queue.append((opcode, payload))
            self.queue_bytes += len(payload)
            self.condition.notify()
            return True

    def send_binary(self, payload: bytes) -> bool:
        ok = True
        for offset in range(0, len(payload), MAX_FRAME_BYTES):
            ok = self._enqueue(0x2, payload[offset: offset + MAX_FRAME_BYTES]) and ok
        return ok

    def send_event(self, event: dict[str, object]) -> bool:
        return self._enqueue(0x1, json_bytes(event))

    def send_event_priority(self, event: dict[str, object]) -> bool:
        return self._enqueue(0x1, json_bytes(event), True)

    def _run(self) -> None:
        while True:
            with self.condition:
                while not self.queue and not self.closed:
                    self.condition.wait()
                if self.closed and not self.queue:
                    return
                opcode, payload = self.queue.popleft()
                self.queue_bytes -= len(payload)
            try:
                _send_ws_frame(self.stream, opcode, payload)
            except Exception:
                with self.condition:
                    self.closed = True
                    self.queue.clear()
                    self.queue_bytes = 0
                    self.condition.notify_all()
                return

    def close(self, send_close: bool = False) -> None:
        with self.condition:
            if not self.closed:
                if send_close:
                    self.queue.append((0x8, b""))
                self.closed = True
                self.condition.notify_all()
            thread = self.thread
        if thread is not threading.current_thread():
            thread.join(timeout=1)


class RelayHandler(BaseHTTPRequestHandler):
    server_version = "RelayTermBridge/2.0"
    protocol_version = "HTTP/1.1"

    def handle(self) -> None:
        try:
            super().handle()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError):
            pass

    def log_message(self, fmt: str, *args: Any) -> None:
        # Request paths are safe to log; headers and bodies may contain secrets.
        message = fmt % args
        message = re.sub(r"/pair/[A-Za-z0-9_-]+", "/pair/[redacted]", message)
        print("[relay] " + message, flush=True)

    def send_json(self, status: int, value: Any) -> None:
        body = json_bytes(value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def authorized(self) -> bool:
        return is_authorized(self.headers.get("Authorization", ""))

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length < 0 or length > 1024 * 1024:
            raise OverflowError("payload_too_large")
        value = json.loads(self.rfile.read(length))
        if not isinstance(value, dict):
            raise ValueError("object_required")
        return value

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        if parsed.path == "/health":
            self.send_json(200, {
                "ok": True, "service": "relayterm",
                "bridgeGeneration": BRIDGE_GENERATION,
            })
            return
        if parsed.path.startswith("/pair/"):
            code = parsed.path[len("/pair/"):]
            endpoint = PAIRING_MANAGER.inspect(code)
            if endpoint is None:
                self.send_html(410, b"<!doctype html><title>RelayTerm</title><p>Pairing expired.</p>")
            else:
                self.send_html(200, pairing_page(code, endpoint))
            return
        if parsed.path == "/v1/pty":
            self._handle_websocket(parse_qs(parsed.query).get("sessionId", [""])[0])
            return
        codex_sessions = re.fullmatch(r"/v1/profiles/([^/]+)/codex-sessions", parsed.path)
        if codex_sessions is not None:
            if not self.authorized():
                self.send_json(401, {"error": "unauthorized"})
                return
            try:
                profile_id = validate_session_id(unquote(codex_sessions.group(1)))
                self.send_json(200, codex_session_service().sessions(profile_id))
            except CodexSessionError as exc:
                self.send_json(exc.status, exc.payload())
            except Exception as exc:
                self.send_json(500, {"error": str(exc)})
            return
        if parsed.path in ("/v1/profiles", "/v1/sessions"):
            if not self.authorized():
                self.send_json(401, {"error": "unauthorized"})
                return
            try:
                value = PROFILE_STORE.catalog() if parsed.path == "/v1/profiles" else {
                    "sessions": session_statuses()
                }
                self.send_json(200, value)
            except Exception as exc:
                self.send_json(500, {"error": str(exc)})
            return
        self.send_json(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        terminate = re.fullmatch(r"/v1/sessions/([^/]+)/terminate", path)
        codex_create = re.fullmatch(r"/v1/profiles/([^/]+)/codex-sessions", path)
        public_exchange = path == "/v1/pairing/exchange"
        if (path not in ("/v1/exec", "/v1/pairing/challenges", "/v1/pairing/exchange")
                and terminate is None and codex_create is None):
            self.send_json(404, {"error": "not_found"})
            return
        if not public_exchange and not self.authorized():
            self.send_json(401, {"error": "unauthorized"})
            return
        try:
            payload = self.read_json()
            if path == "/v1/exec":
                status, value = execute_legacy(payload)
                self.send_json(status, value)
            elif path == "/v1/pairing/challenges":
                code, expires = PAIRING_MANAGER.create(str(payload.get("endpoint", "")))
                self.send_json(201, {"challenge": code, "expiresIn": expires})
            elif public_exchange:
                endpoint = PAIRING_MANAGER.exchange(str(payload.get("challenge", "")))
                if endpoint is None:
                    self.send_json(410, {"error": "pairing_expired"})
                else:
                    catalog = PROFILE_STORE.catalog()
                    self.send_json(200, {
                        "endpoint": endpoint, "token": TOKEN,
                        "profiles": catalog["profiles"], "revision": catalog["revision"],
                    })
            elif terminate is not None:
                profile_id = validate_session_id(unquote(terminate.group(1)))
                if bool(payload.get("remove", False)):
                    found = SESSION_MANAGER.remove_profile(profile_id, terminate=True)
                else:
                    found = SESSION_MANAGER.terminate_profile(profile_id, bool(payload.get("force", True)))
                self.send_json(200 if found else 404, {
                    "ok": found, "profileId": profile_id,
                    **({} if found else {"error": "session_not_found"}),
                })
            elif codex_create is not None:
                profile_id = validate_session_id(unquote(codex_create.group(1)))
                value = codex_session_service().create(
                    profile_id, lock=bool(payload.get("lock", True)),
                )
                self.send_json(201, value)
        except OverflowError:
            self.send_json(413, {"error": "payload_too_large"})
        except CodexSessionError as exc:
            self.send_json(exc.status, exc.payload())
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json(400, {"error": str(exc) or "invalid_json"})
        except Exception as exc:
            self.send_json(500, {"error": str(exc)})

    def do_PUT(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        binding = re.fullmatch(r"/v1/profiles/([^/]+)/codex-binding", path)
        if binding is None:
            self.send_json(404, {"error": "not_found"})
            return
        if not self.authorized():
            self.send_json(401, {"error": "unauthorized"})
            return
        try:
            payload = self.read_json()
            profile_id = validate_session_id(unquote(binding.group(1)))
            self.send_json(200, codex_session_service().set_binding(profile_id, payload))
        except OverflowError:
            self.send_json(413, {"error": "payload_too_large"})
        except CodexSessionError as exc:
            self.send_json(exc.status, exc.payload())
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json(400, {"error": str(exc) or "invalid_json"})
        except Exception as exc:
            self.send_json(500, {"error": str(exc)})

    def _handle_websocket(self, query_session_id: str) -> None:
        if not self.authorized():
            self.send_json(401, {"error": "unauthorized"})
            return
        if self.headers.get("Upgrade", "").lower() != "websocket":
            self.send_json(426, {"error": "websocket_upgrade_required"})
            return
        key = self.headers.get("Sec-WebSocket-Key", "")
        try:
            if self.headers.get("Sec-WebSocket-Version", "13") != "13" or len(base64.b64decode(key, validate=True)) != 16:
                raise ValueError()
            accept = base64.b64encode(hashlib.sha1(
                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
            ).digest()).decode("ascii")
        except (UnicodeError, ValueError):
            self.send_json(400, {"error": "websocket_handshake_invalid"})
            return
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.close_connection = True
        self.connection.settimeout(None)
        writer = _WsWriter(self.wfile)
        session: Optional[PtySession] = None
        attachment: Optional[SessionAttachment] = None
        opened = False
        fragmented_opcode: Optional[int] = None
        fragmented = bytearray()
        try:
            while True:
                first, second = _read_exact(self.rfile, 2)
                fin, opcode, masked = bool(first & 0x80), first & 0x0F, bool(second & 0x80)
                if first & 0x70:
                    raise WebSocketProtocolError("reserved_bits_invalid")
                length = second & 0x7F
                if length == 126:
                    length = int.from_bytes(_read_exact(self.rfile, 2), "big")
                elif length == 127:
                    length = int.from_bytes(_read_exact(self.rfile, 8), "big")
                if length > MAX_FRAME_BYTES or not masked:
                    raise WebSocketProtocolError("frame_invalid")
                mask = _read_exact(self.rfile, 4)
                raw = bytearray(_read_exact(self.rfile, length))
                for index in range(length):
                    raw[index] ^= mask[index % 4]
                payload_bytes = bytes(raw)
                if attachment is not None and session is not None:
                    # Count text/binary and WebSocket control frames as
                    # liveness signals for the desktop attachment.
                    session.touch_attachment(attachment)
                if opcode == 0x8:
                    if not fin or length > 125:
                        raise WebSocketProtocolError("control_frame_invalid")
                    break
                if opcode == 0x9:
                    if not fin or length > 125:
                        raise WebSocketProtocolError("control_frame_invalid")
                    writer._enqueue(0xA, payload_bytes)
                    continue
                if opcode == 0xA:
                    if not fin or length > 125:
                        raise WebSocketProtocolError("control_frame_invalid")
                    continue
                if opcode == 0x0:
                    if fragmented_opcode is None:
                        raise WebSocketProtocolError("fragment_invalid")
                    if len(payload_bytes) > MAX_FRAME_BYTES - len(fragmented):
                        raise WebSocketProtocolError("frame_too_large")
                    fragmented.extend(payload_bytes)
                    if not fin:
                        continue
                    opcode, payload_bytes = fragmented_opcode, bytes(fragmented)
                    fragmented_opcode, fragmented = None, bytearray()
                elif opcode in (0x1, 0x2):
                    if fragmented_opcode is not None:
                        raise WebSocketProtocolError("fragment_invalid")
                    if not fin:
                        fragmented_opcode = opcode
                        fragmented.extend(payload_bytes)
                        continue
                # A heartbeat/control frame is still proof that the desktop
                # transport is alive.  Touch before dispatching the payload so
                # malformed application messages cannot keep a dead socket.
                if attachment is not None and opcode in (0x1, 0x2):
                    session.touch_attachment(attachment)
                if opcode == 0x2:
                    if not opened or session is None or attachment is None:
                        writer.send_event({"type": "error", "code": "not_open", "message": "open_required"})
                        continue
                    try:
                        session.write_from(attachment, payload_bytes)
                    except Exception as exc:
                        writer.send_event({"type": "error", "code": str(exc), "message": str(exc)})
                    continue
                if opcode != 0x1:
                    raise WebSocketProtocolError("opcode_invalid")
                try:
                    message = json.loads(payload_bytes.decode("utf-8"))
                    if not isinstance(message, dict):
                        raise ValueError("object_required")
                except Exception as exc:
                    writer.send_event({"type": "error", "code": "invalid_json", "message": str(exc)})
                    continue
                message_type = str(message.get("type", ""))
                if message_type == "open":
                    if opened:
                        writer.send_event({"type": "error", "code": "already_open", "message": "already_open"})
                        continue
                    resumed = False
                    try:
                        sid, profile_id, spec, cwd = open_spec(message, query_session_id, SESSION_MANAGER)
                        session, resumed = SESSION_MANAGER.open_session(
                            sid, spec, cwd, message.get("cols", 100), message.get("rows", 32),
                            bool(message.get("resume", True)), profile_id=profile_id,
                            start_immediately=False,
                        )
                        client_type = str(message.get("clientType", "legacy"))
                        attachment, snapshot, ended = session.attach(
                            writer.send_binary, writer.send_event,
                            lambda: self.connection.shutdown(socket.SHUT_RDWR),
                            client_id=str(message.get("clientId", "")),
                            client_type=client_type,
                            cols=message.get("cols", 100), rows=message.get("rows", 32),
                            defer_output=True,
                        )
                        session.ensure_started()
                        ended = session.ended
                        opened = True
                        last_opened_at = record_profile_open(session)
                        writer.send_event_priority({
                            "type": "ready", "sessionId": session.session_id,
                            "profileId": session.profile_id, "pid": session.pid,
                            "resumed": bool(resumed), "role": session.role_for(attachment),
                            "lastOpenedAt": last_opened_at,
                            "codexThreadId": session.codex_thread_id or None,
                        })
                        snapshot = replay_snapshot(snapshot, client_type, bool(resumed))
                        if not attachment.activate_output(snapshot):
                            raise RuntimeError("attachment_send_failed")
                        if ended:
                            writer.send_event({"type": "exit", "code": int(session.exit_code or 0), "cwd": session.cwd})
                    except Exception as exc:
                        _cleanup_open_failure(SESSION_MANAGER, session, attachment, resumed)
                        attachment = None
                        writer.send_event(websocket_error(exc))
                        break
                elif message_type == "resize" and session is not None and attachment is not None:
                    try:
                        session.resize_from(attachment, message.get("rows", 32), message.get("cols", 100))
                    except Exception as exc:
                        writer.send_event({"type": "error", "code": str(exc), "message": str(exc)})
                elif message_type == "signal" and session is not None and attachment is not None:
                    try:
                        session.signal_from(attachment, str(message.get("name", "")))
                    except Exception as exc:
                        writer.send_event({"type": "error", "code": str(exc), "message": str(exc)})
                elif message_type in ("ping", "heartbeat"):
                    writer.send_event({"type": "pong"})
                elif message_type == "close":
                    if session is not None:
                        if bool(message.get("terminate", False)):
                            session.terminate(True)
                        if attachment is not None:
                            reason = str(message.get("reason", ""))
                            session.detach(attachment, invoke_close=False, reason=reason)
                            attachment = None
                    break
                else:
                    writer.send_event({"type": "error", "code": "message_invalid", "message": "unknown_type"})
        except (EOFError, OSError, WebSocketProtocolError):
            pass
        finally:
            if session is not None and attachment is not None:
                session.detach(attachment, invoke_close=False)
            writer.close(send_close=True)
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self.connection.close()
            except OSError:
                pass


class RelayHTTPServer(ThreadingHTTPServer):
    """Bounded request workers so long-running bridges cannot exhaust threads."""

    request_queue_size = 128

    def __init__(self, server_address, handler_class, *, max_workers: int = 16):
        super().__init__(server_address, handler_class)
        self.daemon_threads = True
        self._request_executor = ThreadPoolExecutor(
            max_workers=max(2, int(max_workers)),
            thread_name_prefix="relayterm-http",
        )

    def process_request(self, request, client_address):  # noqa: N802
        try:
            self._request_executor.submit(
                self.process_request_thread, request, client_address,
            )
        except (RuntimeError, MemoryError):
            self.shutdown_request(request)

    def server_close(self):  # noqa: N802
        self._request_executor.shutdown(wait=False, cancel_futures=True)
        super().server_close()


def create_app(manager: SessionManager | None = None, token: str | None = None):
    """Build an aiohttp adapter with the same routes as the stdlib server."""
    try:
        from aiohttp import web
    except ImportError as exc:
        raise RuntimeError("aiohttp_required") from exc
    import asyncio

    active = manager or SESSION_MANAGER
    required_token = TOKEN if token is None else token

    def authorized(request) -> bool:
        return is_authorized(request.headers.get("Authorization", ""), required_token)

    async def health(_request):
        return web.json_response({
            "ok": True, "service": "relayterm",
            "bridgeGeneration": BRIDGE_GENERATION,
        })

    async def profiles(request):
        if not authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        return web.json_response(PROFILE_STORE.catalog())

    async def sessions(request):
        if not authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        activity_store = recent_activity_store()
        activity = activity_store.load() if activity_store is not None else {}
        values = active.list_status()
        for item in values:
            profile_id = str(item.get("profileId", ""))
            if profile_id:
                item["lastOpenedAt"] = activity.get(profile_id, item.get("lastOpenedAt"))
        return web.json_response({"sessions": values})

    async def codex_sessions_route(request):
        if not authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            profile_id = validate_session_id(request.match_info["profile_id"])
            value = await asyncio.to_thread(codex_session_service(active).sessions, profile_id)
            return web.json_response(value)
        except CodexSessionError as exc:
            return web.json_response(exc.payload(), status=exc.status)
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=500)

    async def codex_binding_route(request):
        if not authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            profile_id = validate_session_id(request.match_info["profile_id"])
            payload = await request.json()
            value = await asyncio.to_thread(
                codex_session_service(active).set_binding, profile_id, payload,
            )
            return web.json_response(value)
        except CodexSessionError as exc:
            return web.json_response(exc.payload(), status=exc.status)
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=400)

    async def codex_create_route(request):
        if not authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            profile_id = validate_session_id(request.match_info["profile_id"])
            payload = await request.json() if request.can_read_body else {}
            value = await asyncio.to_thread(
                codex_session_service(active).create,
                profile_id,
                lock=bool(payload.get("lock", True)),
            )
            return web.json_response(value, status=201)
        except CodexSessionError as exc:
            return web.json_response(exc.payload(), status=exc.status)
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=400)

    async def exec_route(request):
        if not authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            status, result = await asyncio.to_thread(execute_legacy, await request.json())
            return web.json_response(result, status=status)
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=400)

    async def terminate_route(request):
        if not authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        profile_id = request.match_info["profile_id"]
        try:
            profile_id = validate_session_id(profile_id)
            payload = await request.json() if request.can_read_body else {}
            if bool(payload.get("remove", False)):
                found = active.remove_profile(profile_id, terminate=True)
            else:
                found = active.terminate_profile(profile_id, bool(payload.get("force", True)))
            return web.json_response({"ok": found, "profileId": profile_id}, status=200 if found else 404)
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=400)

    async def create_pairing(request):
        if not authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            payload = await request.json()
            code, expires = PAIRING_MANAGER.create(str(payload.get("endpoint", "")))
            return web.json_response({"challenge": code, "expiresIn": expires}, status=201)
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=400)

    async def exchange_pairing(request):
        payload = await request.json()
        endpoint = PAIRING_MANAGER.exchange(str(payload.get("challenge", "")))
        if endpoint is None:
            return web.json_response({"error": "pairing_expired"}, status=410)
        catalog = PROFILE_STORE.catalog()
        return web.json_response({
            "endpoint": endpoint, "token": required_token,
            "profiles": catalog["profiles"], "revision": catalog["revision"],
        })

    async def pair_page(request):
        code = request.match_info["challenge"]
        endpoint = PAIRING_MANAGER.inspect(code)
        if endpoint is None:
            return web.Response(text="Pairing expired.", status=410)
        return web.Response(body=pairing_page(code, endpoint), content_type="text/html")

    async def pty_route(request):
        if not authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        websocket = web.WebSocketResponse(
            max_msg_size=MAX_FRAME_BYTES, heartbeat=25, autoping=False,
        )
        await websocket.prepare(request)
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[tuple[str, object]] = asyncio.Queue(maxsize=128)
        session: PtySession | None = None
        attachment: SessionAttachment | None = None
        opened = False
        resyncing = False

        def enqueue(kind: str, value: object) -> bool:
            def put() -> None:
                nonlocal resyncing
                if websocket.closed:
                    return
                if resyncing:
                    return
                try:
                    queue.put_nowait((kind, value))
                except asyncio.QueueFull:
                    while not queue.empty():
                        try:
                            queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                    try:
                        queue.put_nowait(("event", {"type": "resync_required"}))
                        queue.put_nowait(("close", None))
                        resyncing = True
                    except asyncio.QueueFull:
                        pass
            try:
                if asyncio.get_running_loop() is loop:
                    put()
                else:
                    loop.call_soon_threadsafe(put)
                return True
            except RuntimeError:
                try:
                    loop.call_soon_threadsafe(put)
                    return True
                except RuntimeError:
                    return False

        async def pump() -> None:
            while not websocket.closed:
                kind, value = await queue.get()
                if kind == "binary":
                    await websocket.send_bytes(value)  # type: ignore[arg-type]
                elif kind == "close":
                    await websocket.close(code=1013, message=b"resync_required")
                    return
                else:
                    await websocket.send_json(value)

        pump_task = asyncio.create_task(pump())
        try:
            async for message in websocket:
                if attachment is not None and session is not None:
                    # aiohttp exposes protocol ping/pong frames as messages on
                    # some versions and application JSON frames on all; both
                    # count as an inbound heartbeat.
                    session.touch_attachment(attachment)
                if message.type == web.WSMsgType.PING:
                    if attachment is not None:
                        session.touch_attachment(attachment)
                    await websocket.pong(message.data)
                    continue
                if message.type == web.WSMsgType.PONG:
                    continue
                if message.type == web.WSMsgType.BINARY:
                    if session is None or attachment is None:
                        enqueue("event", {"type": "error", "code": "not_open", "message": "open_required"})
                    else:
                        try:
                            session.write_from(attachment, bytes(message.data))
                        except Exception as exc:
                            enqueue("event", {"type": "error", "code": str(exc), "message": str(exc)})
                    continue
                if message.type != web.WSMsgType.TEXT:
                    if message.type in (web.WSMsgType.CLOSE, web.WSMsgType.ERROR):
                        break
                    continue
                try:
                    payload = json.loads(message.data)
                    if not isinstance(payload, dict):
                        raise ValueError("object_required")
                    message_type = str(payload.get("type", ""))
                    if message_type == "open":
                        if opened:
                            raise ValueError("already_open")
                        resumed = False
                        try:
                            sid, profile_id, spec, cwd = await asyncio.to_thread(
                                open_spec, payload, request.query.get("sessionId", ""), active,
                            )
                            session, resumed = active.open_session(
                                sid, spec, cwd, payload.get("cols", 100), payload.get("rows", 32),
                                bool(payload.get("resume", True)), profile_id=profile_id,
                                start_immediately=False,
                            )
                            client_type = str(payload.get("clientType", "legacy"))
                            attachment, snapshot, ended = session.attach(
                                lambda data: enqueue("binary", data), lambda event: enqueue("event", event),
                                lambda: loop.call_soon_threadsafe(
                                    lambda: asyncio.create_task(websocket.close(code=1000, message=b"replaced"))
                                ),
                                client_id=str(payload.get("clientId", "")),
                                client_type=client_type,
                                cols=payload.get("cols", 100), rows=payload.get("rows", 32),
                                defer_output=True,
                            )
                            session.ensure_started()
                            ended = session.ended
                            opened = True
                            last_opened_at = record_profile_open(session)
                            await websocket.send_json({
                                "type": "ready", "sessionId": session.session_id,
                                "profileId": session.profile_id, "pid": session.pid,
                                "resumed": bool(resumed), "role": session.role_for(attachment),
                                "lastOpenedAt": last_opened_at,
                                "codexThreadId": session.codex_thread_id or None,
                            })
                            snapshot = replay_snapshot(snapshot, client_type, bool(resumed))
                            if not attachment.activate_output(snapshot):
                                raise RuntimeError("attachment_send_failed")
                            if ended:
                                enqueue("event", {
                                    "type": "exit", "code": int(session.exit_code or 0), "cwd": session.cwd,
                                })
                        except Exception as exc:
                            _cleanup_open_failure(active, session, attachment, resumed)
                            attachment = None
                            if not websocket.closed:
                                await websocket.send_json(websocket_error(exc))
                            break
                    elif message_type == "resize" and session is not None and attachment is not None:
                        session.resize_from(attachment, payload.get("rows", 32), payload.get("cols", 100))
                    elif message_type == "signal" and session is not None and attachment is not None:
                        session.signal_from(attachment, str(payload.get("name", "")))
                    elif message_type in ("ping", "heartbeat"):
                        if attachment is not None:
                            session.touch_attachment(attachment)
                        enqueue("event", {"type": "pong"})
                    elif message_type == "close":
                        if session is not None and bool(payload.get("terminate", False)):
                            session.terminate(True)
                        if session is not None and attachment is not None:
                            session.detach(
                                attachment,
                                invoke_close=False,
                                reason=str(payload.get("reason", "")),
                            )
                            attachment = None
                        break
                    else:
                        raise ValueError("message_invalid")
                except Exception as exc:
                    enqueue("event", {"type": "error", "code": str(exc), "message": str(exc)})
        finally:
            if session is not None and attachment is not None:
                session.detach(attachment, invoke_close=False)
            pump_task.cancel()
            try:
                await pump_task
            except asyncio.CancelledError:
                pass
            if not websocket.closed:
                await websocket.close()
        return websocket

    app = web.Application(client_max_size=1024 * 1024)
    app.router.add_get("/health", health)
    app.router.add_get("/v1/profiles", profiles)
    app.router.add_get("/v1/sessions", sessions)
    app.router.add_get("/v1/profiles/{profile_id}/codex-sessions", codex_sessions_route)
    app.router.add_put("/v1/profiles/{profile_id}/codex-binding", codex_binding_route)
    app.router.add_post("/v1/profiles/{profile_id}/codex-sessions", codex_create_route)
    app.router.add_post("/v1/exec", exec_route)
    app.router.add_post("/v1/sessions/{profile_id}/terminate", terminate_route)
    app.router.add_post("/v1/pairing/challenges", create_pairing)
    app.router.add_post("/v1/pairing/exchange", exchange_pairing)
    app.router.add_get("/pair/{challenge}", pair_page)
    app.router.add_get("/v1/pty", pty_route)
    return app


def main() -> None:
    print(f"RelayTerm bridge listening on http://{HOST}:{PORT}", flush=True)
    server = RelayHTTPServer((HOST, PORT), RelayHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        SESSION_MANAGER.shutdown()
        CODEX_APP_SERVER.shutdown()


if __name__ == "__main__":
    main()
