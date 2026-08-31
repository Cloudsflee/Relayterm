from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from bridge.profile_catalog import (
    Profile,
    ProfileStore,
    move_profile_up,
    pin_profile,
    unpin_profile,
)
from pc.config import SettingsStore, TokenStore, dpapi_protect, dpapi_unprotect


class ProfileStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="relayterm-profile-")
        self.root = Path(self.temporary.name)
        self.path = self.root / "data" / "profiles.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_v1_migrates_order_once_to_v3_array_order(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"version": 1, "profiles": [
            {"id": "later", "name": "Later", "cwd": str(self.root), "order": 2},
            {"id": "first", "name": "First", "workingDirectory": str(self.root), "order": 1},
        ]}), encoding="utf-8")
        store = ProfileStore(self.path, str(self.root))
        profiles = store.load()
        self.assertEqual(["first", "later"], [item.id for item in profiles])
        value = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(3, value["version"])
        self.assertEqual("pwsh", value["profiles"][0]["shell"])
        self.assertFalse(value["profiles"][0]["pinned"])
        self.assertNotIn("order", json.dumps(value))
        self.assertFalse(list(self.path.parent.glob("*.tmp")))

        # Version 3 reloads preserve the persisted array even when names would
        # have changed the old order/name tie-break.
        value["profiles"][0]["name"] = "Zulu"
        value["profiles"][1]["name"] = "Alpha"
        self.path.write_text(json.dumps(value), encoding="utf-8")
        self.assertEqual(["first", "later"], [item.id for item in store.load()])

    def test_v2_preserves_array_order_and_migrates_only_known_codex_command(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"version": 2, "profiles": [
            {
                "id": "codex", "name": "Codex", "workingDirectory": str(self.root),
                "shell": "pwsh", "startupCommand": "codex resume --last --yolo",
                "order": 999, "pinned": False, "enabled": True,
            },
            {
                "id": "custom", "name": "Custom", "workingDirectory": str(self.root),
                "shell": "cmd", "startupCommand": "codex resume --last --search",
                "order": -999, "pinned": False, "enabled": True,
            },
        ]}), encoding="utf-8")
        profiles = ProfileStore(self.path, str(self.root)).load()
        self.assertEqual(["codex", "custom"], [item.id for item in profiles])
        self.assertEqual("codex", profiles[0].launch_mode)
        self.assertEqual(("--yolo",), profiles[0].codex_args)
        self.assertEqual("", profiles[0].startup_command)
        self.assertEqual("command", profiles[1].launch_mode)
        self.assertEqual("codex resume --last --search", profiles[1].startup_command)
        value = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(3, value["version"])
        self.assertNotIn("order", json.dumps(value))

    def test_save_normalises_pinned_partition_and_revision_tracks_moves(self) -> None:
        store = ProfileStore(self.path, str(self.root))
        values = [
            Profile("plain-a", "Plain A", str(self.root)),
            Profile("pin-a", "Pin A", str(self.root), pinned=True),
            Profile("pin-b", "Pin B", str(self.root), pinned=True),
            Profile("plain-b", "Plain B", str(self.root)),
        ]
        saved = store.save(values)
        self.assertEqual(["pin-a", "pin-b", "plain-a", "plain-b"], [item.id for item in saved])
        first_revision = store.catalog()["revision"]

        saved = store.save(move_profile_up(saved, "plain-b"))
        self.assertEqual(["pin-a", "pin-b", "plain-b", "plain-a"], [item.id for item in saved])
        self.assertTrue(saved[2].pinned)
        second_revision = store.catalog()["revision"]
        self.assertNotEqual(first_revision, second_revision)

        saved = store.save(move_profile_up(saved, "plain-b"))
        self.assertEqual(["pin-a", "plain-b", "pin-b", "plain-a"], [item.id for item in saved])
        saved = store.save(pin_profile(saved, "plain-a"))
        self.assertEqual(["plain-a", "pin-a", "plain-b", "pin-b"], [item.id for item in saved])
        saved = store.save(unpin_profile(saved, "plain-b"))
        self.assertEqual(["plain-a", "pin-a", "pin-b", "plain-b"], [item.id for item in saved])
        self.assertFalse(saved[-1].pinned)
        self.assertNotIn("order", self.path.read_text(encoding="utf-8"))

    def test_validation_and_duplicate_rejection(self) -> None:
        store = ProfileStore(self.path, str(self.root))
        valid = Profile("valid", "Valid", str(self.root), "cmd", "", False, True)
        store.save([valid])
        with self.assertRaisesRegex(ValueError, "profile_id_duplicate"):
            store.save([valid, valid])
        with self.assertRaisesRegex(ValueError, "profile_directory_missing"):
            store.save([Profile("bad", "Bad", str(self.root / "missing"))])
        with self.assertRaisesRegex(ValueError, "profile_shell_invalid"):
            Profile.from_dict({"id": "bad", "name": "Bad", "workingDirectory": str(self.root), "shell": "fish"})

    def test_corrupt_file_is_preserved_and_recovered(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{broken", encoding="utf-8")
        profiles = ProfileStore(self.path, str(self.root)).load()
        self.assertEqual(1, len(profiles))
        self.assertEqual("default", profiles[0].id)
        self.assertEqual(1, len(list(self.path.parent.glob("profiles.json.corrupt-*"))))

    def test_dpapi_and_persistent_token_roundtrip(self) -> None:
        value = b"relayterm-secret"
        self.assertEqual(value, dpapi_unprotect(dpapi_protect(value)))
        store = TokenStore(self.root / "token.dpapi")
        first = store.load_or_create()
        second = store.load_or_create()
        self.assertEqual(first, second)
        self.assertGreaterEqual(len(first), 32)
        self.assertNotIn(first.encode("ascii"), store.path.read_bytes())

    def test_invalid_settings_values_fall_back_without_crashing(self) -> None:
        settings = self.root / "settings.json"
        settings.write_text('{"port":"broken","host":""}', encoding="utf-8")
        value = SettingsStore(settings).load()
        self.assertEqual(18765, value["port"])
        self.assertEqual("127.0.0.1", value["host"])


if __name__ == "__main__":
    unittest.main()
