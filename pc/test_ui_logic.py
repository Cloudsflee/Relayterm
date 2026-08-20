from __future__ import annotations

import datetime as dt
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from bridge.profile_catalog import Profile
from pc.agent import (
    _console_python_executable,
    _focus_window,
    _select_terminal_window,
    _terminal_title_matches,
    button_availability,
    format_activity,
    profile_display_state,
)


class UiLogicTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="relayterm-ui-")
        self.profile = Profile("p", "Project", self.temp.name, "pwsh", "", 2, True)
        self.now = dt.datetime(2026, 8, 11, 14, 32, tzinfo=dt.timezone.utc)

    def tearDown(self):
        self.temp.cleanup()

    def test_activity_formats_relative_and_calendar_times(self):
        self.assertEqual("刚刚", format_activity("2026-08-11T14:31:45+00:00", self.now))
        self.assertEqual("5 分钟前", format_activity("2026-08-11T14:27:00+00:00", self.now))
        self.assertEqual("今天 12:32", format_activity("2026-08-11T12:32:00+00:00", self.now))
        self.assertEqual("08-10 19:20", format_activity("2026-08-10T19:20:00+00:00", self.now))
        self.assertEqual("-", format_activity("broken", self.now))

    def test_fixed_state_vocabulary(self):
        self.assertEqual(("未运行", "-"), profile_display_state(self.profile, None))
        recent = dt.datetime.now(dt.timezone.utc).isoformat()
        self.assertEqual(("运行中", "刚刚"), profile_display_state(self.profile, {"running": True, "lastActivityAt": recent}))
        self.assertEqual(("桌面已连接", "刚刚"), profile_display_state(self.profile, {"running": True, "desktopConnected": True, "lastActivityAt": recent}))
        self.assertEqual(("已退出", "-"), profile_display_state(self.profile, {"running": False}))
        disabled = Profile(self.profile.id, self.profile.name, self.profile.working_directory, enabled=False)
        self.assertEqual(("已禁用", "-"), profile_display_state(disabled, {"running": True}))

    def test_button_enablement(self):
        self.assertEqual({"edit": False, "delete": False, "stop": False, "open": False}, button_availability(None, None, True))
        running = {"running": True}
        self.assertEqual({"edit": True, "delete": True, "stop": True, "open": True}, button_availability(self.profile, running, True))
        self.assertFalse(button_availability(self.profile, running, False)["open"])
        disabled = Profile(self.profile.id, self.profile.name, self.profile.working_directory, enabled=False)
        self.assertFalse(button_availability(disabled, running, True)["open"])

    def test_terminal_title_accepts_attach_status_prefix(self):
        titles = ("安卓", "RelayTerm - default")
        self.assertTrue(_terminal_title_matches("⠹ 安卓", titles))
        self.assertTrue(_terminal_title_matches("RelayTerm - default", titles))
        self.assertTrue(_terminal_title_matches("RelayTerm - default [controller]", titles))
        self.assertTrue(_terminal_title_matches("Administrator: RelayTerm - default [controller]", titles))
        self.assertFalse(_terminal_title_matches("subapi", titles))

    def test_terminal_title_uses_token_boundaries_for_short_project_names(self):
        self.assertTrue(_terminal_title_matches("sec [controller]", ("sec",)))
        self.assertFalse(_terminal_title_matches("Windows Security", ("sec",)))

    @unittest.skipUnless(os.name == "nt", "Windows executable names")
    def test_attach_uses_console_python_when_launcher_uses_pythonw(self):
        root = Path(self.temp.name)
        pythonw = root / "pythonw.exe"
        python = root / "python.exe"
        pythonw.touch()
        python.touch()
        self.assertEqual(str(python), _console_python_executable(pythonw))
        python.unlink()
        self.assertEqual(str(pythonw), _console_python_executable(pythonw))

    def test_terminal_window_selection_prefers_new_and_foreground_windows(self):
        previous = {10: "Unrelated", 20: "Other"}
        self.assertEqual(30, _select_terminal_window(
            [(30, "Codex"), (10, "Unrelated")], ("sec",), previous, 10, 20,
        ))
        self.assertEqual(20, _select_terminal_window(
            [(10, "Unrelated"), (20, "Codex")], ("sec",), previous, 20, 10,
        ))

    def test_terminal_window_selection_handles_reused_window_with_locked_title(self):
        previous = {10: "Unrelated", 20: "Other"}
        self.assertEqual(20, _select_terminal_window(
            [(10, "Unrelated"), (20, "sec")], ("sec",), previous, None,
            allow_z_order_fallback=False,
        ))
        self.assertEqual(10, _select_terminal_window(
            [(10, "Codex")], ("sec",), {10: "Unrelated"}, None,
            allow_z_order_fallback=False,
        ))

    def test_terminal_window_selection_ignores_unrelated_spinner_title_changes(self):
        previous = {10: "⠏ subapi", 20: "⠇ 安卓"}
        self.assertIsNone(_select_terminal_window(
            [(10, "⠙ subapi"), (20, "⠸ 安卓")], ("sec",), previous, None,
            allow_z_order_fallback=False,
        ))

    def test_terminal_window_selection_delays_ambiguous_z_order_fallback(self):
        windows = [(10, "Unrelated"), (20, "Other")]
        previous = dict(windows)
        self.assertIsNone(_select_terminal_window(
            windows, ("sec",), previous, None, allow_z_order_fallback=False,
        ))
        self.assertEqual(10, _select_terminal_window(
            windows, ("sec",), previous, None, allow_z_order_fallback=True,
        ))

    @unittest.skipUnless(os.name == "nt", "Windows foreground APIs")
    def test_focus_requires_the_target_to_become_foreground(self):
        def api(foreground_after: int):
            user32 = SimpleNamespace(
                GetForegroundWindow=Mock(side_effect=[100, foreground_after]),
                GetWindowThreadProcessId=Mock(return_value=20),
                AttachThreadInput=Mock(return_value=True),
                IsIconic=Mock(return_value=False),
                ShowWindow=Mock(return_value=True),
                BringWindowToTop=Mock(return_value=True),
                SetWindowPos=Mock(return_value=True),
                AllowSetForegroundWindow=Mock(return_value=True),
                SetForegroundWindow=Mock(return_value=False),
            )
            kernel32 = SimpleNamespace(GetCurrentThreadId=Mock(return_value=30))
            return SimpleNamespace(user32=user32, kernel32=kernel32)

        with patch("pc.agent.ctypes.windll", api(100)):
            self.assertFalse(_focus_window(200))
        with patch("pc.agent.ctypes.windll", api(200)):
            self.assertTrue(_focus_window(200))


if __name__ == "__main__":
    unittest.main()
