from __future__ import annotations

import json
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from bridge.codex_sessions import (
    AppServerProtocolError,
    AppServerTransportError,
    CodexAppServerClient,
    CodexBindingStore,
    CodexSessionError,
    CodexSessionService,
    build_codex_resume_command,
    normalize_working_directory,
)
from bridge.profile_catalog import Profile, ProfileStore


THREAD_A = "019c5a2f-87f6-7db0-babc-2bb3923347a3"
THREAD_B = "019c5a30-24d7-7230-ae95-fb3e920a06da"
THREAD_C = "019c5a30-6758-7ca2-8820-86ef170fde30"


FAKE_SERVER = r"""
import json, pathlib, sys, time
mode, state_path = sys.argv[1], pathlib.Path(sys.argv[2])
try:
    count = int(state_path.read_text()) + 1
except Exception:
    count = 1
state_path.write_text(str(count))

def send(value):
    print(json.dumps(value, separators=(",", ":")), flush=True)

for raw in sys.stdin:
    message = json.loads(raw)
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialize":
        send({"id": request_id, "result": {"userAgent": "fake"}})
    elif method == "initialized":
        pass
    elif method == "thread/list":
        if mode == "restart" and count == 1:
            sys.exit(7)
        if mode == "timeout":
            time.sleep(5)
            continue
        if mode == "protocol":
            print("{broken", flush=True)
            continue
        cursor = message.get("params", {}).get("cursor")
        if not cursor:
            send({"id": request_id, "result": {"data": [{
                "id": "019c5a2f-87f6-7db0-babc-2bb3923347a3", "source": "cli",
                "cwd": "C:\\repo", "preview": "first", "updatedAt": 2,
                "recencyAt": 4, "status": {"type": "notLoaded"}, "ephemeral": False,
                "parentThreadId": None
            }], "nextCursor": "page-2"}})
        else:
            send({"id": request_id, "result": {"data": [{
                "id": "019c5a30-24d7-7230-ae95-fb3e920a06da", "source": "vscode",
                "cwd": "C:\\repo", "preview": "second", "updatedAt": 3,
                "recencyAt": 3, "status": {"type": "idle"}, "ephemeral": False,
                "parentThreadId": None
            }], "nextCursor": None}})
"""


class AppServerClientTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="relayterm-fake-app-server-")
        root = Path(self.temp.name)
        self.script = root / "fake_server.py"
        self.script.write_text(textwrap.dedent(FAKE_SERVER), encoding="utf-8")
        self.state = root / "starts.txt"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def client(self, mode: str, timeout: float = 1.0) -> CodexAppServerClient:
        return CodexAppServerClient(
            [sys.executable, str(self.script), mode, str(self.state)],
            timeout=timeout, cache_seconds=60,
        )

    def test_initialization_pagination_and_short_cache(self) -> None:
        client = self.client("pages")
        try:
            first = client.list_threads()
            second = client.list_threads()
            self.assertEqual([THREAD_A, THREAD_B], [item["id"] for item in first])
            self.assertEqual(first, second)
            self.assertEqual("1", self.state.read_text(encoding="utf-8"))
        finally:
            client.shutdown()

    def test_transport_exit_restarts_once(self) -> None:
        client = self.client("restart")
        try:
            self.assertEqual(2, len(client.list_threads()))
            self.assertEqual("2", self.state.read_text(encoding="utf-8"))
        finally:
            client.shutdown()

    def test_create_names_and_verifies_thread_before_releasing_it(self) -> None:
        client = self.client("pages")
        saved = thread(THREAD_C, self.temp.name, "appServer", 30)
        with patch.object(client, "request", side_effect=[
            {"thread": saved}, {}, {"thread": saved}, {},
        ]) as request:
            self.assertEqual(THREAD_C, client.start_thread(self.temp.name)["id"])
        self.assertEqual(
            ["thread/start", "thread/name/set", "thread/read", "thread/unsubscribe"],
            [call.args[0] for call in request.call_args_list],
        )
        self.assertFalse(request.call_args_list[0].args[1]["ephemeral"])
        self.assertEqual("legacy", request.call_args_list[0].args[1]["historyMode"])
        self.assertTrue(request.call_args_list[2].args[1]["includeTurns"])
        self.assertTrue(request.call_args_list[1].args[1]["name"])

    def test_failed_create_does_not_repeat_allocation_or_return_an_unsaved_uuid(self) -> None:
        client = self.client("pages")
        with (
            patch.object(client, "_ensure_started_locked"),
            patch.object(client, "_raw_request_locked", side_effect=AppServerTransportError("timeout")) as raw,
            self.assertRaises(AppServerTransportError),
        ):
            client.request("thread/start", {"cwd": self.temp.name})
        self.assertEqual(1, raw.call_count)
        with (
            patch.object(client, "request", side_effect=[
                {"thread": thread(THREAD_C, self.temp.name, "appServer", 30)},
                AppServerTransportError("name failed"),
            ]) as request,
            self.assertRaises(AppServerTransportError),
        ):
            client.start_thread(self.temp.name)
        self.assertEqual(2, request.call_count)

    def test_timeout_and_protocol_error_are_bounded(self) -> None:
        timeout_client = self.client("timeout", timeout=0.15)
        try:
            with self.assertRaises(AppServerTransportError):
                timeout_client.list_threads()
        finally:
            timeout_client.shutdown()
        self.state.unlink(missing_ok=True)
        protocol_client = self.client("protocol", timeout=0.5)
        try:
            with self.assertRaises(AppServerProtocolError):
                protocol_client.list_threads()
        finally:
            protocol_client.shutdown()
        self.assertEqual("2", self.state.read_text(encoding="utf-8"))


