from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from bridge import relay_bridge
from bridge.profile_catalog import Profile, ProfileStore
from bridge.pty_backend import PtyBackend, PtyLaunchSpec, _child_environment, _shell_command


class FakeBackend:
    next_pid = 9000
    instances: list["FakeBackend"] = []

    def __init__(self, command, cwd, cols, rows, on_output, on_exit):
        self.pid = FakeBackend.next_pid
        FakeBackend.next_pid += 1
        self.command = command
        self.cwd = cwd
        self.cols = cols
        self.rows = rows
        self.on_output = on_output
        self.on_exit = on_exit
        self.dead = False
        FakeBackend.instances.append(self)
        on_output(b"boot\r\n")

    def write(self, value: bytes) -> None:
        self.on_output(b"in:" + value)

    def resize(self, rows: int, cols: int) -> None:
        self.rows, self.cols = rows, cols

    def send_interrupt(self) -> None:
        self.on_output(b"INT")

    def send_eof(self) -> None:
        self.on_output(b"EOF")

    def terminate(self, force: bool = False) -> None:
        if not self.dead:
            self.dead = True
            self.on_exit(137 if force else 0)

    def close(self) -> None:
        self.terminate(False)

    def is_alive(self) -> bool:
        return not self.dead


def read_exact(stream: socket.socket, count: int) -> bytes:
    data = bytearray()
    while len(data) < count:
        part = stream.recv(count - len(data))
        if not part:
            raise EOFError()
        data.extend(part)
    return bytes(data)


def read_frame(stream: socket.socket) -> tuple[int, bytes]:
    first, second = read_exact(stream, 2)
    length = second & 0x7F
    if length == 126:
        length = int.from_bytes(read_exact(stream, 2), "big")
    elif length == 127:
        length = int.from_bytes(read_exact(stream, 8), "big")
    return first & 0x0F, read_exact(stream, length)


def send_frame(stream: socket.socket, opcode: int, payload: bytes, *, fin: bool = True) -> None:
    mask = b"TEST"
    length = len(payload)
    if length < 126:
        size = bytes((0x80 | length,))
    elif length <= 0xFFFF:
        size = bytes((0x80 | 126,)) + length.to_bytes(2, "big")
    else:
        size = bytes((0x80 | 127,)) + length.to_bytes(8, "big")
    header = bytes((((0x80 if fin else 0) | opcode),)) + size
    masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
    stream.sendall(header + mask + masked)


def close_stream(stream: socket.socket) -> None:
    try:
        send_frame(stream, 1, json.dumps({"type": "close", "terminate": False}).encode())
    except OSError:
        pass
    try:
        stream.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    stream.close()


class PtyProtocolTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.old_token = relay_bridge.TOKEN
        cls.old_manager = relay_bridge.SESSION_MANAGER
        relay_bridge.TOKEN = "PTY_TOKEN"
        FakeBackend.instances.clear()
        relay_bridge.SESSION_MANAGER = relay_bridge.SessionManager(
            4, 60, backend_factory=FakeBackend, output_limit=1024
        )
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), relay_bridge.RelayHandler)
        cls.server.daemon_threads = True
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        relay_bridge.SESSION_MANAGER.shutdown()
        relay_bridge.SESSION_MANAGER = cls.old_manager
        relay_bridge.TOKEN = cls.old_token

    def connect(self, session_id: str) -> socket.socket:
        stream = socket.create_connection(("127.0.0.1", self.server.server_port), timeout=3)
        key = base64.b64encode(b"0123456789abcdef").decode()
        request = (
            f"GET /v1/pty?sessionId={session_id} HTTP/1.1\r\n"
            "Host: 127.0.0.1\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            "Sec-WebSocket-Version: 13\r\nSec-WebSocket-Key: " + key + "\r\n"
            "Authorization: Bearer PTY_TOKEN\r\n\r\n"
        )
        stream.sendall(request.encode())
        response = b""
        while b"\r\n\r\n" not in response:
            response += stream.recv(4096)
        self.assertIn(b"101 Switching Protocols", response)
        expected = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
        self.assertIn(expected, response)
        return stream

    def test_open_input_and_reconnect_buffer(self) -> None:
        first = self.connect("profile-a")
        send_frame(first, 1, json.dumps({"type": "open", "sessionId": "profile-a", "startupCommand": "fake"}).encode())
        first.settimeout(3)
        frames = [read_frame(first) for _ in range(2)]
        self.assertEqual("ready", json.loads(frames[0][1]).get("type"))
        self.assertTrue(any(op == 1 and json.loads(payload).get("type") == "ready" for op, payload in frames))
        self.assertTrue(any(op == 2 and b"boot" in payload for op, payload in frames))
        send_frame(first, 2, b"abc")
        opcode, payload = read_frame(first)
        self.assertEqual(2, opcode)
        self.assertEqual(b"in:abc", payload)
        send_frame(first, 1, json.dumps({"type": "close", "terminate": False}).encode())
        close_stream(first)

        second = self.connect("profile-a")
        send_frame(second, 1, json.dumps({"type": "open", "sessionId": "profile-a", "resume": True}).encode())
        second.settimeout(3)
        messages = []
        while len(messages) < 4:
            try:
                messages.append(read_frame(second))
            except socket.timeout:
                break
        ready = [json.loads(payload) for op, payload in messages if op == 1]
        self.assertTrue(any(item.get("resumed") is True for item in ready))
        self.assertTrue(any(op == 2 and b"boot" in payload for op, payload in messages))
        send_frame(second, 1, json.dumps({"type": "resize", "cols": 90, "rows": 20}).encode())
        time.sleep(0.05)
        backend = relay_bridge.SESSION_MANAGER.sessions["profile-a"].backend
        self.assertEqual((90, 20), (backend.cols, backend.rows))
        close_stream(second)

    def test_profile_ready_records_recent_open_and_desktop_close_keeps_pty(self) -> None:
        previous_store = relay_bridge.PROFILE_STORE
        with tempfile.TemporaryDirectory(prefix="relayterm-profile-ready-") as directory:
            relay_bridge.PROFILE_STORE = ProfileStore(Path(directory) / "profiles.json", directory)
            relay_bridge.PROFILE_STORE.save([Profile("recent-profile", "Recent", directory)])
            stream = self.connect("recent-profile")
            try:
                send_frame(stream, 1, json.dumps({
                    "type": "open", "profileId": "recent-profile",
                    "clientId": "desktop-recent", "clientType": "desktop",
                }).encode())
                stream.settimeout(3)
                ready = json.loads(read_frame(stream)[1])
                read_frame(stream)  # startup output
                self.assertEqual("ready", ready["type"])
                self.assertTrue(ready["lastOpenedAt"].endswith("Z"))
                self.assertEqual(
                    ready["lastOpenedAt"],
                    relay_bridge.PROFILE_STORE.catalog()["profiles"][0]["lastOpenedAt"],
                )
                send_frame(stream, 1, json.dumps({
                    "type": "close", "reason": "terminal_closed", "terminate": False,
                }).encode())
                for _ in range(30):
                    status = relay_bridge.SESSION_MANAGER.get("recent-profile").status()
                    if status["desktopState"] == "closed":
                        break
                    time.sleep(0.01)
                self.assertEqual("closed", status["desktopState"])
                self.assertFalse(status["desktopConnected"])
                self.assertFalse(relay_bridge.SESSION_MANAGER.get("recent-profile").backend.dead)
            finally:
                stream.close()
                relay_bridge.PROFILE_STORE = previous_store

    def test_missing_token_is_rejected(self) -> None:
        stream = socket.create_connection(("127.0.0.1", self.server.server_port), timeout=3)
        stream.sendall(b"GET /v1/pty HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n\r\n")
        response = stream.recv(1024)
        self.assertIn(b"401", response)
        stream.close()

    def test_failed_pty_start_rolls_back_deferred_session(self) -> None:
        def fail_backend(*_args):
            raise RuntimeError("test_start_failed")

        previous = relay_bridge.SESSION_MANAGER
        failed = relay_bridge.SessionManager(2, 60, backend_factory=fail_backend)
        relay_bridge.SESSION_MANAGER = failed
        stream = None
        try:
            stream = self.connect("failed-open")
            send_frame(stream, 1, json.dumps({
                "type": "open", "sessionId": "failed-open", "startupCommand": "fake",
                "clientId": "desktop-failed", "clientType": "desktop",
            }).encode())
            stream.settimeout(3)
            opcode, payload = read_frame(stream)
            self.assertEqual(1, opcode)
            self.assertEqual("test_start_failed", json.loads(payload)["code"])
            self.assertEqual([], failed.list_status())
        finally:
            if stream is not None:
                stream.close()
            failed.shutdown()
            relay_bridge.SESSION_MANAGER = previous

    def test_fragmented_binary_input_is_bounded_and_reassembled(self) -> None:
        stream = self.connect("fragmented")
        try:
            send_frame(stream, 1, json.dumps({
                "type": "open", "sessionId": "fragmented", "startupCommand": "fake",
            }).encode())
            stream.settimeout(3)
            read_frame(stream)
            read_frame(stream)
            send_frame(stream, 2, b"a", fin=False)
            send_frame(stream, 0, b"b", fin=False)
            send_frame(stream, 0, b"c")
            opcode, payload = read_frame(stream)
            self.assertEqual(2, opcode)
            self.assertEqual(b"in:abc", payload)
        finally:
            close_stream(stream)

    def test_desktop_resume_filters_terminal_generated_input_triggers(self) -> None:
        session_id = "desktop-replay"
        first = self.connect(session_id)
        send_frame(first, 1, json.dumps({
            "type": "open", "sessionId": session_id, "startupCommand": "fake",
            "clientId": "desktop-first", "clientType": "desktop",
        }).encode())
        first.settimeout(3)
        for _ in range(2):
            read_frame(first)
        unsafe = (
            b"visible-before\x1b[c\x1b[?1004h\x1b[?9001h"
            b"\x1b]10;?\x1b\\visible-after"
        )
        relay_bridge.SESSION_MANAGER.sessions[session_id].backend.on_output(unsafe)
        self.assertIn(b"visible-before", read_frame(first)[1])
        close_stream(first)

        second = self.connect(session_id)
        send_frame(second, 1, json.dumps({
            "type": "open", "sessionId": session_id, "resume": True,
            "clientId": "desktop-second", "clientType": "desktop",
        }).encode())
        second.settimeout(3)
        frames = [read_frame(second) for _ in range(2)]
        replayed = b"".join(payload for opcode, payload in frames if opcode == 2)
        self.assertIn(b"visible-beforevisible-after", replayed)
        self.assertNotIn(b"\x1b[c", replayed)
        self.assertNotIn(b"\x1b[?1004h", replayed)
        self.assertNotIn(b"\x1b[?9001h", replayed)
        self.assertNotIn(b"\x1b]10;?", replayed)
        close_stream(second)


