from __future__ import annotations

import tempfile
import unittest

from bridge.pty_backend import PtyLaunchSpec
from bridge.session_manager import SessionManager, replay_snapshot, sanitize_desktop_replay


class FakeBackend:
    next_pid = 12000

    def __init__(self, spec, cwd, cols, rows, on_output, on_exit):
        self.pid = FakeBackend.next_pid
        FakeBackend.next_pid += 1
        self.spec = spec
        self.cwd = cwd
        self.cols, self.rows = cols, rows
        self.on_output, self.on_exit = on_output, on_exit
        self.writes = []
        self.closed = False

    def write(self, value):
        self.writes.append(bytes(value))

    def resize(self, rows, cols):
        self.rows, self.cols = rows, cols

    def send_interrupt(self):
        self.writes.append(b"INT")

    def send_eof(self):
        self.writes.append(b"EOF")

    def terminate(self, force=False):
        if not self.closed:
            self.closed = True
            self.on_exit(137 if force else 0)

    def close(self):
        self.terminate(False)


class SessionManagerMultiAttachmentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="relayterm-session-")
        self.manager = SessionManager(4, 60, FakeBackend, 4096)
        spec = PtyLaunchSpec.profile("pwsh", self.directory.name, "")
        self.session, _ = self.manager.open_session(
            "project", spec, self.directory.name, 100, 30, True, profile_id="project"
        )

    def tearDown(self) -> None:
        self.manager.shutdown()
        self.directory.cleanup()

    def attach(self, client_id, client_type, cols=100, rows=30, binary_result=True):
        output, events, closed = [], [], []

        def binary(value):
            output.append(value)
            return binary_result

        attachment, _, _ = self.session.attach(
            binary, lambda value: events.append(value), lambda: closed.append(True),
            client_id=client_id, client_type=client_type, cols=cols, rows=rows,
        )
        return attachment, output, events, closed

    def test_broadcast_and_last_input_claims_control(self) -> None:
        desktop, desktop_output, desktop_events, _ = self.attach("desktop", "desktop", 120, 40)
        android, android_output, android_events, _ = self.attach("android", "android", 80, 24)
        backend = self.session.backend
        backend.on_output(b"shared")
        self.assertEqual([b"shared"], desktop_output)
        self.assertEqual([b"shared"], android_output)
        self.assertEqual("controller", self.session.role_for(desktop))

        self.session.resize_from(android, 20, 70)
        self.assertEqual((40, 120), (backend.rows, backend.cols))
        self.session.write_from(android, b"phone")
        self.assertEqual("controller", self.session.role_for(android))
        self.assertEqual((20, 70), (backend.rows, backend.cols))
        self.assertEqual(b"phone", backend.writes[-1])
        self.assertTrue(any(event["type"] == "control_changed" for event in desktop_events))
        self.assertTrue(any(event["type"] == "control_changed" for event in android_events))

    def test_controller_disconnect_prefers_desktop_and_applies_its_size(self) -> None:
        desktop, _, _, _ = self.attach("desktop", "desktop", 132, 44)
        android, _, _, _ = self.attach("android", "android", 75, 18)
        self.session.write_from(android, b"claim")
        self.session.detach(android, invoke_close=False)
        self.assertEqual("controller", self.session.role_for(desktop))
        self.assertEqual((44, 132), (self.session.backend.rows, self.session.backend.cols))

    def test_invalid_signal_does_not_claim_control(self) -> None:
        desktop, _, _, _ = self.attach("desktop", "desktop")
        android, _, _, _ = self.attach("android", "android")
        self.assertEqual("controller", self.session.role_for(desktop))

        with self.assertRaisesRegex(ValueError, "signal_invalid"):
            self.session.signal_from(android, "not-a-signal")

        self.assertEqual("controller", self.session.role_for(desktop))
        self.assertEqual("observer", self.session.role_for(android))

    def test_new_desktop_replaces_old_without_dropping_android(self) -> None:
        old, _, _, old_closed = self.attach("desktop-old", "desktop")
        android, _, _, _ = self.attach("android", "android")
        new, _, _, _ = self.attach("desktop-new", "desktop")
        self.assertTrue(old.closed)
        self.assertEqual([True], old_closed)
        self.assertIn(android.attachment_id, self.session.attachments)
        self.assertIn(new.attachment_id, self.session.attachments)
        self.assertEqual(2, len(self.session.attachments))

    def test_failed_slow_sink_only_detaches_it(self) -> None:
        slow, _, _, _ = self.attach("slow", "android", binary_result=False)
        desktop, output, _, _ = self.attach("desktop", "desktop")
        self.session.backend.on_output(b"next")
        self.assertNotIn(slow.attachment_id, self.session.attachments)
        self.assertIn(desktop.attachment_id, self.session.attachments)
        self.assertEqual([b"next"], output)

    def test_status_contains_catalog_fields(self) -> None:
        self.attach("desktop", "desktop")
        value = self.session.status()
        self.assertEqual("project", value["profileId"])
        self.assertTrue(value["running"])
        self.assertTrue(value["desktopConnected"])
        self.assertEqual("desktop", value["controller"]["clientType"])
        self.assertIn("createdAt", value)
        self.assertIn("lastActivityAt", value)


