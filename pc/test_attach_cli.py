from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from pc.attach_cli import MAX_FRAME_BYTES, SPECIAL_KEYS, connect_stream, read_key, send_input_batch


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


if __name__ == "__main__":
    unittest.main()