@unittest.skipUnless(
    os.name == "nt" and importlib.util.find_spec("winpty") is not None,
    "Windows pywinpty is required for the native PTY smoke test",
)
class WindowsPtyBackendTest(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows terminal environment")
    def test_profile_pty_enables_truecolor_and_pwsh_ansi_rendering(self) -> None:
        with patch.dict(os.environ, {
            "NO_COLOR": "1", "TERM": "dumb", "COLORTERM": "",
        }, clear=False):
            environment = _child_environment()
        self.assertNotIn("NO_COLOR", environment)
        self.assertEqual("xterm-256color", environment["TERM"])
        self.assertEqual("truecolor", environment["COLORTERM"])
        command = _shell_command(PtyLaunchSpec.profile("pwsh", os.getcwd()))
        self.assertIn("$PSStyle.OutputRendering='Ansi'", command)

        output = bytearray()
        exited = threading.Event()
        backend = PtyBackend(
            PtyLaunchSpec.profile(
                "pwsh", os.getcwd(), 'Write-Output "`e[31mRELAYTERM_RED`e[0m"',
            ),
            os.getcwd(), 100, 32, output.extend, lambda _code: exited.set(),
        )
        try:
            deadline = time.monotonic() + 8
            expected = b"\x1b[31mRELAYTERM_RED\x1b[0m"
            while expected not in output and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertIn(expected, bytes(output))
            backend.write(b"exit\r\n")
            self.assertTrue(exited.wait(8))
        finally:
            backend.close()

    def test_child_sees_a_terminal(self) -> None:
        output = bytearray()
        backend = PtyBackend(
            "python -c \"import sys; print('RELAYTERM_TTY=' + str(sys.stdin.isatty()))\"",
            os.getcwd(),
            100,
            32,
            output.extend,
            lambda _code: None,
        )
        process = backend._proc
        try:
            deadline = time.monotonic() + 10
            while backend.is_alive() and time.monotonic() < deadline:
                time.sleep(0.1)
            time.sleep(0.2)
            self.assertIn(b"RELAYTERM_TTY=True", bytes(output))
            self.assertNotIn(b"stdin is not a terminal", bytes(output))
        finally:
            backend.close()
        self.assertEqual(-1, process.fileobj.fileno())
        self.assertEqual(-1, process._server.fileno())

    def test_profile_command_returns_to_pwsh_until_shell_exits(self) -> None:
        output = bytearray()
        exited = threading.Event()
        backend = PtyBackend(
            PtyLaunchSpec.profile(
                "pwsh", os.getcwd(), "Write-Output RELAYTERM_PROFILE_READY"
            ),
            os.getcwd(), 100, 32, output.extend, lambda _code: exited.set(),
        )
        try:
            deadline = time.monotonic() + 10
            while b"RELAYTERM_PROFILE_READY" not in output and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertIn(b"RELAYTERM_PROFILE_READY", bytes(output))
            self.assertTrue(backend.is_alive())
            backend.write(b"Write-Output RELAYTERM_AFTER_COMMAND\r\n")
            deadline = time.monotonic() + 5
            while b"RELAYTERM_AFTER_COMMAND" not in output and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertIn(b"RELAYTERM_AFTER_COMMAND", bytes(output))
            backend.write(b"exit\r\n")
            self.assertTrue(exited.wait(8))
        finally:
            backend.close()


if __name__ == "__main__":
    unittest.main()
