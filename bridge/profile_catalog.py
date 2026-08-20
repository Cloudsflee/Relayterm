"""Validated, atomically persisted PC-managed project profiles."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


SUPPORTED_SHELLS = ("pwsh", "powershell", "cmd", "wsl")
PROFILE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
MAX_NAME_LENGTH = 80
MAX_COMMAND_LENGTH = 8192


@dataclass(frozen=True)
class Profile:
    id: str
    name: str
    working_directory: str
    shell: str = "pwsh"
    startup_command: str = ""
    order: int = 0
    enabled: bool = True

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
        try:
            order = int(value.get("order", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError("profile_order_invalid") from exc
        if order < -1_000_000 or order > 1_000_000:
            raise ValueError("profile_order_invalid")
        enabled = value.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError("profile_enabled_invalid")
        return cls(profile_id, name, directory, shell, startup_command, order, enabled)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "workingDirectory": self.working_directory,
            "shell": self.shell,
            "startupCommand": self.startup_command,
            "order": self.order,
            "enabled": self.enabled,
        }


def sort_profiles(profiles: Iterable[Profile]) -> list[Profile]:
    return sorted(profiles, key=lambda item: (item.order, item.name.casefold(), item.id))


def default_profile(directory: str | os.PathLike[str] | None = None) -> Profile:
    working_directory = os.path.abspath(os.fspath(directory or os.getcwd()))
    name = Path(working_directory).name or "RelayTerm"
    return Profile("default", name[:MAX_NAME_LENGTH], working_directory)


class ProfileStore:
    """JSON profile catalog with migration, validation and corruption recovery."""

    VERSION = 1

    def __init__(self, path: str | os.PathLike[str], initial_directory: str | None = None) -> None:
        self.path = Path(path)
        self.initial_directory = os.path.abspath(initial_directory or os.getcwd())

    def _decode(self, raw: str) -> tuple[list[Profile], bool]:
        value = json.loads(raw)
        migrated = False
        if isinstance(value, list):
            items = value
            migrated = True
        elif isinstance(value, dict) and isinstance(value.get("profiles"), list):
            items = value["profiles"]
            migrated = int(value.get("version", 0) or 0) != self.VERSION
        else:
            raise ValueError("profile_catalog_invalid")
        profiles: list[Profile] = []
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
                                 ("order", len(profiles)), ("enabled", True)):
                if key not in migrated_item:
                    migrated_item[key] = default
                    migrated = True
            profile = Profile.from_dict(migrated_item)
            if profile.id in seen:
                raise ValueError("profile_id_duplicate")
            seen.add(profile.id)
            profiles.append(profile)
        return sort_profiles(profiles), migrated

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
        return validated

    def get(self, profile_id: str) -> Profile | None:
        for profile in self.load():
            if profile.id == profile_id:
                return profile
        return None

    def catalog(self) -> dict[str, Any]:
        profiles = self.load()
        encoded = json.dumps([item.to_dict() for item in profiles], ensure_ascii=False,
                             sort_keys=True, separators=(",", ":")).encode("utf-8")
        return {
            "version": self.VERSION,
            "revision": hashlib.sha256(encoded).hexdigest()[:16],
            "profiles": [item.to_dict() for item in profiles if item.enabled],
        }
