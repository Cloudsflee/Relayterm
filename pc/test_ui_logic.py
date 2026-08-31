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
    Agent,
    _console_python_executable,
    _focus_window,
    _select_terminal_window,
    _terminal_title_matches,
    button_availability,
    codex_binding_text,
    format_activity,
    profile_display_state,
    profile_menu_availability,
    sort_profiles_recent,
)


class UiLogicTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="relayterm-ui-")
        self.profile = Profile("p", "Project", self.temp.name, "pwsh", "", False, True)
        self.now = dt.datetime(2026, 8, 11, 14, 32, tzinfo=dt.timezone.utc)

    def tearDown(self):
        self.temp.cleanup()

    def test_activity_formats_relative_and_calendar_times(self):
        self.assertEqual("刚刚", format_activity("2026-08-11T14:31:45+00:00", self.now))
        self.assertEqual("5 分钟前", format_activity("2026-08-11T14:27:00+00:00", self.now))
        self.assertEqual("今天 12:32", format_activity("2026-08-11T12:32:00+00:00", self.now))
        self.assertEqual("08-10 19:20", format_activity("2026-08-10T19:20:00+00:00", self.now))
        self.assertEqual("-", format_activity("broken", self.now))

    def test_codex_binding_summary_and_cancelled_switch_preserve_running_pty(self):
        thread_a = "019c5a2f-87f6-7db0-babc-2bb3923347a3"
        thread_b = "019c5a30-24d7-7230-ae95-fb3e920a06da"
        self.assertEqual("自动选择 · 019c5a2f", codex_binding_text({
            "mode": "auto", "selectedThreadId": thread_a,
            "binding": {"mode": "auto"},
        }))
        self.assertEqual("锁定已归档 · 019c5a2f", codex_binding_text({
            "binding": {"mode": "locked", "threadId": thread_a, "status": "archived"},
        }))

        profile = Profile(
            "codex", "Codex", self.temp.name, "pwsh", "", False, True, "codex", (),
        )
        agent = Agent.__new__(Agent)
        agent.sessions = {profile.id: {"running": True, "codexThreadId": thread_a}}
        agent.panel = None
        agent.api = Mock()
        agent.codex_details = {}
        agent._selection_changed = Mock()
        agent._launch_profile = Mock()
        with patch("pc.agent.messagebox.askyesno", return_value=False):
            self.assertFalse(agent._apply_codex_binding(profile, "locked", thread_b))
        agent.api.assert_not_called()
        agent._launch_profile.assert_not_called()
        self.assertEqual(thread_a, agent.sessions[profile.id]["codexThreadId"])

    def test_confirmed_codex_switch_updates_binding_then_removes_and_reconnects(self):
        thread_a = "019c5a2f-87f6-7db0-babc-2bb3923347a3"
        thread_b = "019c5a30-24d7-7230-ae95-fb3e920a06da"
        profile = Profile(
            "codex", "Codex", self.temp.name, "pwsh", "", False, True, "codex", (),
        )
        agent = Agent.__new__(Agent)
        agent.sessions = {profile.id: {"running": True, "codexThreadId": thread_a}}
        agent.panel = None
        agent.api = Mock(side_effect=[
            (200, {"binding": {"mode": "locked", "threadId": thread_b}}),
            (200, {"ok": True}),
        ])
        agent.codex_details = {}
        agent._selection_changed = Mock()
        agent._launch_profile = Mock()
        with patch("pc.agent.messagebox.askyesno", return_value=True):
            self.assertTrue(agent._apply_codex_binding(profile, "locked", thread_b))
        self.assertEqual(2, agent.api.call_count)
        self.assertEqual("PUT", agent.api.call_args_list[0].args[1])
        self.assertEqual("POST", agent.api.call_args_list[1].args[1])
        self.assertEqual({"force": True, "remove": True}, agent.api.call_args_list[1].args[2])
        agent._launch_profile.assert_called_once_with(
            profile, fresh=True, codex_thread_id=thread_b,
        )

    def test_fixed_state_vocabulary(self):
        self.assertEqual(("未运行", "-"), profile_display_state(self.profile, None))
        recent = dt.datetime.now(dt.timezone.utc).isoformat()
        self.assertEqual(("运行中", "刚刚"), profile_display_state(self.profile, {"running": True, "lastOpenedAt": recent}))
        self.assertEqual(("旧版排空中", "刚刚"), profile_display_state(
            self.profile, {"running": True, "draining": True, "lastOpenedAt": recent},
        ))
        self.assertEqual(("桌面已连接", "刚刚"), profile_display_state(self.profile, {"running": True, "desktopConnected": True, "lastOpenedAt": recent}))
        self.assertEqual(("终端已关闭", "刚刚"), profile_display_state(
            self.profile, {"running": True, "desktopState": "closed", "lastOpenedAt": recent},
        ))
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

    def test_recent_sort_places_pins_first_and_keeps_catalog_ties(self):
        profiles = [
            Profile("pin-late", "Pin late", self.temp.name, pinned=True),
            Profile("pin-recent", "Pin recent", self.temp.name, pinned=True),
            Profile("late-order", "Z", self.temp.name),
            Profile("first-order", "A", self.temp.name),
            Profile("missing", "Missing", self.temp.name),
        ]
        activity = {
            "pin-late": "2026-07-01T00:00:00Z",
            "pin-recent": "2026-09-01T00:00:00Z",
            "late-order": "2026-08-01T00:00:00Z",
            "first-order": "2026-08-01T00:00:00Z",
        }
        self.assertEqual(
            ["pin-late", "pin-recent", "late-order", "first-order", "missing"],
            [item.id for item in sort_profiles_recent(profiles, activity)],
        )

    def test_context_menu_states_only_disable_first_pin_and_invalid_actions(self):
        profiles = [
            Profile("pin-a", "Pin A", self.temp.name, pinned=True),
            Profile("pin-b", "Pin B", self.temp.name, pinned=True),
            Profile("plain", "Plain", self.temp.name),
        ]
        self.assertEqual(
            {"move_up": False, "pin": True, "unpin": True},
            profile_menu_availability(profiles, "pin-a"),
        )
        self.assertEqual(
            {"move_up": True, "pin": True, "unpin": True},
            profile_menu_availability(profiles, "pin-b"),
        )
        self.assertEqual(
            {"move_up": True, "pin": True, "unpin": False},
            profile_menu_availability(profiles, "plain"),
        )

    def test_right_click_selects_hit_row_and_blank_space_does_not_open_menu(self):
        class Tree:
            def __init__(self):
                self.hit = "pin-a"
                self.selected = ("plain",)
                self.focused = ""

            def identify_row(self, _y):
                return self.hit

            def selection_set(self, profile_id):
                self.selected = (profile_id,)

            def selection(self):
                return self.selected

            def focus(self, profile_id):
                self.focused = profile_id

        tree = Tree()
        menu = Mock()
        agent = Agent.__new__(Agent)
        agent.tree = tree
        agent.profile_menu = menu
        agent.profiles = agent.catalog_profiles = [
            Profile("pin-a", "Pin A", self.temp.name, pinned=True),
            Profile("plain", "Plain", self.temp.name),
        ]
        agent._selection_changed = Mock()
        event = SimpleNamespace(y=10, x_root=20, y_root=30)

        self.assertEqual("break", agent._show_profile_menu(event))
        self.assertEqual(("pin-a",), tree.selected)
        self.assertEqual("pin-a", tree.focused)
        menu.entryconfigure.assert_any_call(0, state="disabled")
        menu.entryconfigure.assert_any_call(2, state="normal")
        menu.tk_popup.assert_called_once_with(20, 30)

        tree.hit = ""
        menu.reset_mock()
        self.assertEqual("break", agent._show_profile_menu(event))
        menu.tk_popup.assert_not_called()

    def test_menu_actions_persist_clear_search_keep_selection_and_do_not_connect(self):
        class Store:
            def __init__(self, values):
                self.values = list(values)

            def load(self):
                return list(self.values)

            def save(self, values):
                self.values = list(values)
                return list(self.values)

        class Tree:
            def __init__(self, selected):
                self.selected = (selected,)
                self.focused = selected

            def selection(self):
                return self.selected

            def selection_set(self, profile_id):
                self.selected = (profile_id,)

            def focus(self, profile_id):
                self.focused = profile_id

            def exists(self, profile_id):
                return any(item.id == profile_id for item in store.values)

        class Variable:
            def __init__(self, value):
                self.value = value

            def get(self):
                return self.value

            def set(self, value):
                self.value = value

        values = [
            Profile("pin-a", "Pin A", self.temp.name, pinned=True),
            Profile("pin-b", "Pin B", self.temp.name, pinned=True),
            Profile("plain-a", "Plain A", self.temp.name),
            Profile("plain-b", "Plain B", self.temp.name),
        ]
        store = Store(values)
        agent = Agent.__new__(Agent)
        agent.store = store
        agent.catalog_profiles = list(values)
        agent.profiles = list(values)
        agent.recent_activity = {"plain-a": "2026-08-02T00:00:00Z"}
        agent.tree = Tree("plain-b")
        agent.search_var = Variable("Plain B")
        agent.search_entry = Mock()
        agent.search_placeholder_active = False
        agent.refresh_tree = Mock()
        agent._selection_changed = Mock()
        agent.start_selected = Mock()

        agent._reorder_selected("move_up")
        self.assertEqual(["pin-a", "pin-b", "plain-b", "plain-a"], [item.id for item in store.values])
        self.assertTrue(store.values[2].pinned)
        self.assertEqual("", agent.search_var.get())
        self.assertEqual(("plain-b",), agent.tree.selection())

        agent.search_var.set("again")
        agent._reorder_selected("move_up")
        self.assertEqual(["pin-a", "plain-b", "pin-b", "plain-a"], [item.id for item in store.values])

        agent.tree.selection_set("plain-a")
        agent._reorder_selected("pin")
        self.assertEqual(["plain-a", "pin-a", "plain-b", "pin-b"], [item.id for item in store.values])

        agent._reorder_selected("unpin")
        self.assertEqual(["pin-a", "plain-b", "pin-b", "plain-a"], [item.id for item in store.values])
        self.assertFalse(store.values[-1].pinned)
        self.assertEqual(("plain-a",), agent.tree.selection())
        self.assertEqual(4, agent.refresh_tree.call_count)
        agent.start_selected.assert_not_called()

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
