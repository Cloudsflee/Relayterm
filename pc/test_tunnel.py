from __future__ import annotations

import queue
import threading
import unittest
from unittest.mock import patch

from pc.tunnel import QuickTunnel


class _Output:
    def __init__(self) -> None:
        self.lines: queue.Queue[str | None] = queue.Queue()

    def __iter__(self):
        while True:
            line = self.lines.get(timeout=2)
            if line is None:
                return
            yield line

    def close(self) -> None:
        pass


class _Process:
    next_pid = 5000

    def __init__(self, *_args, **_kwargs) -> None:
        self.pid = _Process.next_pid
        _Process.next_pid += 1
        self.stdout = _Output()
        self.returncode: int | None = None
        self.finished = threading.Event()

    def poll(self):
        return self.returncode

    def terminate(self) -> None:
        self.finish(-15)

    def kill(self) -> None:
        self.finish(-9)

    def wait(self, timeout=None):
        if not self.finished.wait(timeout):
            raise TimeoutError()
        return self.returncode

    def finish(self, code: int) -> None:
        if self.finished.is_set():
            return
        self.returncode = code
        self.finished.set()
        self.stdout.lines.put(None)


class QuickTunnelTest(unittest.TestCase):
    def test_stop_then_start_ignores_old_reader_completion(self) -> None:
        processes: list[_Process] = []

        def create(*args, **kwargs):
            process = _Process(*args, **kwargs)
            processes.append(process)
            return process

        states: list[tuple[str, str]] = []
        with patch("pc.tunnel.subprocess.Popen", side_effect=create):
            tunnel = QuickTunnel(
                "cloudflared",
                "http://127.0.0.1:18765",
                lambda state, detail: states.append((state, detail)),
            )
            tunnel.start()
            first = processes[0]
            tunnel.stop()
            tunnel.start()
            second = processes[1]
            second.stdout.lines.put("INF https://current.trycloudflare.com ready\n")

            deadline = threading.Event()
            for _ in range(100):
                if tunnel.url:
                    break
                deadline.wait(0.01)

            self.assertEqual("https://current.trycloudflare.com", tunnel.url)
            self.assertNotIn(("error", "-15"), states)
            self.assertEqual(2, sum(state == "starting" for state, _ in states))
            self.assertEqual(1, sum(state == "stopped" for state, _ in states))
            self.assertIsNot(first, second)
            tunnel.stop()

    def test_missing_stdout_terminates_process_and_reports_error(self) -> None:
        process = _Process()
        process.stdout = None
        states: list[tuple[str, str]] = []
        with patch("pc.tunnel.subprocess.Popen", return_value=process):
            tunnel = QuickTunnel(
                "cloudflared",
                "http://127.0.0.1:18765",
                lambda state, detail: states.append((state, detail)),
            )
            tunnel.start()
            tunnel._thread.join(timeout=2)

        self.assertFalse(tunnel.running)
        self.assertIsNone(tunnel.process)
        self.assertIn(("error", "tunnel_stdout_unavailable"), states)

    def test_stdout_failure_terminates_process_and_reports_error(self) -> None:
        class BrokenOutput:
            def __iter__(self):
                raise OSError("test_read_failed")

            def close(self) -> None:
                pass

        process = _Process()
        process.stdout = BrokenOutput()
        states: list[tuple[str, str]] = []
        with patch("pc.tunnel.subprocess.Popen", return_value=process):
            tunnel = QuickTunnel(
                "cloudflared",
                "http://127.0.0.1:18765",
                lambda state, detail: states.append((state, detail)),
            )
            tunnel.start()
            tunnel._thread.join(timeout=2)

        self.assertFalse(tunnel.running)
        self.assertIsNone(tunnel.process)
        self.assertIn(("error", "test_read_failed"), states)


if __name__ == "__main__":
    unittest.main()
