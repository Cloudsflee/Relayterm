"""Local RelayTerm configuration and Windows DPAPI secret storage."""

from __future__ import annotations

import base64
import ctypes
import json
import os
import secrets
import tempfile
from ctypes import wintypes
from pathlib import Path
from typing import Any

from bridge.profile_catalog import Profile, ProfileStore


APP_NAME = "RelayTerm"


def data_directory() -> Path:
    root = os.environ.get("RELAYTERM_DATA_DIR")
    if root:
        return Path(root).expanduser().resolve()
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(local) / APP_NAME


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass


class SettingsStore:
    DEFAULTS: dict[str, Any] = {
        "version": 1,
        "autoStart": True,
        "host": "127.0.0.1",
        "port": 18765,
        "quickTunnel": False,
    }

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or data_directory() / "settings.json"

    def load(self) -> dict[str, Any]:
        result = dict(self.DEFAULTS)
        if self.path.exists():
            try:
                value = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(value, dict):
                    result.update(value)
            except (OSError, UnicodeError, json.JSONDecodeError):
                pass
        try:
            port = int(result.get("port", self.DEFAULTS["port"]))
        except (TypeError, ValueError):
            port = int(self.DEFAULTS["port"])
        result["port"] = max(1, min(port, 65535))
        host = str(result.get("host", self.DEFAULTS["host"]) or "").strip()
        result["host"] = host or str(self.DEFAULTS["host"])
        return result

    def save(self, value: dict[str, Any]) -> dict[str, Any]:
        merged = dict(self.DEFAULTS)
        merged.update(value)
        atomic_write(self.path, (json.dumps(merged, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        return merged


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _blob(value: bytes) -> tuple[_DataBlob, Any]:
    buffer = ctypes.create_string_buffer(value)
    return _DataBlob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer


def dpapi_protect(value: bytes) -> bytes:
    if os.name != "nt":
        return b"portable:" + base64.urlsafe_b64encode(value)
    source, source_buffer = _blob(value)
    entropy, entropy_buffer = _blob(b"RelayTerm/v1")
    output = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    ok = crypt32.CryptProtectData(
        ctypes.byref(source), APP_NAME, ctypes.byref(entropy), None, None, 0x01,
        ctypes.byref(output),
    )
    del source_buffer, entropy_buffer
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel32.LocalFree(output.pbData)


def dpapi_unprotect(value: bytes) -> bytes:
    if value.startswith(b"portable:"):
        return base64.urlsafe_b64decode(value[len(b"portable:"):])
    if os.name != "nt":
        raise ValueError("dpapi_platform_required")
    source, source_buffer = _blob(value)
    entropy, entropy_buffer = _blob(b"RelayTerm/v1")
    output = _DataBlob()
    description = wintypes.LPWSTR()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(source), ctypes.byref(description), ctypes.byref(entropy), None, None, 0x01,
        ctypes.byref(output),
    )
    del source_buffer, entropy_buffer
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        if description:
            kernel32.LocalFree(description)
        kernel32.LocalFree(output.pbData)


class TokenStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or data_directory() / "token.dpapi"

    def load_or_create(self) -> str:
        if self.path.exists():
            try:
                protected = base64.b64decode(self.path.read_bytes(), validate=True)
                token = dpapi_unprotect(protected).decode("ascii")
                if len(token) >= 32:
                    return token
            except (OSError, UnicodeError, ValueError):
                pass
        token = secrets.token_urlsafe(32)
        atomic_write(self.path, base64.b64encode(dpapi_protect(token.encode("ascii"))))
        return token


def profile_store(initial_directory: str | None = None) -> ProfileStore:
    return ProfileStore(data_directory() / "profiles.json", initial_directory)


__all__ = [
    "Profile", "ProfileStore", "SettingsStore", "TokenStore", "atomic_write",
    "data_directory", "dpapi_protect", "dpapi_unprotect", "profile_store",
]
