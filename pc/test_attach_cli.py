from __future__ import annotations

import json
import threading
import unittest
from io import StringIO
from unittest.mock import Mock, patch

from pc.attach_cli import (
    MAX_FRAME_BYTES,
    SPECIAL_KEYS,
    build_open_message,
    connect_stream,
    is_terminal_error,
    read_key,
    send_input_batch,
    send_terminal_closed,
    start_heartbeat,
    run,
)


class AttachCliTest(unittest.TestCase):
    def test_closed_pty_stops_receive_and_input_after_one_error(self):
        stream = Mock()
        stream.recv.side_effect = [
            json.dumps({"type": "ready"}),
            json.dumps({"type": "error", "code": "Pty is closed", "message": "Pty is closed"}),
            AssertionError("must stop receiving after fatal error"),
        ]
        output = StringIO()
        with (
            patch("pc.attach_cli.SettingsStore"),
            patch("pc.attach_cli.TokenStore"),
            patch("pc.attach_cli.connect_stream", return_value=stream),
            patch("pc.attach_cli.terminal_size", return_value=(120, 30)),
            patch("pc.attach_cli.client_id", return_value="desktop"),
            patch("pc.attach_cli.install_console_close_handler"),
            patch("pc.attach_cli.ConsoleMode"),
            patch("pc.attach_cli.start_heartbeat"),
            patch("pc.attach_cli.ctypes.windll.kernel32.SetConsoleTitleW"),
            patch("pc.attach_cli.read_key", return_value=None),
            patch("pc.attach_cli.sys.stderr", output),
        ):
            self.assertEqual(1, run("profile", host_override="127.0.0.1", port_override=1234))
        self.assertEqual(2, stream.recv.call_count)
        self.assertEqual(1, output.getvalue().count("Pty is closed"))
        stream.send_binary.assert_not_called()
        stream.close.assert_called_once()

    def test_open_errors_are_terminal_but_invalid_input_does_not_end_live_session(self):
        error = {"type": "error", "code": "codex_thread_unavailable"}
        self.assertTrue(is_terminal_error(error, False))
        self.assertTrue(is_terminal_error({"fatal": True}, True))
        self.assertFalse(is_terminal_error({"code": "signal_invalid"}, True))

    def test_handshake_timeout_is_cleared_for_idle_session(self):
        stream = Mock()
        with patch("pc.attach_cli.websocket.create_connection", return_value=stream) as create:
            result = connect_stream("ws://127.0.0.1:18766/v1/pty", "token")

        self.assertIs(stream, result)
        create.assert_called_once_with(
            "ws://127.0.0.1:18766/v1/pty",
            header=["Authorization: Bearer token"],
            timeout=10,
        )
        stream.settimeout.assert_called_once_with(None)

    def test_console_escape_reply_is_read_as_one_batch(self):
        reply = "\x1b]10;rgb:cccc/cccc/cccc\x1b\\"
        available = [True] + [True] * (len(reply) - 1) + [False]
        with (
            patch("pc.attach_cli.msvcrt.kbhit", side_effect=available),
            patch("pc.attach_cli.msvcrt.getwch", side_effect=list(reply)),
            patch("pc.attach_cli.INPUT_QUIET_SECONDS", 0),
        ):
            self.assertEqual(reply.encode(), read_key())

    def test_extended_key_is_mapped_inside_the_same_batch(self):
        with (
            patch("pc.attach_cli.msvcrt.kbhit", side_effect=[True, False]),
            patch("pc.attach_cli.msvcrt.getwch", side_effect=["\xe0", "H"]),
            patch("pc.attach_cli.INPUT_QUIET_SECONDS", 0),
        ):
            self.assertEqual(SPECIAL_KEYS["H"], read_key())

    def test_control_keys_inside_a_batch_preserve_order(self):
        stream = Mock()
        calls = []
        stream.send_binary.side_effect = lambda value: calls.append(("binary", value))
        stream.send.side_effect = lambda value: calls.append(("event", value))

        send_input_batch(stream, b"before\x03middle\x04after")

        self.assertEqual(
            [
                ("binary", b"before"),
                ("event", '{"type": "signal", "name": "INT"}'),
                ("binary", b"middle"),
                ("event", '{"type": "signal", "name": "EOF"}'),
                ("binary", b"after"),
            ],
            calls,
        )

    def test_large_console_burst_is_split_to_protocol_frames(self):
        stream = Mock()
        value = b"x" * (MAX_FRAME_BYTES + 7)
        send_input_batch(stream, value)
        self.assertEqual(
            [value[:MAX_FRAME_BYTES], value[MAX_FRAME_BYTES:]],
            [call.args[0] for call in stream.send_binary.call_args_list],
        )

    def test_terminal_close_message_preserves_pty(self):
        stream = Mock()
        stream.send.return_value = 1
        self.assertTrue(send_terminal_closed(stream))
        self.assertEqual({
            "type": "close", "terminate": False, "reason": "terminal_closed",
        }, json.loads(stream.send.call_args.args[0]))

    def test_heartbeat_sends_application_ping(self):
        stream = Mock()
        sent = threading.Event()
        stream.send.side_effect = lambda _value: sent.set() or 1
        stop = threading.Event()
        thread = start_heartbeat(stream, stop, interval=0.01)
        self.assertTrue(sent.wait(0.4))
        stop.set()
        thread.join(timeout=1)
        self.assertEqual("ping", json.loads(stream.send.call_args.args[0])["type"])

    def test_open_message_carries_explicit_codex_uuid(self):
        thread_id = "019c5a2f-87f6-7db0-babc-2bb3923347a3"
        value = build_open_message(
            "profile", "desktop", 140, 42, fresh=True, codex_thread_id=thread_id,
        )
        self.assertEqual(thread_id, value["codexThreadId"])
        self.assertFalse(value["resume"])


if __name__ == "__main__":
    unittest.main()
