from __future__ import annotations

import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

from bridge.profile_catalog import Profile
from pc.agent import Agent
from pc.ui_theme import apply_theme, icon_font


class PanelSmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
        except tk.TclError as exc:
            raise unittest.SkipTest(f"Tk display unavailable: {exc}")
        cls.root.withdraw()
        apply_theme(cls.root)
        cls.temp = tempfile.TemporaryDirectory(prefix="relayterm-smoke-")
        base = Path(cls.temp.name)
        (base / "long" / "working" / "directory").mkdir(parents=True)
        cls.profiles = [
            Profile("one", "Alpha", str(base), "pwsh", "", 0, True),
            Profile("two", "Disabled", str(base), "cmd", "echo disabled", 1, False),
            Profile("three", "Long command", str(base / "long" / "working" / "directory"), "wsl", "echo " + "x" * 400, 2, True),
        ]
        agent = Agent.__new__(Agent)
        agent.root = cls.root
        agent.panel = None
        agent.search_var = tk.StringVar()
        agent.search_entry = None
        agent.search_placeholder_active = False
        agent.status_var = tk.StringVar(value="Bridge 就绪")
        agent.endpoint_var = tk.StringVar(value="127.0.0.1:18765")
        agent.tunnel_status_var = tk.StringVar(value="隧道未启动")
        agent.remote_var = tk.StringVar(value="远程访问")
        agent.tree = None
        agent.detail_frame = None
        agent.detail_profile_id = ""
        agent.edit_button = agent.delete_button = agent.stop_button = agent.open_button = None
        agent.icon_font = icon_font(cls.root)
        agent.profiles = list(cls.profiles)
        agent.sessions = {
            "one": {"running": True, "desktopConnected": True, "pid": 1234, "cwd": str(base), "attachmentCount": 1, "controller": {"clientType": "desktop"}},
            "three": {"running": False, "lastActivityAt": "2026-08-10T19:20:00+00:00", "cwd": str(base), "attachmentCount": 0},
        }
        agent.bridge_state = "ready"
        agent.store = type("Store", (), {"load": lambda _self: list(cls.profiles)})()
        agent.build_panel()
        cls.agent = agent
        cls.root.update_idletasks()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.agent.panel.destroy()
            cls.root.destroy()
        finally:
            cls.temp.cleanup()

    def setUp(self):
        self.agent.panel.deiconify()
        self.agent.search_placeholder_active = False
        self.agent.search_var.set("")
        self.agent.refresh_tree()

    def test_show_panel_focuses_search_and_escape_hides(self):
        self.agent.panel.withdraw()
        self.agent.show_panel()
        self.root.update()
        self.assertIs(self.agent.search_entry, self.root.focus_get())
        self.agent.panel.event_generate("<Escape>")
        self.root.update()
        self.assertEqual("withdrawn", self.agent.panel.state())

    def test_enter_runs_current_open_command(self):
        calls = []
        original = self.agent.start_selected
        try:
            self.agent.start_selected = lambda: calls.append(self.agent.selected_profile_id())
            self.agent.tree.selection_set("one")
            self.root.update()
            self.agent.panel.event_generate("<Return>")
            self.root.update()
            self.assertEqual(["one"], calls)
        finally:
            self.agent.start_selected = original

    def test_tree_enter_runs_current_open_command(self):
        calls = []
        original = self.agent.start_selected
        try:
            self.agent.start_selected = lambda: calls.append(self.agent.selected_profile_id())
            self.agent.tree.focus_set()
            self.agent.tree.selection_set("one")
            self.agent.tree.event_generate("<Return>")
            self.root.update()
            self.assertEqual(["one"], calls)
        finally:
            self.agent.start_selected = original

    def test_open_uses_a_new_terminal_window(self):
        self.agent.tree.selection_set("one")
        self.root.update()
        with (
            patch("pc.agent._console_python_executable", return_value=r"C:\Python\python.exe"),
            patch("pc.agent.subprocess.Popen") as popen,
            patch.object(self.agent.root, "after") as after,
        ):
            self.agent.start_selected()
        command = popen.call_args.args[0]
        self.assertEqual(["-w", "0"], command[1:3])
        self.assertIn("--suppressApplicationTitle", command)
        self.assertEqual(r"C:\Python\python.exe", command[command.index("--startingDirectory") + 2])
        after.assert_called_once()

    def test_initial_panel_has_no_selection_or_detail(self):
        self.agent.tree.selection_remove(*self.agent.tree.selection())
        self.agent._selection_changed()
        self.agent._set_search_placeholder()
        self.assertEqual((), self.agent.tree.selection())
        self.assertEqual("", self.agent.detail_profile_id)
        self.assertEqual("搜索名称或目录", self.agent.search_var.get())
        for button in (self.agent.edit_button, self.agent.delete_button, self.agent.stop_button, self.agent.open_button):
            self.assertEqual("disabled", str(button["state"]))
        self.assertEqual((840, 540), (self.agent.panel.winfo_width(), self.agent.panel.winfo_height()))

    def test_selection_populates_detail_and_button_states(self):
        self.agent.tree.selection_set("one")
        self.root.update()
        self.assertEqual("one", self.agent.detail_profile_id)
        self.assertEqual(str(self.profiles[0].working_directory), self.agent.detail_vars["directory"].get())
        self.assertEqual("桌面已连接", self.agent.detail_vars["state"].get())
        self.assertEqual("normal", str(self.agent.open_button["state"]))
        self.assertEqual("normal", str(self.agent.stop_button["state"]))

    def test_disabled_and_exited_rows_gate_commands(self):
        self.agent.tree.selection_set("two")
        self.root.update()
        self.assertEqual("disabled", str(self.agent.open_button["state"]))
        self.assertEqual("disabled", str(self.agent.stop_button["state"]))
        self.assertEqual("normal", str(self.agent.edit_button["state"]))
        self.agent.tree.selection_set("three")
        self.root.update()
        self.assertEqual("normal", str(self.agent.open_button["state"]))
        self.assertEqual("disabled", str(self.agent.stop_button["state"]))

    def test_search_filters_and_collapses_missing_selection(self):
        self.agent._clear_search_placeholder()
        self.agent.search_var.set("Disabled")
        self.agent.refresh_tree()
        self.assertEqual(("two",), self.agent.tree.get_children())
        self.assertEqual((), self.agent.tree.selection())
        self.assertEqual("", self.agent.detail_profile_id)

    def test_long_command_is_kept_in_detail_label(self):
        self.agent.tree.selection_set("three")
        self.root.update()
        self.assertEqual(400 + 5, len(self.agent.detail_vars["command"].get()))
        self.assertGreaterEqual(self.agent.detail_command.winfo_width(), 500)


if __name__ == "__main__":
    unittest.main()
