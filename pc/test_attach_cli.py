from __future__ import annotations

import json
import threading
import unittest
from unittest.mock import Mock, patch

from pc.attach_cli import (
    MAX_FRAME_BYTES,
    SPECIAL_KEYS,
    build_open_message,
    connect_stream,
    read_key,
    send_input_batch,
    send_terminal_closed,
    start_heartbeat,
)


class AttachCliTest(unittest.TestCase):
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
