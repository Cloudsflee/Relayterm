from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from bridge.profile_catalog import Profile, ProfileStore
from pc.config import SettingsStore, TokenStore, dpapi_protect, dpapi_unprotect


class ProfileStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="relayterm-profile-")
        self.root = Path(self.temporary.name)
        self.path = self.root / "data" / "profiles.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_migrates_sorts_and_writes_atomically(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps([
            {"id": "later", "name": "Later", "cwd": str(self.root), "order": 2},
            {"id": "first", "name": "First", "workingDirectory": str(self.root), "order": 1},
        ]), encoding="utf-8")
        store = ProfileStore(self.path, str(self.root))
        profiles = store.load()
        self.assertEqual(["first", "later"], [item.id for item in profiles])
        value = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(1, value["version"])
        self.assertEqual("pwsh", value["profiles"][0]["shell"])
        self.assertFalse(list(self.path.parent.glob("*.tmp")))

    def test_validation_and_duplicate_rejection(self) -> None:
        store = ProfileStore(self.path, str(self.root))
        valid = Profile("valid", "Valid", str(self.root), "cmd", "", 0, True)
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