class TerminalReplayTest(unittest.TestCase):
    def test_desktop_resume_drops_queries_and_input_reporting_modes(self) -> None:
        value = (
            b"before\x1b[c\x1b[?1004h\x1b[?9001h"
            b"\x1b]10;?\x1b\\\x1b]11;?\x07\x1b[14t"
            b"\x1b[?25$p\x1bP+q544e\x1b\\after"
        )
        self.assertEqual(b"beforeafter", sanitize_desktop_replay(value))

    def test_desktop_resume_preserves_rendering_and_non_desktop_bytes(self) -> None:
        value = (
            b"\x1b]0;Project?\x07\x1b[31mred\x1b[0m"
            b"\x1b[?2004h\x1b[2J\x1b[H\x1b[32;1;1;2;2$x"
        )
        self.assertEqual(value, replay_snapshot(value, "desktop", True))
        query = b"text\x1b]10;?\x1b\\"
        self.assertEqual(query, replay_snapshot(query, "desktop", False))
        self.assertEqual(query, replay_snapshot(query, "android", True))

    def test_deferred_session_starts_after_attachment(self) -> None:
        with tempfile.TemporaryDirectory(prefix="relayterm-deferred-") as directory:
            manager = SessionManager(1, 60, FakeBackend, 4096)
            try:
                spec = PtyLaunchSpec.profile("pwsh", directory, "")
                session, resumed = manager.open_session(
                    "deferred", spec, directory, 100, 30, True,
                    profile_id="deferred", start_immediately=False,
                )
                received = []
                session.attach(
                    received.append, lambda _event: True,
                    client_id="desktop", client_type="desktop",
                )
                self.assertFalse(resumed)
                self.assertIsNone(session.backend)
                session.ensure_started()
                session.backend.on_output(b"live-startup")
                self.assertEqual([b"live-startup"], received)
            finally:
                manager.shutdown()

    def test_failed_deferred_start_can_be_removed_by_identity(self) -> None:
        def fail_backend(*_args):
            raise RuntimeError("pty_start_failed")

        with tempfile.TemporaryDirectory(prefix="relayterm-failed-") as directory:
            manager = SessionManager(1, 60, fail_backend, 4096)
            try:
                spec = PtyLaunchSpec.profile("pwsh", directory, "")
                session, _ = manager.open_session(
                    "failed", spec, directory, 100, 30, True,
                    profile_id="failed", start_immediately=False,
                )
                session.attach(
                    lambda _data: True, lambda _event: True,
                    client_id="desktop", client_type="desktop",
                )
                with self.assertRaisesRegex(RuntimeError, "pty_start_failed"):
                    session.ensure_started()
                self.assertEqual(0, session.status()["pid"])
                self.assertTrue(manager.remove_instance(session))
                self.assertIsNone(manager.get("failed"))
                self.assertFalse(manager.remove_instance(session))
            finally:
                manager.shutdown()

    def test_deferred_attachment_orders_snapshot_before_live_output(self) -> None:
        with tempfile.TemporaryDirectory(prefix="relayterm-order-") as directory:
            manager = SessionManager(1, 60, FakeBackend, 4096)
            try:
                spec = PtyLaunchSpec.profile("pwsh", directory, "")
                session, _ = manager.open_session(
                    "ordered", spec, directory, 100, 30, True,
                    profile_id="ordered", start_immediately=False,
                )
                received = []
                attachment, snapshot, _ = session.attach(
                    received.append, lambda _event: True,
                    client_id="desktop", client_type="desktop", defer_output=True,
                )
                session.ensure_started()
                session.backend.on_output(b"live-before-ready")
                self.assertEqual([], received)
                self.assertTrue(attachment.activate_output(b"snapshot" + snapshot))
                self.assertEqual([b"snapshot", b"live-before-ready"], received)
                session.backend.on_output(b"live-after-ready")
                self.assertEqual(b"live-after-ready", received[-1])
            finally:
                manager.shutdown()


if __name__ == "__main__":
    unittest.main()
