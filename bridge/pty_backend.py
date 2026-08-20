"""Small cross-platform PTY adapter used by the RelayTerm bridge.

Windows deployments use :mod:`pywinpty` when it is installed so Codex gets a
real ConPTY/WinPTY console.  The fallback is intentionally useful for local
tests and development: it uses a Unix pseudo terminal where available and a
pipe-backed ``cmd.exe`` process on Windows machines that do not have the
optional wheel installed.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import threading
import uuid
from dataclasses import dataclass
from typing import Callable, Optional

OutputCallback = Callable[[bytes], None]
ExitCallback = Callable[[int], None]


@dataclass(frozen=True)
class PtyLaunchSpec:
    """Describes either the legacy wrapped command or a persistent profile shell."""

    mode: str
    shell: str
    cwd: str
    startup_command: str = ""

    @classmethod
    def legacy(cls, command: str, cwd: str) -> "PtyLaunchSpec":
        return cls("legacy", "cmd" if os.name == "nt" else "sh", cwd, command)

    @classmethod
    def profile(cls, shell: str, cwd: str, startup_command: str = "") -> "PtyLaunchSpec":
        return cls("profile", shell.strip().lower(), cwd, startup_command)


def _normalise_cwd(cwd: str | None) -> str:
    value = (cwd or "").strip()
    if value:
        candidate = os.path.abspath(os.path.expanduser(value))
        if os.path.isdir(candidate):
            return candidate
    return os.getcwd()


def _shell_command(spec: PtyLaunchSpec) -> str | list[str]:
    if spec.mode == "legacy" and os.name == "nt":
        # Keep the command as argv for pywinpty. Passing the /K payload inside
        # a quoted command string makes pywinpty preserve the quotes, so cmd
        # treats `"chcp ..."` as an executable name. UTF-8 is configured after
        # the PTY is attached, before the profile command is sent.
        return [os.environ.get("COMSPEC", "cmd.exe"), "/Q", "/D", "/K"]
    if spec.mode == "legacy":
        return os.environ.get("SHELL", "/bin/sh")
    if os.name == "nt":
        if spec.shell == "pwsh":
            return [shutil.which("pwsh.exe") or shutil.which("pwsh") or "pwsh.exe", "-NoLogo"]
        if spec.shell == "powershell":
            return [shutil.which("powershell.exe") or "powershell.exe", "-NoLogo"]
        if spec.shell == "cmd":
            return [os.environ.get("COMSPEC", "cmd.exe"), "/Q", "/D", "/K"]
        if spec.shell == "wsl":
            return [shutil.which("wsl.exe") or "wsl.exe", "--cd", spec.cwd]
        raise ValueError("profile_shell_invalid")
    if spec.shell == "pwsh" and shutil.which("pwsh"):
        return [shutil.which("pwsh") or "pwsh", "-NoLogo"]
    return os.environ.get("SHELL", "/bin/sh")


class PtyBackend:
    """A running interactive process with a byte-oriented interface."""

    def __init__(
        self,
        startup_command: str | PtyLaunchSpec,
        cwd: str,
        cols: int,
        rows: int,
        on_output: OutputCallback,
        on_exit: ExitCallback,
    ) -> None:
        if isinstance(startup_command, PtyLaunchSpec):
            self.launch_spec = startup_command
        else:
            self.launch_spec = PtyLaunchSpec.legacy(startup_command.strip() or "codex", cwd)
        if self.launch_spec.mode not in ("legacy", "profile"):
            raise ValueError("launch_mode_invalid")
        self.startup_command = self.launch_spec.startup_command.strip()
        if self.launch_spec.mode == "legacy" and not self.startup_command:
            self.startup_command = "codex"
        self.cwd = _normalise_cwd(self.launch_spec.cwd or cwd)
        self.launch_spec = PtyLaunchSpec(
            self.launch_spec.mode, self.launch_spec.shell, self.cwd, self.startup_command
        )
        self.cols = max(2, min(int(cols or 100), 400))
        self.rows = max(2, min(int(rows or 32), 200))
        self.on_output = on_output
        self.on_exit = on_exit
        self._lock = threading.RLock()
        self._transport_close_lock = threading.Lock()
        self._closed = False
        self._proc = None
        self._master: Optional[int] = None
        self._stdin = None
        self._reader: Optional[threading.Thread] = None
        self._using_winpty = False
        self.cwd_marker = "__RELAYTERM_PTY_CWD_" + uuid.uuid4().hex + "__"
        self.current_cwd = self.cwd
        self._output_pending = bytearray()
        self._start()

    @property
    def pid(self) -> int:
        process = self._proc
        value = getattr(process, "pid", None)
        if value is None:
            value = getattr(process, "pid", 0)
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    def _start(self) -> None:
        # pywinpty has had two public APIs over its lifetime.  Probe both so a
        # bridge upgraded in place continues to work with either wheel.
        if os.name == "nt":
            try:
                from winpty import PtyProcess  # type: ignore

                self._proc = PtyProcess.spawn(
                    _shell_command(self.launch_spec),
                    dimensions=(self.rows, self.cols),
                    cwd=self.cwd,
                )
                self._using_winpty = True
            except Exception:
                try:
                    import winpty  # type: ignore

                    pty = winpty.PTY(self.cols, self.rows)
                    command = _shell_command(self.launch_spec)
                    pty.spawn(command, cwd=self.cwd)
                    self._proc = pty
                    self._using_winpty = True
                except Exception:
                    # The optional native package is not available.  The
                    # pipe-backed fallback still gives deterministic protocol
                    # behaviour in CI and is adequate for simple commands.
                    self._start_pipe_process()
        else:
            self._start_unix_pty()

        if self._proc is None:
            raise RuntimeError("pty_start_failed")
        self._reader = threading.Thread(target=self._read_loop, name="relayterm-pty", daemon=True)
        self._reader.start()
        # Configure an interactive shell before handing it to the user.  The
        # command is written after the reader starts so the prompt cannot block
        # startup when the child emits an initial banner.
        if self.launch_spec.mode == "profile":
            if os.name == "nt" and self.launch_spec.shell == "cmd":
                self.write(b"chcp 65001>nul\r\n")
            if self.startup_command:
                ending = "\r\n" if os.name == "nt" else "\n"
                self.write((self.startup_command + ending).encode("utf-8", "replace"))
        elif os.name == "nt":
            self.write(b"chcp 65001>nul\r\n")
            self.write((self.startup_command + "\r\n").encode("utf-8", "replace"))
            # The shell is a transport wrapper. Once the profile's program
            # returns, leave the wrapper so the client receives an exit event
            # instead of an unrelated command prompt that never ends.
            self.write(("set \"__relayterm_code=%ERRORLEVEL%\"\r\n"
                        "echo " + self.cwd_marker + "%CD%\r\n"
                        "exit /b %__relayterm_code%\r\n").encode("utf-8", "replace"))
        else:
            self.write((self.startup_command + "\n__relayterm_code=$?\n"
                        "printf '\\n" + self.cwd_marker + "%s\\n' \"$PWD\"\n"
                        "exit $__relayterm_code\n").encode("utf-8", "replace"))

    def _start_pipe_process(self) -> None:
        command = _shell_command(self.launch_spec)
        if isinstance(command, str):
            argv = ([os.environ.get("COMSPEC", "cmd.exe"), "/Q", "/D", "/K", "chcp 65001>nul"]
                    if os.name == "nt" else [command])
        else:
            argv = command
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        self._proc = subprocess.Popen(
            argv,
            cwd=self.cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            creationflags=creationflags,
        )
        self._stdin = self._proc.stdin

    def _start_unix_pty(self) -> None:
        try:
            import pty
            import termios
            import fcntl
            import struct

            master, slave = pty.openpty()
            winsize = struct.pack("HHHH", self.rows, self.cols, 0, 0)
            fcntl.ioctl(slave, termios.TIOCSWINSZ, winsize)
            self._proc = subprocess.Popen(
                _shell_command(self.launch_spec),
                cwd=self.cwd,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                start_new_session=True,
                close_fds=True,
            )
            os.close(slave)
            self._master = master
        except Exception:
            self._start_pipe_process()

    def _read_loop(self) -> None:
        code = 0
        try:
            while not self._closed:
                if self._using_winpty:
                    try:
                        chunk = self._proc.read(4096)
                    except (EOFError, OSError, IOError):
                        break
                    if chunk is None or chunk == "":
                        if not self.is_alive():
                            break
                        continue
                    if isinstance(chunk, str):
                        chunk = chunk.encode("utf-8", "replace")
                    self._emit_output(bytes(chunk))
                    continue
                if self._master is not None:
                    try:
                        chunk = os.read(self._master, 65536)
                    except (OSError, IOError):
                        break
                else:
                    stream = getattr(self._proc, "stdout", None)
                    if stream is None:
                        break
                    chunk = stream.read(65536)
                if not chunk:
                    break
                self._emit_output(bytes(chunk))
        except Exception:
            # A closed PTY is normal during reconnect/termination.  The exit
            # event still reports the process status below.
            pass
        finally:
            if self._output_pending:
                pending = bytes(self._output_pending)
                self._output_pending.clear()
                try:
                    self.on_output(pending)
                except Exception:
                    pass
            try:
                if self._using_winpty:
                    status = getattr(self._proc, "exitstatus", None)
                    if callable(status):
                        status = status()
                    code = int(status if status is not None else 0)
                else:
                    code = int(self._proc.wait(timeout=1))
            except Exception:
                code = int(getattr(self._proc, "returncode", 0) or 0)
            self._close_winpty_transport()
            self._closed = True
            try:
                self.on_exit(code)
            except Exception:
                pass

    def _emit_output(self, data: bytes) -> None:
        """Remove the private cwd marker before bytes reach the phone."""

        if self.launch_spec.mode == "profile":
            self.on_output(data)
            return

        marker = self.cwd_marker.encode("ascii")
        combined = bytes(self._output_pending) + data
        self._output_pending.clear()
        visible = bytearray()
        while combined:
            index = combined.find(marker)
            if index < 0:
                # Keep only a possible marker prefix; normal output remains
                # streaming even when a shell emits small chunks.
                keep = 0
                upper = min(len(combined), max(0, len(marker) - 1))
                for candidate in range(upper, 0, -1):
                    if combined.endswith(marker[:candidate]):
                        keep = candidate
                        break
                if len(combined) > keep:
                    visible.extend(combined[:-keep] if keep else combined)
                    combined = combined[-keep:] if keep else b""
                self._output_pending.extend(combined)
                break
            visible.extend(combined[:index])
            remainder = combined[index + len(marker):]
            newline = -1
            for offset, value in enumerate(remainder):
                if value in (10, 13):
                    newline = offset
                    break
            if newline < 0:
                self._output_pending.extend(combined[index:])
                break
            path = remainder[:newline].decode("utf-8", "replace").strip()
            if path and os.path.isdir(path):
                self.current_cwd = os.path.abspath(path)
            skip = newline
            while skip < len(remainder) and remainder[skip] in (10, 13): skip += 1
            combined = remainder[skip:]
        if visible:
            self.on_output(bytes(visible))

    def is_alive(self) -> bool:
        if self._closed:
            return False
        try:
            if self._using_winpty:
                value = self._proc.isalive()
                return bool(value)
            return self._proc.poll() is None
        except Exception:
            return False

    def write(self, data: bytes) -> None:
        if not data:
            return
        with self._lock:
            if self._closed:
                raise RuntimeError("session_ended")
            if self._using_winpty:
                value = data.decode("utf-8", "replace")
                self._proc.write(value)
            elif self._master is not None:
                os.write(self._master, data)
            elif self._stdin is not None:
                self._stdin.write(data)
                self._stdin.flush()

    def send_interrupt(self) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                method = getattr(self._proc, "sendintr", None)
                if callable(method):
                    method()
                    return
            except Exception:
                pass
            try:
                if self._master is not None:
                    os.write(self._master, b"\x03")
                elif os.name == "nt" and hasattr(self._proc, "send_signal"):
                    self._proc.send_signal(signal.CTRL_BREAK_EVENT)
                elif self._stdin is not None:
                    self._stdin.write(b"\x03")
                    self._stdin.flush()
            except Exception:
                pass

    def send_eof(self) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                method = getattr(self._proc, "sendeof", None)
                if callable(method):
                    method()
                    return
            except Exception:
                pass
            try:
                if self._master is not None:
                    os.write(self._master, b"\x04")
                elif self._stdin is not None:
                    self._stdin.write(b"\x04")
                    self._stdin.flush()
            except Exception:
                pass

    def resize(self, rows: int, cols: int) -> None:
        rows = max(2, min(int(rows), 200))
        cols = max(2, min(int(cols), 400))
        with self._lock:
            self.rows, self.cols = rows, cols
            try:
                method = getattr(self._proc, "setwinsize", None)
                if callable(method):
                    # pywinpty uses (rows, cols), while the low-level PTY API
                    # uses (cols, rows).  Try the documented high-level form.
                    method(rows, cols)
                    return
            except Exception:
                pass
            if self._master is not None:
                try:
                    import termios
                    import fcntl
                    import struct

                    fcntl.ioctl(
                        self._master,
                        termios.TIOCSWINSZ,
                        struct.pack("HHHH", rows, cols, 0, 0),
                    )
                except Exception:
                    pass

    def terminate(self, force: bool = False) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                if self._using_winpty:
                    method = getattr(self._proc, "terminate", None)
                    if callable(method):
                        try:
                            method(force=force)
                        except TypeError:
                            method()
                    return
                if force:
                    self._proc.kill()
                else:
                    self._proc.terminate()
            except Exception:
                pass

    def _close_winpty_transport(self) -> None:
        if not self._using_winpty or self._proc is None:
            return
        with self._transport_close_lock:
            process = self._proc
            try:
                method = getattr(process, "close", None)
                if callable(method):
                    try:
                        method(force=False)
                    except TypeError:
                        method()
            except Exception:
                pass

            # PtyProcess.isalive() sets ``closed`` as soon as the child exits.
            # Its close() then skips the socket cleanup guarded by that flag,
            # so release the private transport handles independently as well.
            for name in ("fileobj", "_server"):
                stream = getattr(process, name, None)
                try:
                    if stream is not None:
                        stream.close()
                except Exception:
                    pass
            try:
                process.fd = -1
                process.closed = True
            except Exception:
                pass

    def close(self) -> None:
        self.terminate(False)
        self._close_winpty_transport()
        with self._lock:
            self._closed = True
            reader = self._reader
            try:
                if self._master is not None:
                    os.close(self._master)
                    self._master = None
            except OSError:
                pass
            for stream in (self._stdin, getattr(self._proc, "stdout", None)):
                try:
                    if stream is not None:
                        stream.close()
                except Exception:
                    pass
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=1)


def spawn_pty(
    startup_command: str | PtyLaunchSpec,
    cwd: str,
    cols: int,
    rows: int,
    on_output: OutputCallback,
    on_exit: ExitCallback,
) -> PtyBackend:
    """Factory kept separate so tests can inject a deterministic fake."""

    if isinstance(startup_command, PtyLaunchSpec):
        if len(startup_command.startup_command) > 8192:
            raise ValueError("startup_command_invalid")
    elif not isinstance(startup_command, str) or len(startup_command) > 8192:
        raise ValueError("startup_command_invalid")
    return PtyBackend(startup_command, cwd, cols, rows, on_output, on_exit)
