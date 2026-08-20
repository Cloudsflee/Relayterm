from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stdout
from http.server import ThreadingHTTPServer
from io import StringIO
from pathlib import Path
from urllib.parse import quote

from bridge import relay_bridge
from bridge.profile_catalog import Profile, ProfileStore
from bridge.pty_backend import PtyLaunchSpec
from bridge.session_manager import SessionManager
from bridge.test_session_manager import FakeBackend


class CatalogApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory(prefix="relayterm-api-")
        cls.root = Path(cls.temporary.name)
        cls.old_token = relay_bridge.TOKEN
        cls.old_store = relay_bridge.PROFILE_STORE
        cls.old_manager = relay_bridge.SESSION_MANAGER
        cls.old_pairing = relay_bridge.PAIRING_MANAGER
        relay_bridge.TOKEN = "CATALOG_TOKEN"
        relay_bridge.PROFILE_STORE = ProfileStore(cls.root / "profiles.json", str(cls.root))
        relay_bridge.PROFILE_STORE.save([Profile("project", "Project", str(cls.root), "pwsh", "", 0, True)])
        relay_bridge.SESSION_MANAGER = SessionManager(4, 60, FakeBackend, 4096)
        relay_bridge.PAIRING_MANAGER = relay_bridge.PairingManager(120)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), relay_bridge.RelayHandler)
        cls.server.daemon_threads = True
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        relay_bridge.SESSION_MANAGER.shutdown()
        relay_bridge.TOKEN = cls.old_token
        relay_bridge.PROFILE_STORE = cls.old_store
        relay_bridge.SESSION_MANAGER = cls.old_manager
        relay_bridge.PAIRING_MANAGER = cls.old_pairing
        cls.temporary.cleanup()

    def request(self, path, method="GET", body=None, token="CATALOG_TOKEN"):
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(self.base + path, data=data, method=method)
        if token is not None:
            request.add_header("Authorization", "Bearer " + token)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=4) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def request_raw(self, path, token=None):
        request = urllib.request.Request(self.base + path, method="GET")
        if token is not None:
            request.add_header("Authorization", "Bearer " + token)
        try:
            with urllib.request.urlopen(request, timeout=4) as response:
                return response.status, response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode("utf-8")

    def test_catalog_and_session_status_require_auth(self) -> None:
        self.assertEqual(401, self.request("/v1/profiles", token=None)[0])
        status, catalog = self.request("/v1/profiles")
        self.assertEqual(200, status)
        self.assertEqual("project", catalog["profiles"][0]["id"])
        spec = PtyLaunchSpec.profile("pwsh", str(self.root), "")
        relay_bridge.SESSION_MANAGER.open_session(
            "project", spec, str(self.root), profile_id="project"
        )
        status, sessions = self.request("/v1/sessions")
        self.assertEqual(200, status)
        self.assertEqual("project", sessions["sessions"][0]["profileId"])

    def test_terminate_and_pairing_are_one_use(self) -> None:
        spec = PtyLaunchSpec.profile("pwsh", str(self.root), "")
        relay_bridge.SESSION_MANAGER.open_session(
            "project", spec, str(self.root), resume=False, profile_id="project"
        )
        status, value = self.request("/v1/sessions/project/terminate", "POST", {"force": True})
        self.assertEqual(200, status)
        self.assertTrue(value["ok"])

        status, challenge = self.request(
            "/v1/pairing/challenges", "POST", {"endpoint": "https://relay.example"}
        )
        self.assertEqual(201, status)
        code = challenge["challenge"]
        log_capture = StringIO()
        with redirect_stdout(log_capture):
            page_status, _ = self.request_raw("/pair/" + code, token=None)
        self.assertEqual(200, page_status)
        self.assertNotIn(code, log_capture.getvalue())
        self.assertIn("/pair/[redacted]", log_capture.getvalue())
        status, paired = self.request(
            "/v1/pairing/exchange", "POST", {"challenge": code}, token=None
        )
        self.assertEqual(200, status)
        self.assertEqual("CATALOG_TOKEN", paired["token"])
        self.assertEqual(410, self.request(
            "/v1/pairing/exchange", "POST", {"challenge": code}, token=None
        )[0])

    def test_expired_pairing_is_removed(self) -> None:
        manager = relay_bridge.PairingManager(120)
        code, _ = manager.create("https://relay.example")
        with manager._lock:
            manager._items[code] = (time.monotonic() - 1, "https://relay.example")
        self.assertIsNone(manager.inspect(code))
        self.assertIsNone(manager.exchange(code))

    def test_terminate_survives_catalog_removal_and_decodes_profile_id(self) -> None:
        profile_id = "removed:project"
        spec = PtyLaunchSpec.profile("pwsh", str(self.root), "")
        session, _ = relay_bridge.SESSION_MANAGER.open_session(
            profile_id, spec, str(self.root), resume=False, profile_id=profile_id,
        )
        self.assertNotIn(profile_id, {
            item["id"] for item in relay_bridge.PROFILE_STORE.catalog()["profiles"]
        })
        status, value = self.request(
            "/v1/sessions/" + quote(profile_id, safe="") + "/terminate",
            "POST",
            {"force": True},
        )
        self.assertEqual(200, status)
        self.assertTrue(value["ok"])
        self.assertTrue(session.ended)


if __name__ == "__main__":
    unittest.main()
