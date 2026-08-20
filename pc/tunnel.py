"""On-demand Cloudflare Quick Tunnel process wrapper."""

from __future__ import annotations

import re
import subprocess
import threading
from typing import Callable


TUNNEL_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com", re.IGNORECASE)


class QuickTunnel:
    def __init__(self, executable: str, local_url: str,
                 callback: Callable[[str, str], None] | None = None) -> None:
        self.executable = executable
        self.local_url = local_url
        self.callback = callback or (lambda _state, _detail: None)
        self.process: subprocess.Popen[str] | None = None
        self.url = ""
        self._thread: threading.Thread | None = None
        self._generation = 0
        self._lock = threading.RLock()

    @property
    def running(self) -> bool:
        with self._lock:
            return self.process is not None and self.process.poll() is None

    def start(self) -> None:
        with self._lock:
            if self.process is not None and self.process.poll() is None:
                return
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            process = subprocess.Popen(
                [self.executable, "tunnel", "--protocol", "http2", "--url", self.local_url, "--no-autoupdate"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                errors="replace", creationflags=flags,
            )
            self._generation += 1
            generation = self._generation
            self.process = process
            self.url = ""
            thread = threading.Thread(
                target=self._read,
                args=(process, generation),
                name="relayterm-cloudflared",
                daemon=True,
            )
            self._thread = thread
        self._notify("starting", "")
        thread.start()

    def _notify(self, state: str, detail: str) -> None:
        try:
            self.callback(state, detail)
        except Exception:
            pass

    def _read(self, process: subprocess.Popen[str], generation: int) -> None:
        failure = ""
        try:
            if process.stdout is None:
                raise RuntimeError("tunnel_stdout_unavailable")
            for line in process.stdout:
                match = TUNNEL_URL_RE.search(line)
                if match:
                    url = match.group(0).rstrip("/")
                    with self._lock:
                        if self.process is not process or self._generation != generation:
                            return
                        if url == self.url:
                            continue
                        self.url = url
                    self._notify("ready", url)
            code = process.wait()
        except Exception as exc:
            failure = str(exc) or type(exc).__name__
            try:
                alive = process.poll() is None
            except Exception:
                # A broken process wrapper must be treated as alive so the
                # cleanup attempt still gets a chance to terminate it.
                alive = True
            if alive:
                try:
                    process.terminate()
                    process.wait(timeout=5)
                except Exception:
                    try:
                        process.kill()
                        process.wait(timeout=3)
                    except Exception:
                        pass
            try:
                code = process.poll()
            except Exception:
                code = -1
        finally:
            try:
                if process.stdout is not None:
                    process.stdout.close()
            except Exception:
                pass
        with self._lock:
            if self.process is not process or self._generation != generation:
                return
            self.process = None
            self.url = ""
        if failure:
            self._notify("error", failure)
        else:
            self._notify("stopped" if code == 0 else "error", str(code))

    def stop(self) -> None:
        with self._lock:
            process = self.process
            thread = self._thread
            self.process = None
            self._thread = None
            self.url = ""
            self._generation += 1
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1)
        self._notify("stopped", "")