class FakeClient:
    def __init__(self, active=None, archived=None) -> None:
        self.active = list(active or [])
        self.archived = list(archived or [])
        self.started = []
        self.read_ids = []

    def read_thread(self, thread_id):
        self.read_ids.append(thread_id)
        return next((t for t in self.active if t["id"] == thread_id), {})

    def list_threads(self, archived=False, **_kwargs):
        return list(self.archived if archived else self.active)

    def start_thread(self, cwd):
        self.started.append(cwd)
        return thread(THREAD_C, cwd, "appServer", 30, title="new")


class FakeManager:
    def __init__(self, active=None) -> None:
        self.active = active

    def get_by_profile(self, profile_id):
        return self.active if self.active and self.active.profile_id == profile_id else None


def thread(
    thread_id: str, cwd: str, source: str, updated: int, *, title: str = "title",
    parent=None, ephemeral=False, status="notLoaded",
):
    return {
        "id": thread_id, "cwd": cwd, "source": source, "updatedAt": updated,
        "recencyAt": updated, "name": title, "preview": "secret full prompt",
        "parentThreadId": parent, "ephemeral": ephemeral, "status": {"type": status},
    }


class SessionServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="relayterm-codex-service-")
        self.root = Path(self.temp.name)
        self.project = self.root / "repo" / "project"
        self.sibling = self.root / "repo" / "sibling"
        self.other = self.root / "other"
        for value in (self.project, self.sibling, self.other):
            value.mkdir(parents=True, exist_ok=True)
        self.store = ProfileStore(self.root / "profiles.json", str(self.project))
        self.profile = Profile(
            "project", "Project", str(self.project), "pwsh", "", False, True,
            "codex", ("--yolo",),
        )
        self.store.save([self.profile])
        self.bindings = CodexBindingStore(self.root / "codex_bindings.json")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def service(self, client, manager=None):
        repo = normalize_working_directory(self.root / "repo")

        def roots(path):
            normalized = normalize_working_directory(path)
            if normalized.startswith(normalize_working_directory(self.root / "repo")):
                return repo
            return normalize_working_directory(self.other)

        return CodexSessionService(
            client, self.bindings, self.store, manager or FakeManager(), git_root_resolver=roots,
        )

    def test_filters_sources_subagents_temporary_threads_and_sorts_recency(self) -> None:
        values = [
            thread(THREAD_A, str(self.project), "cli", 10, title="older"),
            thread(THREAD_B, str(self.project) + os.sep, "vscode", 20, title="newer"),
            thread(THREAD_C, str(self.sibling), "appServer", 30, title="repo"),
            thread("019c5a31-0000-7000-8000-000000000001", str(self.project), "exec", 99),
            thread("019c5a31-0000-7000-8000-000000000002", str(self.project), "cli", 98,
                   parent=THREAD_A),
            thread("019c5a31-0000-7000-8000-000000000003", str(self.project), "cli", 97,
                   ephemeral=True),
        ]
        exact, repository = self.service(FakeClient(values)).discover(self.profile)
        self.assertEqual([THREAD_B, THREAD_A], [item.id for item in exact])
        self.assertEqual([THREAD_C], [item.id for item in repository])
        encoded = repository[0].to_dict()
        self.assertEqual(
            {"id", "title", "source", "cwd", "updatedAt", "matchType", "status"},
            set(encoded),
        )
        self.assertNotIn("secret full prompt", json.dumps(encoded))

    def test_auto_selection_repo_confirmation_create_and_locked_validation(self) -> None:
        repo_only = FakeClient([thread(THREAD_C, str(self.sibling), "appServer", 30)])
        service = self.service(repo_only)
        with self.assertRaisesRegex(CodexSessionError, "明确选择") as raised:
            service.resolve(self.profile)
        self.assertEqual("codex_selection_required", raised.exception.code)

        empty = FakeClient()
        self.assertEqual(THREAD_C, self.service(empty).resolve(self.profile))
        self.assertEqual([str(self.project)], empty.started)
        self.assertEqual("auto", self.bindings.get("project")["mode"])

        self.bindings.set_locked("project", THREAD_A)
        archived = FakeClient([], [thread(THREAD_A, str(self.project), "cli", 10)])
        with self.assertRaises(CodexSessionError) as invalid:
            self.service(archived).resolve(self.profile)
        self.assertEqual("codex_binding_invalid", invalid.exception.code)
        self.assertEqual("archived", invalid.exception.details["bindingStatus"])

    def test_current_relay_pty_wins_and_different_uuid_conflicts(self) -> None:
        active = SimpleNamespace(
            profile_id="project", ended=False, codex_thread_id=THREAD_A,
            cwd=str(self.project), launch_spec=object(),
        )
        service = self.service(FakeClient(), FakeManager(active))
        self.assertEqual(THREAD_A, service.resolve(self.profile))
        with self.assertRaises(CodexSessionError) as conflict:
            service.resolve(self.profile, THREAD_B)
        self.assertEqual("codex_thread_conflict", conflict.exception.code)

    def test_resume_checks_fresh_storage_and_preserves_invalid_lock(self) -> None:
        client = FakeClient([thread(THREAD_A, str(self.project), "cli", 30)])
        service = self.service(client)
        self.bindings.set_locked(self.profile.id, THREAD_A)
        client.read_thread = Mock(side_effect=CodexSessionError("codex_thread_unavailable"))
        with patch.object(client, "list_threads", wraps=client.list_threads) as listing:
            with self.assertRaises(CodexSessionError):
                service.resolve(self.profile)
        listing.assert_any_call(False, force=True)
        self.assertEqual(THREAD_A, self.bindings.get(self.profile.id)["threadId"])
        self.assertEqual([], client.started)
        with self.assertRaises(CodexSessionError):
            service.set_binding(self.profile.id, {"mode": "locked", "threadId": THREAD_A})

    def test_binding_store_roundtrip_and_shell_quoting(self) -> None:
        saved = self.bindings.set_locked("project", THREAD_A)
        self.assertEqual(THREAD_A, saved["threadId"])
        payload = json.loads(self.bindings.path.read_text(encoding="utf-8"))
        self.assertEqual(1, payload["version"])
        self.assertEqual("locked", payload["bindings"][0]["mode"])

        power, marker = build_codex_resume_command("pwsh", THREAD_A, ["--yolo", "x y"])
        cmd, cmd_marker = build_codex_resume_command("cmd", THREAD_A, ["--yolo", "x y"])
        self.assertIn("'" + THREAD_A + "'", power)
        self.assertIn("'x y'", power)
        self.assertIn('"' + THREAD_A + '"', cmd)
        self.assertNotIn("--last", power + cmd)
        self.assertIn(marker, power)
        self.assertIn(cmd_marker, cmd)


if __name__ == "__main__":
    unittest.main()
