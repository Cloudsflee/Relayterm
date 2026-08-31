"""Validated, atomically persisted PC-managed project profiles."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SUPPORTED_SHELLS = ("pwsh", "powershell", "cmd", "wsl")
PROFILE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
MAX_NAME_LENGTH = 80
MAX_COMMAND_LENGTH = 8192
MAX_CODEX_ARGS = 64
MAX_CODEX_ARG_LENGTH = 2048
CODEX_MIGRATION_RE = re.compile(
    r"^codex(?:\.exe)?\s+resume\s+--last\s+--yolo$", re.IGNORECASE,
)


def parse_utc_timestamp(value: object) -> datetime | None:
    """Parse an ISO-8601 timestamp and return an aware UTC datetime."""
    if value in (None, "", "-"):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value, timezone.utc)
        except (TypeError, ValueError, OverflowError, OSError):
            return None
    try:
        text = str(value).strip()
        if "T" not in text and " " not in text:
            return None
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        # Activity records are required to be UTC.  Treat a legacy naive value
        # as UTC so one malformed record cannot break catalog ordering.
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def utc_iso_timestamp(value: datetime | float | None = None) -> str:
    """Return the canonical millisecond UTC representation used on disk."""
    if value is None:
        parsed = datetime.now(timezone.utc)
    elif isinstance(value, datetime):
        parsed = value
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        parsed = parsed.astimezone(timezone.utc)
    else:
        parsed = datetime.fromtimestamp(float(value), timezone.utc)
    return parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class Profile:
    id: str
    name: str
    working_directory: str
    shell: str = "pwsh"
    startup_command: str = ""
    pinned: bool = False
    enabled: bool = True
    launch_mode: str = "command"
    codex_args: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, value: dict[str, Any], *, require_path: bool = True) -> "Profile":
        if not isinstance(value, dict):
            raise ValueError("profile_invalid")
        profile_id = str(value.get("id", "")).strip()
        if not PROFILE_ID_RE.fullmatch(profile_id):
            raise ValueError("profile_id_invalid")
        name = str(value.get("name", "")).strip()
        if not name or len(name) > MAX_NAME_LENGTH:
            raise ValueError("profile_name_invalid")
        raw_directory = str(value.get("workingDirectory", value.get("cwd", ""))).strip()
        if not raw_directory:
            raise ValueError("profile_directory_required")
        directory = os.path.abspath(os.path.expandvars(os.path.expanduser(raw_directory)))
        if require_path and not os.path.isdir(directory):
            raise ValueError("profile_directory_missing")
        shell = str(value.get("shell", "pwsh")).strip().lower()
        if shell not in SUPPORTED_SHELLS:
            raise ValueError("profile_shell_invalid")
        startup_command = str(value.get("startupCommand", "")).strip()
        if len(startup_command) > MAX_COMMAND_LENGTH:
            raise ValueError("profile_command_too_long")
        pinned = value.get("pinned", False)
        if not isinstance(pinned, bool):
            raise ValueError("profile_pinned_invalid")
        enabled = value.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError("profile_enabled_invalid")
        launch_mode = str(value.get("launchMode", "command")).strip().lower()
        if launch_mode not in ("command", "codex"):
            raise ValueError("profile_launch_mode_invalid")
        raw_codex_args = value.get("codexArgs", [])
        if not isinstance(raw_codex_args, (list, tuple)) or len(raw_codex_args) > MAX_CODEX_ARGS:
            raise ValueError("profile_codex_args_invalid")
        codex_args: list[str] = []
        total = 0
        for raw_arg in raw_codex_args:
            if not isinstance(raw_arg, str):
                raise ValueError("profile_codex_args_invalid")
            arg = raw_arg.strip()
            if not arg or len(arg) > MAX_CODEX_ARG_LENGTH or any(char in arg for char in "\x00\r\n"):
                raise ValueError("profile_codex_args_invalid")
            if shell == "cmd" and any(char in arg for char in '%!^&|<>()"'):
                raise ValueError("profile_codex_args_invalid")
            total += len(arg)
            if total > MAX_COMMAND_LENGTH:
                raise ValueError("profile_codex_args_invalid")
            codex_args.append(arg)
        if launch_mode == "codex":
            if shell == "wsl":
                raise ValueError("profile_codex_shell_unsupported")
            startup_command = ""
        return cls(
            profile_id, name, directory, shell, startup_command, pinned, enabled,
            launch_mode, tuple(codex_args),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "workingDirectory": self.working_directory,
            "shell": self.shell,
            "startupCommand": self.startup_command,
            "pinned": self.pinned,
            "enabled": self.enabled,
            "launchMode": self.launch_mode,
            "codexArgs": list(self.codex_args),
        }


def sort_profiles(profiles: Iterable[Profile]) -> list[Profile]:
    """Return the canonical persisted order using a stable pinned partition."""
    values = list(profiles)
    return [item for item in values if item.pinned] + [item for item in values if not item.pinned]


def sort_profiles_by_recent(
    profiles: Iterable[Profile], recent_activity: dict[str, object] | None = None,
) -> list[Profile]:
    """Keep pinned items manual, then sort other items by recent activity."""
    activity = recent_activity or {}
    values = list(profiles)

    def key(value: tuple[int, Profile]) -> tuple[int, int, float, int, str, str]:
        position, item = value
        if item.pinned:
            return (0, 0, 0.0, position, item.name.casefold(), item.id)
        raw = activity.get(item.id)
        if isinstance(raw, dict):
            raw = raw.get("lastOpenedAt", raw.get("timestamp"))
        parsed = parse_utc_timestamp(raw)
        return (
            1,
            0 if parsed is not None else 1,
            -(parsed.timestamp() if parsed is not None else 0.0),
            position,
            item.name.casefold(),
            item.id,
        )

    return [item for _position, item in sorted(enumerate(values), key=key)]


def move_profile_up(profiles: Iterable[Profile], profile_id: str) -> list[Profile]:
    """Move a pinned profile up once, or append an unpinned one to the pinned group."""
    values = sort_profiles(profiles)
    index = next((i for i, item in enumerate(values) if item.id == profile_id), -1)
    if index < 0:
        return values
    target = values[index]
    if target.pinned:
        previous = next((i for i in range(index - 1, -1, -1) if values[i].pinned), -1)
        if previous >= 0:
            values[previous], values[index] = values[index], values[previous]
        return values
    values.pop(index)
    pinned_count = sum(1 for item in values if item.pinned)
    values.insert(pinned_count, replace(target, pinned=True))
    return values


def pin_profile(profiles: Iterable[Profile], profile_id: str) -> list[Profile]:
    """Pin a profile and place it first in the manual pinned group."""
    values = sort_profiles(profiles)
    index = next((i for i, item in enumerate(values) if item.id == profile_id), -1)
    if index < 0:
        return values
    target = values.pop(index)
    values.insert(0, replace(target, pinned=True))
    return values


def unpin_profile(profiles: Iterable[Profile], profile_id: str) -> list[Profile]:
    """Return a pinned profile to the recent-activity group."""
    values = sort_profiles(profiles)
    index = next((i for i, item in enumerate(values) if item.id == profile_id), -1)
    if index < 0 or not values[index].pinned:
        return values
    values[index] = replace(values[index], pinned=False)
    return sort_profiles(values)


class RecentActivityStore:
    """Versioned, atomically persisted profile open timestamps.

    The canonical file shape is ``{"version": 1, "lastOpenedAt": {...}}``.
    Readers also accept the names used by early development builds so an
    interrupted upgrade never discards valid activity data.
    """

    VERSION = 1

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    @staticmethod
    def _normalise_mapping(value: object) -> dict[str, str]:
        if not isinstance(value, dict):
            return {}
        result: dict[str, str] = {}
        for profile_id, timestamp in value.items():
            key = str(profile_id).strip()
            if isinstance(timestamp, dict):
                timestamp = timestamp.get("lastOpenedAt", timestamp.get("timestamp"))
            parsed = parse_utc_timestamp(timestamp)
            if key and parsed is not None:
                result[key] = utc_iso_timestamp(parsed)
        return result

    def _decode(self, raw: str) -> dict[str, str]:
        value = json.loads(raw)
        if isinstance(value, dict):
            version = value.get("version", self.VERSION)
            try:
                version = int(version)
            except (TypeError, ValueError):
                version = 0
            if version <= 0 or version > self.VERSION:
                raise ValueError("recent_activity_version_invalid")
            for key in ("lastOpenedAt", "activities", "profiles", "items", "records"):
                if key in value:
                    return self._normalise_mapping(value[key])
            # Accept a compact map from an unreleased pre-versioned build.
            return self._normalise_mapping({k: v for k, v in value.items() if k != "version"})
        # A bare map is harmless to migrate and is useful for old fixtures.
        if isinstance(value, list):
            result: dict[str, object] = {}
            for item in value:
                if isinstance(item, dict):
                    result.update(item)
            return self._normalise_mapping(result)
        raise ValueError("recent_activity_invalid")

    def load(self) -> dict[str, str]:
        with self._lock:
            if not self.path.exists():
                return {}
            try:
                return self._decode(self.path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
                # Activity is advisory; a damaged file must never hide the
                # profile catalog or prevent the bridge from starting.
                try:
                    corrupt = self.path.with_name(
                        f"{self.path.name}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
                    )
                    os.replace(self.path, corrupt)
                except OSError:
                    pass
                try:
                    self.save({})
                except OSError:
                    pass
                return {}

    def save(self, values: dict[str, object] | Iterable[tuple[str, object]]) -> dict[str, str]:
        mapping = dict(values) if not isinstance(values, dict) else values
        normalised = self._normalise_mapping(mapping)
        payload = {"version": self.VERSION, "lastOpenedAt": normalised}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            handle, temporary = tempfile.mkstemp(
                prefix=self.path.name + ".", suffix=".tmp", dir=self.path.parent,
            )
            try:
                with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                    json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            finally:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
        return dict(normalised)

    def record(self, profile_id: str, timestamp: object = None) -> str:
        key = str(profile_id or "").strip()
        if not key:
            raise ValueError("profile_id_required")
        parsed = (
            parse_utc_timestamp(timestamp)
            if timestamp is not None
            else datetime.now(timezone.utc)
        )
        if parsed is None:
            raise ValueError("recent_activity_timestamp_invalid")
        value = utc_iso_timestamp(parsed)
        with self._lock:
            mapping = self.load()
            mapping[key] = value
            self.save(mapping)
        return value

    def get(self, profile_id: str, default: str | None = None) -> str | None:
        return self.load().get(str(profile_id or "").strip(), default)

    def remove(self, profile_id: str) -> bool:
        key = str(profile_id or "").strip()
        if not key:
            return False
        with self._lock:
            mapping = self.load()
            if key not in mapping:
                return False
            del mapping[key]
            self.save(mapping)
            return True

    def remove_except(self, profile_ids: Iterable[str]) -> int:
        allowed = {str(item or "").strip() for item in profile_ids if str(item or "").strip()}
        with self._lock:
            mapping = self.load()
            stale = [key for key in mapping if key not in allowed]
            if stale:
                self.save({key: value for key, value in mapping.items() if key in allowed})
            return len(stale)

def default_profile(directory: str | os.PathLike[str] | None = None) -> Profile:
    working_directory = os.path.abspath(os.fspath(directory or os.getcwd()))
    name = Path(working_directory).name or "RelayTerm"
    return Profile("default", name[:MAX_NAME_LENGTH], working_directory)


class ProfileStore:
    """JSON profile catalog with migration, validation and corruption recovery."""

    VERSION = 3

    def __init__(self, path: str | os.PathLike[str], initial_directory: str | None = None) -> None:
        self.path = Path(path)
        self.initial_directory = os.path.abspath(initial_directory or os.getcwd())
        self.recent_activity = RecentActivityStore(self.path.with_name("recent_activity.json"))

    @property
    def recent_activity_path(self) -> Path:
        return self.recent_activity.path

    def _decode(self, raw: str) -> tuple[list[Profile], bool]:
        value = json.loads(raw)
        migrated = False
        if isinstance(value, list):
            items = value
            migrated = True
            legacy_order = True
            source_version = 1
        elif isinstance(value, dict) and isinstance(value.get("profiles"), list):
            items = value["profiles"]
            try:
                version = int(value.get("version", 0) or 0)
            except (TypeError, ValueError) as exc:
                raise ValueError("profile_catalog_version_invalid") from exc
            if version > self.VERSION:
                raise ValueError("profile_catalog_version_invalid")
            source_version = version
            # Only v1 used ``order`` as authoritative data.  A v2 catalog
            # already persists its intended array order and must keep it while
            # v3 launch fields are added.
            legacy_order = version <= 1
            migrated = version != self.VERSION
        else:
            raise ValueError("profile_catalog_invalid")
        profiles: list[Profile] = []
        legacy_profiles: list[tuple[int, Profile]] = []
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("profile_invalid")
            migrated_item = dict(item)
            if not migrated_item.get("id"):
                migrated_item["id"] = uuid.uuid4().hex
                migrated = True
            if "workingDirectory" not in migrated_item and "cwd" in migrated_item:
                migrated_item["workingDirectory"] = migrated_item.get("cwd")
                migrated = True
            for key, default in (("shell", "pwsh"), ("startupCommand", ""),
                                 ("pinned", False), ("enabled", True)):
                if key not in migrated_item:
                    migrated_item[key] = default
                    migrated = True
            if source_version < self.VERSION:
                command = str(migrated_item.get("startupCommand", "")).strip()
                if CODEX_MIGRATION_RE.fullmatch(command):
                    migrated_item["launchMode"] = "codex"
                    migrated_item["codexArgs"] = ["--yolo"]
                    migrated_item["startupCommand"] = ""
                else:
                    migrated_item.setdefault("launchMode", "command")
                    migrated_item.setdefault("codexArgs", [])
            else:
                for key, default in (("launchMode", "command"), ("codexArgs", [])):
                    if key not in migrated_item:
                        migrated_item[key] = default
                        migrated = True
            if "order" in migrated_item:
                migrated = True
            if legacy_order:
                try:
                    order = int(migrated_item.get("order", len(profiles)))
                except (TypeError, ValueError) as exc:
                    raise ValueError("profile_order_invalid") from exc
                if order < -1_000_000 or order > 1_000_000:
                    raise ValueError("profile_order_invalid")
            else:
                order = len(profiles)
            profile = Profile.from_dict(migrated_item)
            if profile.id in seen:
                raise ValueError("profile_id_duplicate")
            seen.add(profile.id)
            profiles.append(profile)
            legacy_profiles.append((order, profile))
        if legacy_order:
            profiles = [item for _order, item in sorted(
                legacy_profiles,
                key=lambda pair: (pair[0], pair[1].name.casefold(), pair[1].id),
            )]
        canonical = sort_profiles(profiles)
        if [item.id for item in canonical] != [item.id for item in profiles]:
            migrated = True
        return canonical, migrated

    def load(self, *, create: bool = True) -> list[Profile]:
        if not self.path.exists():
            profiles = [default_profile(self.initial_directory)] if create else []
            if create:
                self.save(profiles)
            return profiles
        try:
            profiles, migrated = self._decode(self.path.read_text(encoding="utf-8"))
            if migrated:
                self.save(profiles)
            return profiles
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
            if not create:
                raise
            suffix = time.strftime("%Y%m%d-%H%M%S")
            corrupt = self.path.with_name(f"{self.path.name}.corrupt-{suffix}")
            try:
                os.replace(self.path, corrupt)
            except OSError:
                pass
            profiles = [default_profile(self.initial_directory)]
            self.save(profiles)
            return profiles

    def save(self, profiles: Iterable[Profile | dict[str, Any]]) -> list[Profile]:
        validated: list[Profile] = []
        seen: set[str] = set()
        for value in profiles:
            profile = value if isinstance(value, Profile) else Profile.from_dict(value)
            # Revalidate dataclass instances so callers cannot bypass path checks.
            profile = Profile.from_dict(profile.to_dict())
            if profile.id in seen:
                raise ValueError("profile_id_duplicate")
            seen.add(profile.id)
            validated.append(profile)
        validated = sort_profiles(validated)
        payload = {"version": self.VERSION, "profiles": [item.to_dict() for item in validated]}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(prefix=self.path.name + ".", suffix=".tmp", dir=self.path.parent)
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
        # Saving the authoritative profile list is also the deletion boundary
        # for advisory activity records.
        try:
            self.recent_activity.remove_except(item.id for item in validated)
        except OSError:
            # Activity cleanup is advisory and must not make profile writes
            # fail after the authoritative catalog has been committed.
            pass
        return validated

    def get(self, profile_id: str) -> Profile | None:
        for profile in self.load():
            if profile.id == profile_id:
                return profile
        return None

    def catalog(self) -> dict[str, Any]:
        profiles = self.load()
        activity = self.recent_activity.load()
        encoded_profiles = []
        for item in profiles:
            encoded = item.to_dict()
            encoded["lastOpenedAt"] = activity.get(item.id)
            encoded_profiles.append(encoded)
        encoded = json.dumps(encoded_profiles, ensure_ascii=False,
                             sort_keys=True, separators=(",", ":")).encode("utf-8")
        return {
            "version": self.VERSION,
            "revision": hashlib.sha256(encoded).hexdigest()[:16],
            "profiles": [item for item in encoded_profiles if item.get("enabled")],
        }
