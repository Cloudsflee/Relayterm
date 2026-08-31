from __future__ import annotations

import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from bridge.profile_catalog import ProfileStore
from pc.agent import Agent
from pc.drain_switch import (
    BRIDGE_GENERATION,
    BridgeProbe,
    DrainStateStore,
    drain_profile_ids,
    merge_session_snapshots,
    prepare_shadow_catalog,
    rollback_live_drain,
    verified_bridge_pid,
    verified_legacy_agent_pid,
)


class DrainSwitchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="relayterm-drain-")
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_state_roundtrip_and_corruption_recovery(self) -> None:
        path = self.root / "drain_state.json"
        store = DrainStateStore(path)
        value = store.save({
            "version": 1,
            "primary": {
                "host": "127.0.0.1", "port": 19001, "pid": 12,
                "generation": BRIDGE_GENERATION,
                "profilePath": str(self.root / "profiles.v3.json"),
            },
            "drains": [{
                "host": "127.0.0.1", "port": 18766, "pid": 34,
                "generation": "legacy", "profileIds": ["one", "one", "bad id"],
                "startedAt": "2026-08-31T00:00:00Z", "emptyPolls": 2,
            }],
        })
        self.assertEqual(["one"], value["drains"][0]["profileIds"])
        self.assertEqual(2, value["drains"][0]["emptyPolls"])
        self.assertEqual({"one"}, drain_profile_ids(store.load()))
        self.assertFalse(list(path.parent.glob("*.tmp")))

        path.write_text("{broken", encoding="utf-8")
        self.assertEqual(DrainStateStore.empty(), store.load())
        self.assertEqual(1, len(list(path.parent.glob("drain_state.json.corrupt-*"))))

    def test_shadow_catalog_migrates_without_touching_legacy_source(self) -> None:
        source = self.root / "profiles.json"
        shadow = self.root / "profiles.v3.json"
        source.write_text(json.dumps({"version": 1, "profiles": [{
            "id": "project", "name": "Project", "workingDirectory": str(self.root),
            "shell": "pwsh", "startupCommand": "codex resume --last --yolo",
            "order": 0, "enabled": True,
        }]}), encoding="utf-8")
        original = source.read_bytes()
        prepare_shadow_catalog(source, shadow)
        profiles = ProfileStore(shadow, str(self.root)).load()
        self.assertEqual("codex", profiles[0].launch_mode)
        self.assertEqual(("--yolo",), profiles[0].codex_args)
        self.assertEqual(original, source.read_bytes())
        self.assertEqual(1, json.loads(source.read_text(encoding="utf-8"))["version"])
        self.assertEqual(3, json.loads(shadow.read_text(encoding="utf-8"))["version"])

    def test_merge_routes_only_running_drain_sessions_and_primary_wins_duplicates(self) -> None:
        primary = [
            {"profileId": "same", "running": True, "pid": 1},
            {"profileId": "new", "running": False, "state": "exited"},
        ]
        old_url = "http://127.0.0.1:18766"
        drains = [(old_url, [
            {"profileId": "same", "running": True, "pid": 2},
            {"profileId": "old", "running": True, "pid": 3},
            {"profileId": "ended", "running": False, "pid": 4},
        ])]
        sessions, routes = merge_session_snapshots(primary, drains)
        self.assertEqual(1, sessions["same"]["pid"])
        self.assertTrue(sessions["same"]["bridgeConflict"])
        self.assertTrue(sessions["old"]["draining"])
        self.assertEqual(old_url, routes["old"])
        self.assertNotIn("ended", sessions)

    def test_agent_plans_side_by_side_start_and_persists_active_routes(self) -> None:
        standard = self.root / "profiles.json"
        standard.write_text(json.dumps({"version": 1, "profiles": [{
            "id": "old", "name": "Old", "workingDirectory": str(self.root),
            "shell": "pwsh", "startupCommand": "", "order": 0, "enabled": True,
        }]}), encoding="utf-8")
        probe = BridgeProbe(
            "127.0.0.1", 18766, "", 1,
            {"version": 1, "profiles": []},
            ({"profileId": "old", "running": True, "pid": 99},),
            1234,
        )
        agent = Agent.__new__(Agent)
        agent.settings = {"host": "127.0.0.1", "port": 18766}
        agent.host, agent.port = "127.0.0.1", 18766
        agent.token = "token"
        agent.logger = logging.getLogger("relayterm-test-drain")
        agent.port_notice = ""
        agent.standard_profile_path = standard
        agent.drain_store = DrainStateStore(self.root / "drain_state.json")
        agent.drain_state = agent.drain_store.load()
        agent.drain_bridges = []
        agent._initial_drain_snapshots = []

        with (
            patch("pc.agent.port_available", return_value=False),
            patch("pc.agent.probe_bridge", return_value=probe),
            patch("pc.agent.find_available_port", return_value=18767),
            patch("pc.agent.data_directory", return_value=self.root),
            patch("pc.agent.process_parent_id", return_value=5678),
        ):
            profile_path = agent._prepare_bridge_topology()

        self.assertEqual(self.root / "profiles.v3.json", profile_path)
        self.assertEqual(18767, agent.port)
        self.assertEqual(18766, agent.settings["port"])
        self.assertEqual(["old"], agent.drain_bridges[0]["profileIds"])
        self.assertEqual(5678, agent.drain_bridges[0]["agentPid"])
        self.assertEqual(
            "http://127.0.0.1:18766", agent._initial_drain_snapshots[0][0],
        )
        persisted = agent.drain_store.load()
        self.assertEqual(18767, persisted["primary"]["port"])
        self.assertEqual({"old"}, drain_profile_ids(persisted))
        self.assertEqual(1, json.loads(standard.read_text(encoding="utf-8"))["version"])

    def test_old_process_identity_requires_listener_pid_and_bridge_marker(self) -> None:
        with (
            patch("pc.drain_switch.listener_process_id", return_value=321),
            patch("pc.drain_switch.process_command_line",
                  return_value=r"C:\Python\pythonw.exe -m bridge.relay_bridge"),
        ):
            self.assertEqual(321, verified_bridge_pid(18766, 321))
            self.assertEqual(0, verified_bridge_pid(18766, 999))
        with (
            patch("pc.drain_switch.listener_process_id", return_value=321),
            patch("pc.drain_switch.process_command_line", return_value="unrelated.exe"),
        ):
            self.assertEqual(0, verified_bridge_pid(18766, 321))
        with patch(
            "pc.drain_switch.process_command_line",
            return_value=r'pythonw.exe "E:\workspace\pc\agent.py" --startup',
        ):
            self.assertEqual(654, verified_legacy_agent_pid(654))

    def test_live_rollback_check_requires_an_empty_new_bridge(self) -> None:
        state = {
            "version": 1,
            "primary": {
                "host": "127.0.0.1", "port": 18767, "pid": 22, "agentPid": 11,
                "profilePath": str(self.root / "profiles.v3.json"),
            },
            "drains": [{"host": "127.0.0.1", "port": 18766}],
        }
        state_store = Mock()
        state_store.load.return_value = state
        token_store = Mock()
        token_store.load_or_create.return_value = "token"
        empty_probe = BridgeProbe(
            "127.0.0.1", 18767, BRIDGE_GENERATION, 3, {}, (), 22,
        )
        with (
            patch("pc.drain_switch.DrainStateStore", return_value=state_store),
            patch("pc.drain_switch.TokenStore", return_value=token_store),
            patch("pc.drain_switch.probe_bridge", return_value=empty_probe),
        ):
            result = rollback_live_drain(check_only=True)
        self.assertEqual("ready", result["status"])
        self.assertEqual(18766, result["restorePort"])

        busy_probe = BridgeProbe(
            "127.0.0.1", 18767, BRIDGE_GENERATION, 3, {},
            ({"profileId": "busy", "running": True},), 22,
        )
        with (
            patch("pc.drain_switch.DrainStateStore", return_value=state_store),
            patch("pc.drain_switch.TokenStore", return_value=token_store),
            patch("pc.drain_switch.probe_bridge", return_value=busy_probe),
            self.assertRaisesRegex(RuntimeError, "drain_primary_has_running_sessions"),
        ):
            rollback_live_drain(check_only=True)


if __name__ == "__main__":
    unittest.main()
