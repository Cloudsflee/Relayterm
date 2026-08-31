"""Side-by-side bridge draining without moving live PTY handles."""

from __future__ import annotations

import ctypes
import json
import os
import re
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .config import SettingsStore, TokenStore, atomic_write, data_directory

BRIDGE_GENERATION = "drain-switch-v1"
DRAIN_STATE_VERSION = 1
SHADOW_PROFILE_NAME = "profiles.v3.json"
PROFILE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


@dataclass(frozen=True)
class BridgeProbe:
    host: str
    port: int
    generation: str
    catalog_version: int
    catalog: dict[str, object]
    sessions: tuple[dict[str, object], ...]
    pid: int = 0

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def running_sessions(self) -> tuple[dict[str, object], ...]:
        return tuple(item for item in self.sessions if bool(item.get("running")))


class DrainStateStore:
    """Atomically persisted bridge topology used by the launcher and bridge."""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path) if path is not None else data_directory() / "drain_state.json"

    @staticmethod
    def empty() -> dict[str, object]:
        return {"version": DRAIN_STATE_VERSION, "primary": None, "drains": []}

    @staticmethod
    def _endpoint(value: object, *, primary: bool = False) -> dict[str, object]:
        if not isinstance(value, dict):
            raise ValueError("drain_endpoint_invalid")
        host = str(value.get("host", "127.0.0.1") or "").strip()
        try:
            port = int(value.get("port", 0))
            pid = int(value.get("pid", 0) or 0)
            agent_pid = int(value.get("agentPid", 0) or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("drain_endpoint_invalid") from exc
        if not host or port < 1 or port > 65535 or pid < 0 or agent_pid < 0:
            raise ValueError("drain_endpoint_invalid")
        result: dict[str, object] = {
            "host": host,
            "port": port,
            "pid": pid,
            "agentPid": agent_pid,
            "generation": str(value.get("generation", "") or ""),
        }
        if primary:
            path = str(value.get("profilePath", "") or "").strip()
            if not path:
                raise ValueError("drain_profile_path_invalid")
            result["profilePath"] = os.path.abspath(os.path.expanduser(path))
        else:
            raw_ids = value.get("profileIds", [])
            if not isinstance(raw_ids, list):
                raise ValueError("drain_profile_ids_invalid")
            profile_ids = []
            for raw in raw_ids:
                profile_id = str(raw or "").strip()
                if PROFILE_ID_RE.fullmatch(profile_id) and profile_id not in profile_ids:
                    profile_ids.append(profile_id)
            result["profileIds"] = profile_ids
            result["startedAt"] = str(value.get("startedAt", "") or "")
            try:
                result["emptyPolls"] = max(0, int(value.get("emptyPolls", 0) or 0))
            except (TypeError, ValueError):
                result["emptyPolls"] = 0
        return result

    def _decode(self, raw: str) -> dict[str, object]:
        value = json.loads(raw)
        if not isinstance(value, dict) or int(value.get("version", 0) or 0) != DRAIN_STATE_VERSION:
            raise ValueError("drain_state_invalid")
        primary_raw = value.get("primary")
        primary = None if primary_raw is None else self._endpoint(primary_raw, primary=True)
        drains_raw = value.get("drains", [])
        if not isinstance(drains_raw, list):
            raise ValueError("drain_state_invalid")
        drains = [self._endpoint(item) for item in drains_raw]
        return {"version": DRAIN_STATE_VERSION, "primary": primary, "drains": drains}

    def load(self) -> dict[str, object]:
        if not self.path.exists():
            return self.empty()
        try:
            return self._decode(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
            corrupt = self.path.with_name(
                f"{self.path.name}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
            )
            try:
                os.replace(self.path, corrupt)
            except OSError:
                pass
            return self.empty()

    def save(self, value: dict[str, object]) -> dict[str, object]:
        primary_raw = value.get("primary")
        primary = None if primary_raw is None else self._endpoint(primary_raw, primary=True)
        drains_raw = value.get("drains", [])
        if not isinstance(drains_raw, list):
            raise ValueError("drain_state_invalid")
        payload = {
            "version": DRAIN_STATE_VERSION,
            "primary": primary,
            "drains": [self._endpoint(item) for item in drains_raw],
        }
        atomic_write(
            self.path,
            (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )
        return payload


def request_json(
    base_url: str,
    path: str,
    token: str,
    method: str = "GET",
    body: dict[str, object] | None = None,
    *,
    authenticated: bool = True,
    timeout: float = 2.0,
) -> tuple[int, dict[str, object]]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(base_url.rstrip("/") + path, data=data, method=method)
    if authenticated:
        request.add_header("Authorization", "Bearer " + token)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            value = json.loads(raw) if raw else {}
            return response.status, value if isinstance(value, dict) else {}
    except urllib.error.HTTPError as exc:
        try:
            value = json.loads(exc.read())
        except Exception:
            value = {"error": str(exc)}
        return exc.code, value if isinstance(value, dict) else {"error": str(exc)}


def probe_bridge(host: str, port: int, token: str, *, timeout: float = 2.0) -> BridgeProbe | None:
    base_url = f"http://{host}:{int(port)}"
    try:
        health_status, health = request_json(
            base_url, "/health", token, authenticated=False, timeout=timeout,
        )
        if health_status != 200 or health.get("service") != "relayterm":
            return None
        catalog_status, catalog = request_json(base_url, "/v1/profiles", token, timeout=timeout)
        session_status, session_value = request_json(base_url, "/v1/sessions", token, timeout=timeout)
    except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError):
        return None
    if catalog_status != 200 or session_status != 200:
        return None
    try:
        version = int(catalog.get("version", 0) or 0)
    except (TypeError, ValueError):
        version = 0
    raw_sessions = session_value.get("sessions", [])
    sessions = tuple(dict(item) for item in raw_sessions if isinstance(item, dict))
    pid = listener_process_id(port) if os.name == "nt" else 0
    return BridgeProbe(
        str(host), int(port), str(health.get("bridgeGeneration", "") or ""),
        version, dict(catalog), sessions, pid,
    )


def port_is_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as stream:
        try:
            stream.bind((host, int(port)))
            return True
        except OSError:
            return False


def find_available_port(host: str, start: int, *, span: int = 100) -> int | None:
    for candidate in range(max(1, int(start)), min(65536, int(start) + max(1, int(span)))):
        if port_is_free(host, candidate):
            return candidate
    return None


def prepare_shadow_catalog(
    source: str | os.PathLike[str],
    destination: str | os.PathLike[str],
    catalog: dict[str, object] | None = None,
) -> Path:
    source_path = Path(source)
    destination_path = Path(destination)
    if source_path.exists():
        data = source_path.read_bytes()
    else:
        value = catalog if isinstance(catalog, dict) else {"version": 1, "profiles": []}
        data = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    atomic_write(destination_path, data)
    return destination_path


def drain_profile_ids(value: dict[str, object]) -> set[str]:
    result: set[str] = set()
    drains = value.get("drains", []) if isinstance(value, dict) else []
    if not isinstance(drains, list):
        return result
    for item in drains:
        if not isinstance(item, dict):
            continue
        ids = item.get("profileIds", [])
        if isinstance(ids, list):
            result.update(str(profile_id) for profile_id in ids if PROFILE_ID_RE.fullmatch(str(profile_id)))
    return result


def merge_session_snapshots(
    primary: Iterable[dict[str, object]],
    drains: Iterable[tuple[str, Iterable[dict[str, object]]]],
) -> tuple[dict[str, dict[str, object]], dict[str, str]]:
    sessions: dict[str, dict[str, object]] = {}
    routes: dict[str, str] = {}
    for raw in primary:
        profile_id = str(raw.get("profileId", "") or "")
        if profile_id:
            sessions[profile_id] = dict(raw)
    for base_url, values in drains:
        for raw in values:
            profile_id = str(raw.get("profileId", "") or "")
            if not profile_id or not bool(raw.get("running")):
                continue
            current = sessions.get(profile_id)
            if current is not None and bool(current.get("running")):
                current["bridgeConflict"] = True
                continue
            item = dict(raw)
            item["draining"] = True
            item["bridgeEndpoint"] = base_url
            sessions[profile_id] = item
            routes[profile_id] = base_url
    return sessions, routes


class _TcpRowOwnerPid(ctypes.Structure):
    _fields_ = [
        ("state", ctypes.c_uint32),
        ("local_addr", ctypes.c_uint32),
        ("local_port", ctypes.c_uint32),
        ("remote_addr", ctypes.c_uint32),
        ("remote_port", ctypes.c_uint32),
        ("pid", ctypes.c_uint32),
    ]


def listener_process_id(port: int) -> int:
    if os.name != "nt":
        return 0
    size = ctypes.c_uint32(0)
    api = ctypes.windll.iphlpapi.GetExtendedTcpTable
    api(None, ctypes.byref(size), False, socket.AF_INET, 3, 0)
    if not size.value:
        return 0
    buffer = ctypes.create_string_buffer(size.value)
    if api(buffer, ctypes.byref(size), False, socket.AF_INET, 3, 0) != 0:
        return 0
    count = ctypes.c_uint32.from_buffer_copy(buffer.raw[:4]).value
    offset = 4
    row_size = ctypes.sizeof(_TcpRowOwnerPid)
    for index in range(count):
        start = offset + index * row_size
        row = _TcpRowOwnerPid.from_buffer_copy(buffer.raw[start:start + row_size])
        if socket.ntohs(row.local_port & 0xFFFF) == int(port):
            return int(row.pid)
    return 0


def process_command_line(pid: int) -> str:
    if os.name != "nt" or int(pid) <= 0:
        return ""
    powershell = os.environ.get("RELAYTERM_POWERSHELL", "powershell.exe")
    script = (
        "$p=Get-CimInstance Win32_Process -Filter \"ProcessId=" + str(int(pid))
        + "\"; if($p){[Console]::Out.Write($p.CommandLine)}"
    )
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(
            [powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=4, check=False, creationflags=flags,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return completed.stdout.strip() if completed.returncode == 0 else ""


def process_parent_id(pid: int) -> int:
    if os.name != "nt" or int(pid) <= 0:
        return 0
    powershell = os.environ.get("RELAYTERM_POWERSHELL", "powershell.exe")
    script = (
        "$p=Get-CimInstance Win32_Process -Filter \"ProcessId=" + str(int(pid))
        + "\"; if($p){[Console]::Out.Write($p.ParentProcessId)}"
    )
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(
            [powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=4, check=False, creationflags=flags,
        )
        return int(completed.stdout.strip()) if completed.returncode == 0 else 0
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return 0


def verified_bridge_pid(port: int, expected_pid: int = 0) -> int:
    pid = listener_process_id(port)
    if pid <= 0 or (expected_pid > 0 and pid != int(expected_pid)):
        return 0
    command = process_command_line(pid).casefold()
    return pid if "bridge.relay_bridge" in command else 0


def terminate_drained_bridge(host: str, port: int, expected_pid: int = 0) -> bool:
    pid = verified_bridge_pid(port, expected_pid)
    if pid <= 0 or pid == os.getpid():
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if port_is_free(host, port):
            return True
        time.sleep(0.1)
    return port_is_free(host, port)


def verified_legacy_agent_pid(agent_pid: int) -> int:
    pid = int(agent_pid)
    if pid <= 0:
        return 0
    command = process_command_line(pid).replace("/", "\\").casefold()
    markers = ("\\pc\\agent.py", "-m pc.agent")
    return pid if any(marker in command for marker in markers) else 0


def terminate_legacy_agent(agent_pid: int) -> bool:
    pid = verified_legacy_agent_pid(agent_pid)
    if pid <= 0 or pid == os.getpid():
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not process_command_line(pid):
            return True
        time.sleep(0.1)
    return not bool(process_command_line(pid))


def rollback_live_drain(*, check_only: bool = False) -> dict[str, object]:
    store = DrainStateStore()
    state = store.load()
    primary = state.get("primary")
    drains = state.get("drains", [])
    if not isinstance(primary, dict) or not isinstance(drains, list) or not drains:
        return {"status": "no_live_drain"}
    token = TokenStore().load_or_create()
    host, port = str(primary["host"]), int(primary["port"])
    probe = probe_bridge(host, port, token)
    if probe is not None and probe.running_sessions:
        raise RuntimeError("drain_primary_has_running_sessions")
    primary_pid = int(primary.get("pid", 0) or 0)
    agent_pid = int(primary.get("agentPid", 0) or 0)
    old = drains[0] if isinstance(drains[0], dict) else None
    if old is None:
        raise RuntimeError("drain_rollback_route_missing")
    result = {
        "status": "ready",
        "primaryPort": port,
        "primaryPid": primary_pid,
        "agentPid": agent_pid,
        "restorePort": int(old["port"]),
    }
    if check_only:
        return result
    if not port_is_free(host, port):
        if not terminate_drained_bridge(host, port, primary_pid):
            raise RuntimeError("drain_primary_stop_failed")
    if agent_pid and not terminate_legacy_agent(agent_pid):
        raise RuntimeError("drain_sidecar_stop_failed")
    settings_store = SettingsStore()
    settings = settings_store.load()
    settings["host"], settings["port"] = str(old["host"]), int(old["port"])
    settings_store.save(settings)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    preserved: list[str] = []
    for path in (store.path, Path(str(primary.get("profilePath", "") or ""))):
        if path.exists():
            target = path.with_name(path.name + f".rollback-preserved-{stamp}")
            os.replace(path, target)
            preserved.append(str(target))
    result["status"] = "restored"
    result["preserved"] = preserved
    return result


__all__ = [
    "BRIDGE_GENERATION", "BridgeProbe", "DrainStateStore", "SHADOW_PROFILE_NAME",
    "drain_profile_ids", "find_available_port", "merge_session_snapshots",
    "port_is_free", "prepare_shadow_catalog", "probe_bridge", "process_parent_id",
    "request_json",
    "terminate_drained_bridge", "terminate_legacy_agent", "verified_bridge_pid",
    "verified_legacy_agent_pid", "rollback_live_drain",
]


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="RelayTerm drain-switch maintenance")
    parser.add_argument("--rollback-live", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if not args.rollback_live:
        parser.error("--rollback-live is required")
    print(json.dumps(rollback_live_drain(check_only=args.check), ensure_ascii=False))
