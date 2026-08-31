from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from bridge.profile_catalog import (
    Profile,
    ProfileStore,
    RecentActivityStore,
    parse_utc_timestamp,
    sort_profiles_by_recent,
)
from bridge.pty_backend import PtyLaunchSpec
from bridge.session_manager import SessionManager
from bridge.test_session_manager import FakeBackend


class RecentActivityStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="relayterm-recent-")
        self.root = Path(self.temp.name)
        self.path = self.root / "recent_activity.json"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_atomic_roundtrip_and_corruption_recovery(self) -> None:
        store = RecentActivityStore(self.path)
        value = store.record("a", "2026-08-01T01:02:03+08:00")
        self.assertTrue(value.endswith("Z"))
        self.assertEqual("2026-07-31T17:02:03.000Z", store.load()["a"])
        self.assertEqual(1, json.loads(self.path.read_text())["version"])
        self.assertFalse(list(self.root.glob("*.tmp")))
        self.path.write_text("{broken", encoding="utf-8")
        self.assertEqual({}, store.load())

    def test_profile_save_removes_deleted_activity(self) -> None:
        store = ProfileStore(self.root / "profiles.json", str(self.root))
        first = Profile("first", "First", str(self.root))
        second = Profile("second", "Second", str(self.root))
        store.save([first, second])
        store.recent_activity.record("first")
        store.save([second])
        self.assertNotIn("first", store.recent_activity.load())

    def test_catalog_exposes_activity_and_changes_revision(self) -> None:
        store = ProfileStore(self.root / "profiles.json", str(self.root))
        store.save([Profile("p", "Project", str(self.root))])
        before = store.catalog()
        self.assertIsNone(before["profiles"][0]["lastOpenedAt"])
        store.recent_activity.record("p", "2026-08-01T00:00:00Z")
        after = store.catalog()
        self.assertEqual("2026-08-01T00:00:00.000Z", after["profiles"][0]["lastOpenedAt"])
        self.assertNotEqual(before["revision"], after["revision"])

    def test_recent_sort_keeps_pins_manual_then_uses_catalog_fallbacks(self) -> None:
        profiles = [
            Profile("pin-z", "Pin Z", str(self.root), pinned=True),
            Profile("pin-a", "Pin A", str(self.root), pinned=True),
            Profile("z", "same", str(self.root)),
            Profile("a", "same", str(self.root)),
            Profile("missing-z", "Missing Z", str(self.root)),
            Profile("missing-a", "Missing A", str(self.root)),
        ]
        ordered = sort_profiles_by_recent(profiles, {
            "pin-z": "2026-07-01T00:00:00Z",
            "pin-a": "2026-09-01T00:00:00Z",
            "z": "2026-08-01T00:00:00Z",
            "a": "2026-08-01T00:00:00Z",
            "missing-z": "broken",
        })
        self.assertEqual(
            ["pin-z", "pin-a", "z", "a", "missing-z", "missing-a"],
            [item.id for item in ordered],
        )
        self.assertIsNone(parse_utc_timestamp("broken"))


class DesktopStateTest(unittest.TestCase):
    def test_desktop_close_and_heartbeat_keep_pty_and_android(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix="relayterm-desktop-state-")
        manager = SessionManager(2, 60, FakeBackend, 4096, desktop_timeout_seconds=1)
        try:
            session, _ = manager.open_session(
                "state", PtyLaunchSpec.profile("pwsh", temp.name, ""), temp.name,
                profile_id="state",
            )
            desktop, _, _ = session.attach(
                lambda _value: True, lambda _value: True,
                client_id="desktop", client_type="desktop",
            )
            android, _, _ = session.attach(
                lambda _value: True, lambda _value: True,
                client_id="android", client_type="android",
            )
            session.detach(desktop, invoke_close=False, reason="terminal_closed")
            status = session.status()
            self.assertEqual("closed", status["desktopState"])
            self.assertFalse(status["desktopConnected"])
            self.assertFalse(session.backend.closed)
            self.assertIn(android.attachment_id, session.attachments)
            # A stale desktop is removed by the manager, while Android stays.
            replacement, _, _ = session.attach(
                lambda _value: True, lambda _value: True,
                client_id="desktop-2", client_type="desktop",
            )
            replacement.last_received_at = time.monotonic() - 2
            manager.reap_idle()
            self.assertNotIn(replacement.attachment_id, session.attachments)
            self.assertIn(android.attachment_id, session.attachments)
        finally:
            manager.shutdown()
            temp.cleanup()


if __name__ == "__main__":
    unittest.main()
