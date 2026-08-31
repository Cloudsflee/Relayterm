"""Codex app-server session discovery and RelayTerm profile bindings."""

from __future__ import annotations

import json
import ntpath
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ALLOWED_SOURCES = frozenset(("cli", "vscode", "appServer"))
THREAD_TITLE_LENGTH = 80
UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
WINDOWS_PATH_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")


class CodexSessionError(RuntimeError):
    """Stable bridge error with an HTTP status and optional safe details."""

    def __init__(
        self, code: str, message: str | None = None, *, status: int = 500,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code
        self.status = int(status)
        self.details = dict(details or {})

    def payload(self) -> dict[str, object]:
        value: dict[str, object] = {"error": self.code, "message": self.message}
        value.update(self.details)
        return value


class AppServerTransportError(CodexSessionError):
    def __init__(self, message: str) -> None:
        super().__init__("codex_app_server_unavailable", message, status=503)


class AppServerProtocolError(CodexSessionError):
    def __init__(self, message: str) -> None:
        super().__init__("codex_app_server_protocol_error", message, status=502)


class AppServerRpcError(CodexSessionError):
    def __init__(self, error: object) -> None:
        value = error if isinstance(error, dict) else {}
        try:
            rpc_code = int(value.get("code", 0))
        except (TypeError, ValueError):
            rpc_code = 0
        rpc_message = str(value.get("message", error or "app-server request failed"))
        code = "codex_app_server_upgrade_required" if rpc_code == -32601 else "codex_app_server_error"
        if rpc_code == -32601:
            rpc_message = "Codex CLI 缺少 thread API，请升级 Codex CLI 后重试：" + rpc_message
        super().__init__(code, rpc_message, status=503, details={"rpcCode": rpc_code})
        self.rpc_code = rpc_code


def canonical_thread_id(value: object) -> str:
    text = str(value or "").strip()
    if not UUID_RE.fullmatch(text):
        raise CodexSessionError("codex_thread_id_invalid", status=400)
    try:
        return str(uuid.UUID(text))
    except (ValueError, AttributeError) as exc:
        raise CodexSessionError("codex_thread_id_invalid", status=400) from exc


def utc_timestamp(value: object = None) -> str | None:
    if value in (None, ""):
        return None
    parsed: datetime
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            parsed = datetime.fromtimestamp(float(value), timezone.utc)
        else:
            text = str(value).strip()
            if text.endswith(("Z", "z")):
                text = text[:-1] + "+00:00"
            parsed = datetime.fromisoformat(text)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            parsed = parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    return parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def timestamp_number(value: object) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return float(value)
        except (TypeError, ValueError, OverflowError):
            return 0.0
    converted = utc_timestamp(value)
    if not converted:
        return 0.0
    try:
        return datetime.fromisoformat(converted.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0.0


def normalize_working_directory(value: object) -> str:
    """Normalize native and Windows fixture paths without requiring existence."""
    text = os.path.expandvars(os.path.expanduser(str(value or "").strip()))
    if not text:
        return ""
    text = text.removeprefix("\\\\?\\")
    if WINDOWS_PATH_RE.match(text):
        return ntpath.normcase(ntpath.normpath(text.replace("/", "\\"))).rstrip("\\")
    try:
        return os.path.normcase(os.path.realpath(os.path.abspath(text))).rstrip(os.sep)
    except (OSError, ValueError):
        return os.path.normcase(os.path.normpath(text)).rstrip(os.sep)


def git_repository_root(directory: str) -> str | None:
    if not directory or not os.path.isdir(directory):
        return None
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        completed = subprocess.run(
            ["git", "-C", directory, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=3, check=False, creationflags=flags,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    root = completed.stdout.strip()
    return normalize_working_directory(root) if root else None


class CodexAppServerClient:
    """Lazy JSONL app-server client with bounded calls and one restart."""

    def __init__(
        self, command: Iterable[str] | None = None, *, timeout: float = 6.0,
        cache_seconds: float = 2.0,
    ) -> None:
        self.command = list(command or [os.environ.get("RELAYTERM_CODEX", "codex"), "app-server", "--stdio"])
        self.timeout = max(0.1, float(timeout))
        self.cache_seconds = max(0.0, float(cache_seconds))
        self._call_lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._process: subprocess.Popen[str] | None = None
        self._initialized = False
        self._next_id = 0
        self._pending: dict[int, tuple[threading.Event, dict[str, object]]] = {}
        self._transport_error: BaseException | None = None
        self._stderr: deque[str] = deque(maxlen=24)
        self._cache: dict[bool, tuple[float, list[dict[str, object]]]] = {}

    def _spawn_command(self) -> list[str]:
        command = list(self.command)
        if not command:
            raise AppServerTransportError("app-server command missing")
        executable = shutil.which(command[0]) or command[0]
        command[0] = executable
        if os.name != "nt":
            return command
        suffix = Path(executable).suffix.casefold()
        if suffix in (".cmd", ".bat"):
            return [
                os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c",
                subprocess.list2cmdline(command),
            ]
        if suffix == ".ps1":
            powershell = shutil.which("pwsh.exe") or shutil.which("powershell.exe") or "powershell.exe"
            return [powershell, "-NoLogo", "-NoProfile", "-File", *command]
        return command

    def _diagnostic(self, fallback: str) -> str:
        detail = " | ".join(item for item in self._stderr if item)
        suffix = (": " + detail[-1200:]) if detail else ""
        return fallback + suffix

    def _reader(self, process: subprocess.Popen[str]) -> None:
        stream = process.stdout
        if stream is None:
            self._fail_transport(process, AppServerTransportError("app-server stdout missing"))
            return
        try:
            for raw in stream:
                line = raw.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                    if not isinstance(message, dict):
                        raise ValueError("JSON object required")
                except (json.JSONDecodeError, ValueError) as exc:
                    self._fail_transport(process, AppServerProtocolError(f"invalid JSONL response: {exc}"))
                    return
                request_id = message.get("id")
                if request_id is None:
                    continue
                try:
                    numeric_id = int(request_id)
                except (TypeError, ValueError):
                    self._fail_transport(process, AppServerProtocolError("response id invalid"))
                    return
                with self._state_lock:
                    pending = self._pending.get(numeric_id)
                if pending is not None:
                    event, holder = pending
                    holder["message"] = message
                    event.set()
                elif "method" in message:
                    # RelayTerm uses only catalog methods.  Answer unexpected
                    # server requests so they never stall the stdio transport.
                    try:
                        self._write_message({
                            "id": request_id,
                            "error": {"code": -32601, "message": "client method unsupported"},
                        }, process)
                    except CodexSessionError:
                        return
        except (OSError, UnicodeError) as exc:
            self._fail_transport(process, AppServerTransportError(str(exc)))
            return
        self._fail_transport(
            process,
            AppServerTransportError(self._diagnostic(f"app-server exited ({process.poll()})")),
        )

    def _stderr_reader(self, process: subprocess.Popen[str]) -> None:
        stream = process.stderr
        if stream is None:
            return
        try:
            for raw in stream:
                value = raw.strip()
                if value:
                    self._stderr.append(value)
        except (OSError, UnicodeError):
            pass

    def _fail_transport(self, process: subprocess.Popen[str], error: BaseException) -> None:
        with self._state_lock:
            if self._process is not process:
                return
            self._transport_error = error
            pending = list(self._pending.values())
        for event, _holder in pending:
            event.set()

    def _write_message(self, value: dict[str, object], process: subprocess.Popen[str] | None = None) -> None:
        with self._write_lock:
            active = process or self._process
            if active is None or active.poll() is not None or active.stdin is None:
                raise AppServerTransportError(self._diagnostic("app-server is not running"))
            try:
                active.stdin.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
                active.stdin.flush()
            except (OSError, BrokenPipeError) as exc:
                raise AppServerTransportError(self._diagnostic(str(exc))) from exc

    def _raw_request_locked(
        self, method: str, params: dict[str, object] | None = None, timeout: float | None = None,
    ) -> object:
        process = self._process
        if process is None:
            raise AppServerTransportError("app-server process missing")
        self._next_id += 1
        request_id = self._next_id
        event = threading.Event()
        holder: dict[str, object] = {}
        with self._state_lock:
            self._pending[request_id] = (event, holder)
            self._transport_error = None
        try:
            self._write_message({"method": method, "id": request_id, "params": params or {}}, process)
            if not event.wait(self.timeout if timeout is None else max(0.1, float(timeout))):
                raise AppServerTransportError(f"app-server request timed out: {method}")
            with self._state_lock:
                transport_error = self._transport_error
            if "message" not in holder:
                if isinstance(transport_error, CodexSessionError):
                    raise transport_error
                raise AppServerTransportError(self._diagnostic(f"app-server ended during {method}"))
            message = holder["message"]
            if not isinstance(message, dict):
                raise AppServerProtocolError("response object missing")
            if "error" in message:
                raise AppServerRpcError(message.get("error"))
            if "result" not in message:
                raise AppServerProtocolError("response result missing")
            return message.get("result")
        finally:
            with self._state_lock:
                self._pending.pop(request_id, None)

    def _start_locked(self) -> None:
        self._stop_locked()
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            process = subprocess.Popen(
                self._spawn_command(),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                creationflags=flags,
            )
        except OSError as exc:
            raise AppServerTransportError(
                "Codex 会话功能未启动；请升级或安装 Codex CLI：" + str(exc)
            ) from exc
        with self._state_lock:
            self._process = process
            self._initialized = False
            self._transport_error = None
            self._stderr.clear()
        threading.Thread(target=self._reader, args=(process,), name="codex-app-server", daemon=True).start()
        threading.Thread(
            target=self._stderr_reader, args=(process,), name="codex-app-server-stderr", daemon=True,
        ).start()
        try:
            result = self._raw_request_locked("initialize", {
                "clientInfo": {"name": "relayterm", "title": "RelayTerm", "version": "0.3.0"},
            })
            if not isinstance(result, dict):
                raise AppServerProtocolError("initialize result invalid")
            self._write_message({"method": "initialized", "params": {}}, process)
            self._initialized = True
        except BaseException:
            self._stop_locked()
            raise

    def _ensure_started_locked(self) -> None:
        process = self._process
        if process is not None and process.poll() is None and self._initialized:
            return
        self._start_locked()

    def request(
        self, method: str, params: dict[str, object] | None = None, *, timeout: float | None = None,
    ) -> object:
        last_error: CodexSessionError | None = None
        for attempt in range(2):
            with self._call_lock:
                try:
                    self._ensure_started_locked()
                    return self._raw_request_locked(method, params, timeout)
                except AppServerRpcError:
                    raise
                except (AppServerTransportError, AppServerProtocolError) as exc:
                    last_error = exc
                    self._stop_locked()
            if attempt == 0:
                continue
        assert last_error is not None
        raise last_error

    def list_threads(self, archived: bool = False, *, force: bool = False) -> list[dict[str, object]]:
        key = bool(archived)
        with self._state_lock:
            cached = self._cache.get(key)
            if not force and cached is not None and cached[0] > time.monotonic():
                return [dict(item) for item in cached[1]]
        cursor: str | None = None
        seen_cursors: set[str] = set()
        seen_ids: set[str] = set()
        result: list[dict[str, object]] = []
        for _page in range(100):
            params: dict[str, object] = {
                "archived": key,
                "limit": 100,
                "sortKey": "recency_at",
                "sortDirection": "desc",
                "sourceKinds": sorted(ALLOWED_SOURCES),
            }
            if cursor:
                params["cursor"] = cursor
            response = self.request("thread/list", params)
            if not isinstance(response, dict) or not isinstance(response.get("data"), list):
                raise AppServerProtocolError("thread/list result invalid")
            for raw in response["data"]:
                if not isinstance(raw, dict):
                    raise AppServerProtocolError("thread/list item invalid")
                thread_id = str(raw.get("id", ""))
                if thread_id and thread_id not in seen_ids:
                    seen_ids.add(thread_id)
                    result.append(dict(raw))
            raw_cursor = response.get("nextCursor")
            if raw_cursor in (None, ""):
                break
            cursor = str(raw_cursor)
            if cursor in seen_cursors:
                raise AppServerProtocolError("thread/list cursor repeated")
            seen_cursors.add(cursor)
        else:
            raise AppServerProtocolError("thread/list page limit exceeded")
        with self._state_lock:
            self._cache[key] = (time.monotonic() + self.cache_seconds, [dict(item) for item in result])
        return result

    def start_thread(self, cwd: str) -> dict[str, object]:
        result = self.request("thread/start", {"cwd": os.path.abspath(cwd)})
        if not isinstance(result, dict) or not isinstance(result.get("thread"), dict):
            raise AppServerProtocolError("thread/start result invalid")
        thread = dict(result["thread"])
        thread_id = canonical_thread_id(thread.get("id"))
        thread["id"] = thread_id
        try:
            self.request("thread/unsubscribe", {"threadId": thread_id})
        except CodexSessionError:
            # Older app-server builds can still persist and resume the thread;
            # leaving the idle subscription in place does not fork history.
            pass
        with self._state_lock:
            self._cache.clear()
        return thread

    def _stop_locked(self) -> None:
        with self._state_lock:
            process = self._process
            self._process = None
            self._initialized = False
            pending = list(self._pending.values())
            self._pending.clear()
        for event, _holder in pending:
            event.set()
        if process is None:
            return
        for stream in (process.stdin, process.stdout, process.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        if process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                except OSError:
                    pass

    def shutdown(self) -> None:
        with self._call_lock:
            self._stop_locked()


class CodexBindingStore:
    """Atomic profile-to-thread preferences kept outside profiles.json."""

    VERSION = 1

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    def _decode(self, value: object) -> dict[str, dict[str, object]]:
        if not isinstance(value, dict) or int(value.get("version", 0) or 0) != self.VERSION:
            raise ValueError("codex_bindings_invalid")
        items = value.get("bindings", [])
        if not isinstance(items, list):
            raise ValueError("codex_bindings_invalid")
        result: dict[str, dict[str, object]] = {}
        for raw in items:
            if not isinstance(raw, dict):
                raise ValueError("codex_binding_invalid")
            profile_id = str(raw.get("profileId", "")).strip()
            mode = str(raw.get("mode", "auto")).strip().lower()
            if not profile_id or mode not in ("auto", "locked"):
                raise ValueError("codex_binding_invalid")
            thread_id = None
            if mode == "locked":
                thread_id = canonical_thread_id(raw.get("threadId"))
            result[profile_id] = {
                "profileId": profile_id,
                "mode": mode,
                "threadId": thread_id,
                "updatedAt": utc_timestamp(raw.get("updatedAt")),
            }
        return result

    def load(self) -> dict[str, dict[str, object]]:
        with self._lock:
            if not self.path.exists():
                return {}
            try:
                return self._decode(json.loads(self.path.read_text(encoding="utf-8")))
            except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
                corrupt = self.path.with_name(
                    f"{self.path.name}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
                )
                try:
                    os.replace(self.path, corrupt)
                except OSError:
                    pass
                return {}

    def save(self, bindings: dict[str, dict[str, object]]) -> dict[str, dict[str, object]]:
        values: list[dict[str, object]] = []
        for profile_id in sorted(bindings):
            raw = dict(bindings[profile_id])
            mode = str(raw.get("mode", "auto"))
            item: dict[str, object] = {
                "profileId": str(profile_id),
                "mode": mode,
                "threadId": canonical_thread_id(raw.get("threadId")) if mode == "locked" else None,
                "updatedAt": utc_timestamp(raw.get("updatedAt")) or utc_timestamp(time.time()),
            }
            values.append(item)
        payload = {"version": self.VERSION, "bindings": values}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            handle, temporary = tempfile.mkstemp(
                prefix=self.path.name + ".", suffix=".tmp", dir=self.path.parent,
            )
            try:
                with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                    json.dump(payload, stream, ensure_ascii=False, indent=2)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            finally:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
        return self.load()

    def get(self, profile_id: str) -> dict[str, object]:
        value = self.load().get(str(profile_id).strip())
        return dict(value) if value is not None else {
            "profileId": str(profile_id).strip(), "mode": "auto", "threadId": None, "updatedAt": None,
        }

    def set_auto(self, profile_id: str) -> dict[str, object]:
        with self._lock:
            values = self.load()
            values[str(profile_id)] = {
                "profileId": str(profile_id), "mode": "auto", "threadId": None,
                "updatedAt": utc_timestamp(time.time()),
            }
            return dict(self.save(values)[str(profile_id)])

    def set_locked(self, profile_id: str, thread_id: str) -> dict[str, object]:
        canonical = canonical_thread_id(thread_id)
        with self._lock:
            values = self.load()
            values[str(profile_id)] = {
                "profileId": str(profile_id), "mode": "locked", "threadId": canonical,
                "updatedAt": utc_timestamp(time.time()),
            }
            return dict(self.save(values)[str(profile_id)])

    def remove_except(self, profile_ids: Iterable[str]) -> int:
        allowed = {str(value) for value in profile_ids}
        with self._lock:
            values = self.load()
            kept = {key: value for key, value in values.items() if key in allowed}
            removed = len(values) - len(kept)
            if removed:
                self.save(kept)
            return removed


def _source_name(thread: dict[str, object]) -> str:
    source = thread.get("source")
    if isinstance(source, str):
        return source
    if isinstance(source, dict) and "subAgent" in source:
        return "subAgent"
    return "unknown"


def _status_name(thread: dict[str, object]) -> str:
    status = thread.get("status")
    if isinstance(status, str):
        return status
    if isinstance(status, dict):
        return str(status.get("type", "unknown"))
    return "unknown"


def _eligible_thread(thread: dict[str, object]) -> bool:
    try:
        canonical_thread_id(thread.get("id"))
    except CodexSessionError:
        return False
    return (
        _source_name(thread) in ALLOWED_SOURCES
        and not bool(thread.get("ephemeral", False))
        and not thread.get("parentThreadId")
    )


@dataclass(frozen=True)
class CodexCandidate:
    id: str
    title: str
    source: str
    cwd: str
    updated_at: str | None
    match_type: str
    status: str
    recency: float

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "title": self.title,
            "source": self.source,
            "cwd": self.cwd,
            "updatedAt": self.updated_at,
            "matchType": self.match_type,
            "status": self.status,
        }


class CodexSessionService:
    def __init__(
        self, client: CodexAppServerClient, bindings: CodexBindingStore,
        profile_store: Any, session_manager: Any,
        *, git_root_resolver: Callable[[str], str | None] = git_repository_root,
    ) -> None:
        self.client = client
        self.bindings = bindings
        self.profile_store = profile_store
        self.session_manager = session_manager
        self.git_root_resolver = git_root_resolver

    def _profile(self, profile_id: str) -> Any:
        profile = self.profile_store.get(str(profile_id))
        if profile is None or not getattr(profile, "enabled", False):
            raise CodexSessionError("profile_not_found", status=404)
        if getattr(profile, "launch_mode", "command") != "codex":
            raise CodexSessionError("profile_not_codex", status=409)
        if getattr(profile, "shell", "") not in ("pwsh", "powershell", "cmd"):
            raise CodexSessionError("profile_codex_shell_unsupported", status=409)
        return profile

    @staticmethod
    def _candidate(thread: dict[str, object], match_type: str) -> CodexCandidate:
        title = str(thread.get("name") or thread.get("preview") or "未命名会话")
        title = " ".join(title.split())
        if len(title) > THREAD_TITLE_LENGTH:
            title = title[: THREAD_TITLE_LENGTH - 1] + "…"
        updated = thread.get("updatedAt")
        recency = thread.get("recencyAt")
        return CodexCandidate(
            canonical_thread_id(thread.get("id")), title, _source_name(thread),
            str(thread.get("cwd", "")), utc_timestamp(updated), match_type,
            _status_name(thread), timestamp_number(recency if recency is not None else updated),
        )

    def discover(self, profile: Any) -> tuple[list[CodexCandidate], list[CodexCandidate]]:
        profile_path = normalize_working_directory(profile.working_directory)
        threads = [value for value in self.client.list_threads(False) if _eligible_thread(value)]
        exact: list[CodexCandidate] = []
        remainder: list[dict[str, object]] = []
        for thread in threads:
            if normalize_working_directory(thread.get("cwd")) == profile_path:
                exact.append(self._candidate(thread, "exact"))
            else:
                remainder.append(thread)
        repository: list[CodexCandidate] = []
        profile_root = self.git_root_resolver(profile.working_directory)
        if profile_root:
            normalized_root = normalize_working_directory(profile_root)
            roots: dict[str, str | None] = {}
            for thread in remainder:
                cwd = str(thread.get("cwd", ""))
                key = normalize_working_directory(cwd)
                if key not in roots:
                    roots[key] = self.git_root_resolver(cwd)
                if roots[key] and normalize_working_directory(roots[key]) == normalized_root:
                    repository.append(self._candidate(thread, "repository"))
        exact.sort(key=lambda item: (-item.recency, item.id))
        repository.sort(key=lambda item: (-item.recency, item.id))
        return exact, repository

    @staticmethod
    def _find(candidates: Iterable[CodexCandidate], thread_id: str) -> CodexCandidate | None:
        return next((item for item in candidates if item.id == thread_id), None)

    def _locked_status(
        self, thread_id: str, exact: list[CodexCandidate], repository: list[CodexCandidate],
    ) -> tuple[str, CodexCandidate | None]:
        candidate = self._find([*exact, *repository], thread_id)
        if candidate is not None:
            return "valid", candidate
        archived = [value for value in self.client.list_threads(True) if _eligible_thread(value)]
        if any(canonical_thread_id(item.get("id")) == thread_id for item in archived):
            return "archived", None
        return "missing", None

    def sessions(self, profile_id: str) -> dict[str, object]:
        profile = self._profile(profile_id)
        exact, repository = self.discover(profile)
        binding = self.bindings.get(profile.id)
        binding_status = "auto"
        selected: CodexCandidate | None = exact[0] if exact else None
        if binding.get("mode") == "locked":
            thread_id = canonical_thread_id(binding.get("threadId"))
            binding_status, selected = self._locked_status(thread_id, exact, repository)
        active = self.session_manager.get_by_profile(profile.id)
        current: dict[str, object] | None = None
        if active is not None and not active.ended:
            current_id = str(getattr(active, "codex_thread_id", "") or "")
            known = self._find([*exact, *repository], current_id) if current_id else None
            current = known.to_dict() if known is not None else {
                "id": current_id or None,
                "title": "",
                "source": "",
                "cwd": str(getattr(active, "cwd", profile.working_directory)),
                "updatedAt": None,
                "matchType": "current",
                "status": "running",
            }
        requires_selection = (
            (binding.get("mode") == "locked" and binding_status != "valid")
            or (binding.get("mode") == "auto" and not exact and bool(repository))
        )
        return {
            "profileId": profile.id,
            "mode": binding.get("mode", "auto"),
            "binding": {
                "mode": binding.get("mode", "auto"),
                "threadId": binding.get("threadId"),
                "updatedAt": binding.get("updatedAt"),
                "status": binding_status,
            },
            "currentRelayThread": current,
            "selectedThreadId": current.get("id") if current else (selected.id if selected else None),
            "requiresSelection": requires_selection,
            "exactCandidates": [item.to_dict() for item in exact],
            "repositoryCandidates": [item.to_dict() for item in repository],
        }

    def set_binding(self, profile_id: str, value: dict[str, object]) -> dict[str, object]:
        profile = self._profile(profile_id)
        mode = str(value.get("mode", "")).strip().lower()
        if mode == "auto":
            binding = self.bindings.set_auto(profile.id)
        elif mode == "locked":
            thread_id = canonical_thread_id(value.get("threadId"))
            exact, repository = self.discover(profile)
            status, candidate = self._locked_status(thread_id, exact, repository)
            if status != "valid" or candidate is None:
                raise CodexSessionError(
                    "codex_binding_invalid", f"locked thread is {status}", status=409,
                    details={"threadId": thread_id, "bindingStatus": status},
                )
            binding = self.bindings.set_locked(profile.id, thread_id)
        else:
            raise CodexSessionError("codex_binding_mode_invalid", status=400)
        return {"profileId": profile.id, "binding": binding}

    def create(self, profile_id: str, *, lock: bool = True) -> dict[str, object]:
        profile = self._profile(profile_id)
        thread = self.client.start_thread(profile.working_directory)
        candidate = self._candidate(thread, "exact")
        binding = self.bindings.set_locked(profile.id, candidate.id) if lock else self.bindings.get(profile.id)
        return {"profileId": profile.id, "thread": candidate.to_dict(), "binding": binding}

    def resolve(self, profile: Any, requested_thread_id: object = None) -> str:
        requested = canonical_thread_id(requested_thread_id) if requested_thread_id not in (None, "") else ""
        active = self.session_manager.get_by_profile(profile.id)
        if active is not None and not active.ended:
            current = str(getattr(active, "codex_thread_id", "") or "")
            if requested and requested != current:
                raise CodexSessionError(
                    "codex_thread_conflict", "RelayTerm profile is running another Codex thread",
                    status=409, details={"currentThreadId": current or None, "requestedThreadId": requested},
                )
            return current

        exact, repository = self.discover(profile)
        if requested:
            if self._find([*exact, *repository], requested) is None:
                status, _candidate = self._locked_status(requested, exact, repository)
                raise CodexSessionError(
                    "codex_thread_unavailable", f"requested thread is {status}", status=409,
                    details={"threadId": requested, "threadStatus": status},
                )
            return requested

        binding = self.bindings.get(profile.id)
        if binding.get("mode") == "locked":
            thread_id = canonical_thread_id(binding.get("threadId"))
            status, candidate = self._locked_status(thread_id, exact, repository)
            if status != "valid" or candidate is None:
                raise CodexSessionError(
                    "codex_binding_invalid", f"locked thread is {status}", status=409,
                    details={"threadId": thread_id, "bindingStatus": status},
                )
            return candidate.id
        if exact:
            return exact[0].id
        if repository:
            raise CodexSessionError(
                "codex_selection_required", "同一 Git 仓库存在其他目录的 Codex 会话，请明确选择",
                status=409,
                details={"repositoryCandidates": [item.to_dict() for item in repository]},
            )
        created = self.create(profile.id, lock=False)
        return canonical_thread_id(created["thread"]["id"])


def _powershell_quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _cmd_argv_quote(value: str) -> str:
    value = str(value)
    if any(char in value for char in '\x00\r\n%!^&|<>()"'):
        raise CodexSessionError("profile_codex_args_cmd_unsupported", status=400)
    result = ['"']
    backslashes = 0
    for char in value:
        if char == "\\":
            backslashes += 1
        elif char == '"':
            result.append("\\" * (backslashes * 2 + 1))
            result.append('"')
            backslashes = 0
        else:
            result.append("\\" * backslashes)
            result.append(char)
            backslashes = 0
    result.append("\\" * (backslashes * 2))
    result.append('"')
    return "".join(result)


def build_codex_resume_command(
    shell: str, thread_id: str, codex_args: Iterable[str], *, executable: str = "codex",
) -> tuple[str, str]:
    """Build a shell command whose only session selector is a verified UUID."""
    canonical = canonical_thread_id(thread_id)
    argv = [str(executable), "resume", canonical, *[str(value) for value in codex_args]]
    marker = "__RELAYTERM_CODEX_FAILED_" + uuid.uuid4().hex + "__"
    shell = str(shell).strip().lower()
    if shell in ("pwsh", "powershell"):
        command = "& " + " ".join(_powershell_quote(value) for value in argv)
        wrapped = (
            "$__relayterm_code=0; try { " + command
            + "; $__relayterm_code=if ($null -eq $LASTEXITCODE) { 0 } else { $LASTEXITCODE } } "
            + "catch { Write-Error $_; $__relayterm_code=127 }; "
            + "if ($__relayterm_code -ne 0) { Write-Output ('" + marker
            + "' + $__relayterm_code); exit $__relayterm_code }"
        )
        return wrapped, marker
    if shell == "cmd":
        command = "call " + " ".join(_cmd_argv_quote(value) for value in argv)
        wrapped = (
            command + "\r\nset \"__relayterm_codex_code=%ERRORLEVEL%\"\r\n"
            + "if not \"%__relayterm_codex_code%\"==\"0\" (echo " + marker
            + "%__relayterm_codex_code% & exit /b %__relayterm_codex_code%)"
        )
        return wrapped, marker
    raise CodexSessionError("profile_codex_shell_unsupported", status=409)


__all__ = [
    "ALLOWED_SOURCES", "AppServerProtocolError", "AppServerRpcError",
    "AppServerTransportError", "CodexAppServerClient", "CodexBindingStore",
    "CodexCandidate", "CodexSessionError", "CodexSessionService",
    "build_codex_resume_command", "canonical_thread_id", "git_repository_root",
    "normalize_working_directory", "utc_timestamp",
]
