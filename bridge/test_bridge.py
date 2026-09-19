from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import shutil
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer

from bridge import relay_bridge


class BridgeProtocolTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        relay_bridge.TOKEN = "TOKEN"
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), relay_bridge.RelayHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def request(
        self,
        path: str,
        method: str = "GET",
        body: dict[str, object] | None = None,
        token: str | None = None,
    ) -> tuple[int, dict[str, object]]:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        if token is not None:
            request.add_header("Authorization", "Bearer " + token)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_health(self) -> None:
        status, value = self.request("/health")
        self.assertEqual(200, status)
        self.assertTrue(value["ok"])
        self.assertEqual("relayterm", value["service"])
        self.assertEqual(relay_bridge.BRIDGE_GENERATION, value["bridgeGeneration"])

    def test_authentication(self) -> None:
        status, body = self.request("/v1/exec", "POST", {"command": "echo no"})
        self.assertEqual(401, status)
        self.assertEqual("unauthorized", body["error"])

    def test_command_output_and_exit_code(self) -> None:
        command = (
            "echo hello & echo err 1>&2 & exit /b 3"
            if os.name == "nt"
            else "echo hello; echo err >&2; exit 3"
        )
        status, body = self.request(
            "/v1/exec",
            "POST",
            {"command": command, "sessionId": "test"},
            "TOKEN",
        )
        self.assertEqual(200, status)
        self.assertEqual("hello", str(body["stdout"]).strip())
        self.assertEqual("err", str(body["stderr"]).strip())
        self.assertEqual(3, body["exitCode"])

    def test_rejects_oversized_command(self) -> None:
        status, body = self.request(
            "/v1/exec",
            "POST",
            {"command": "x" * 8193},
            "TOKEN",
        )
        self.assertEqual(413, status)
        self.assertEqual("command_too_long", body["error"])

    def test_working_directory_is_kept_per_session(self) -> None:
        temporary = tempfile.mkdtemp(prefix="relayterm-")
        try:
            change = f'cd /d "{temporary}"' if os.name == "nt" else f'cd "{temporary}"'
            status, changed = self.request(
                "/v1/exec", "POST", {"command": change, "sessionId": "cwd-test"}, "TOKEN"
            )
            self.assertEqual(200, status)
            self.assertEqual(os.path.normcase(os.path.abspath(temporary)),
                             os.path.normcase(str(changed["cwd"])))
            status, listed = self.request(
                "/v1/exec", "POST", {"command": "cd", "sessionId": "cwd-test"}, "TOKEN"
            )
            self.assertEqual(200, status)
            self.assertEqual(os.path.normcase(os.path.abspath(temporary)),
                             os.path.normcase(str(listed["cwd"])))
        finally:
            shutil.rmtree(temporary, ignore_errors=True)

    def test_bounded_http_server_reuses_a_fixed_worker_pool(self) -> None:
        server = relay_bridge.RelayHTTPServer(
            ("127.0.0.1", 0), relay_bridge.RelayHandler, max_workers=3,
        )
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"

        def request_health(_index: int) -> int:
            with urllib.request.urlopen(base + "/health", timeout=3) as response:
                return response.status

        try:
            with ThreadPoolExecutor(max_workers=12) as workers:
                statuses = list(workers.map(request_health, range(120)))
            self.assertEqual([200] * 120, statuses)
            self.assertLessEqual(server._request_executor._max_workers, 3)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
