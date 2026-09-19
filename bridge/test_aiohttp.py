from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import WSMsgType
from aiohttp.test_utils import TestClient, TestServer

from bridge import relay_bridge
from bridge.codex_sessions import CodexSessionError
from bridge.profile_catalog import Profile, ProfileStore
from bridge.relay_bridge import create_app
from bridge.session_manager import SessionManager
from bridge.test_pty import FakeBackend


class AiohttpPtyProtocolTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.manager = SessionManager(2, 60, backend_factory=FakeBackend, output_limit=4096)
        self.client = TestClient(TestServer(create_app(self.manager, token="AIO_TOKEN")))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        self.manager.shutdown()

    async def test_failed_codex_keeps_raw_error_and_drops_late_input(self) -> None:
        ws = await self.client.ws_connect("/v1/pty", headers={"Authorization": "Bearer AIO_TOKEN"})
        await ws.send_json({"type": "open", "sessionId": "failed-codex"})
        await ws.receive(timeout=3)
        await ws.receive(timeout=3)
        session = self.manager.get("failed-codex")
        session.codex_thread_id = "019c5a2f-87f6-7db0-babc-2bb3923347a3"
        original = b"ERROR: No saved session found with ID fixture\r\n"
        session.on_output(original)
        session.on_exit(7)
        self.assertEqual(original, (await ws.receive(timeout=3)).data)
        error = (await ws.receive(timeout=3)).json()
        self.assertEqual("codex_launch_failed", error["code"])
        self.assertTrue(error["fatal"])
        self.assertEqual(7, (await ws.receive(timeout=3)).json()["code"])
        for _ in range(10):
            await ws.send_bytes(b"late terminal reply")
        await ws.send_json({"type": "ping"})
        self.assertEqual("pong", (await ws.receive(timeout=3)).json()["type"])
        await ws.close()

    async def test_ready_precedes_synchronous_startup_output(self) -> None:
        websocket = await self.client.ws_connect(
            "/v1/pty?sessionId=aio-order",
            headers={"Authorization": "Bearer AIO_TOKEN"},
        )
        await websocket.send_json({
            "type": "open",
            "sessionId": "aio-order",
            "startupCommand": "fake",
            "clientId": "desktop-aio",
            "clientType": "desktop",
        })
        ready = await websocket.receive(timeout=3)
        output = await websocket.receive(timeout=3)
        self.assertEqual(WSMsgType.TEXT, ready.type)
        self.assertEqual("ready", ready.json()["type"])
        self.assertEqual(WSMsgType.BINARY, output.type)
        self.assertIn(b"boot", output.data)
        await websocket.close()

    async def test_failed_start_sends_error_and_removes_session(self) -> None:
        def fail_backend(*_args):
            raise RuntimeError("aio_start_failed")

        await self.client.close()
        self.manager.shutdown()
        self.manager = SessionManager(2, 60, backend_factory=fail_backend)
        self.client = TestClient(TestServer(create_app(self.manager, token="AIO_TOKEN")))
        await self.client.start_server()
        websocket = await self.client.ws_connect(
            "/v1/pty?sessionId=aio-failed",
            headers={"Authorization": "Bearer AIO_TOKEN"},
        )
        await websocket.send_json({"type": "open", "sessionId": "aio-failed"})
        error = await websocket.receive(timeout=3)
        self.assertEqual(WSMsgType.TEXT, error.type)
        self.assertEqual("aio_start_failed", error.json()["code"])
        for _ in range(20):
            if not self.manager.list_status():
                break
            await asyncio.sleep(0.01)
        self.assertEqual([], self.manager.list_status())
        await websocket.close()

    async def test_profile_ready_contains_persisted_last_opened_at(self) -> None:
        previous_store = relay_bridge.PROFILE_STORE
        with tempfile.TemporaryDirectory(prefix="relayterm-aio-recent-") as directory:
            relay_bridge.PROFILE_STORE = ProfileStore(Path(directory) / "profiles.json", directory)
            relay_bridge.PROFILE_STORE.save([Profile("aio-profile", "Aio", directory)])
            try:
                websocket = await self.client.ws_connect(
                    "/v1/pty?sessionId=aio-profile",
                    headers={"Authorization": "Bearer AIO_TOKEN"},
                )
                await websocket.send_json({
                    "type": "open", "profileId": "aio-profile",
                    "clientId": "desktop-aio-profile", "clientType": "desktop",
                })
                ready = await websocket.receive(timeout=3)
                output = await websocket.receive(timeout=3)
                self.assertEqual(WSMsgType.TEXT, ready.type)
                self.assertEqual(WSMsgType.BINARY, output.type)
                opened = ready.json()["lastOpenedAt"]
                self.assertTrue(opened.endswith("Z"))
                self.assertEqual(opened, relay_bridge.PROFILE_STORE.recent_activity.get("aio-profile"))
                await websocket.send_json({
                    "type": "close", "reason": "terminal_closed", "terminate": False,
                })
                for _ in range(30):
                    status = self.manager.get("aio-profile").status()
                    if status["desktopState"] == "closed":
                        break
                    await asyncio.sleep(0.01)
                self.assertEqual("closed", status["desktopState"])
                self.assertFalse(self.manager.get("aio-profile").backend.dead)
                await websocket.close()
            finally:
                relay_bridge.PROFILE_STORE = previous_store

    async def test_codex_http_routes_and_websocket_uuid_conflict(self) -> None:
        thread_id = "019c5a2f-87f6-7db0-babc-2bb3923347a3"
        other_id = "019c5a30-24d7-7230-ae95-fb3e920a06da"
        previous_store = relay_bridge.PROFILE_STORE
        with tempfile.TemporaryDirectory(prefix="relayterm-aio-codex-") as directory:
            relay_bridge.PROFILE_STORE = ProfileStore(Path(directory) / "profiles.json", directory)
            relay_bridge.PROFILE_STORE.save([Profile(
                "codex-profile", "Codex", directory, "pwsh", "", False, True,
                "codex", ("--yolo",),
            )])

            class Service:
                def sessions(_self, profile_id):
                    return {
                        "profileId": profile_id, "mode": "auto",
                        "binding": {"mode": "auto", "status": "auto"},
                        "currentRelayThread": None, "exactCandidates": [],
                        "repositoryCandidates": [], "requiresSelection": False,
                    }

                def set_binding(_self, profile_id, value):
                    return {"profileId": profile_id, "binding": value}

                def create(_self, profile_id, lock=True):
                    return {"profileId": profile_id, "thread": {"id": thread_id}, "locked": lock}

                def resolve(_self, profile, requested=None):
                    active = self.manager.get_by_profile(profile.id)
                    requested = str(requested or "")
                    if active is not None and not active.ended:
                        if requested and requested != active.codex_thread_id:
                            raise CodexSessionError(
                                "codex_thread_conflict", status=409,
                                details={"currentThreadId": active.codex_thread_id,
                                         "requestedThreadId": requested},
                            )
                        return active.codex_thread_id
                    return requested or thread_id

            try:
                with patch.object(relay_bridge, "codex_session_service", return_value=Service()):
                    headers = {"Authorization": "Bearer AIO_TOKEN"}
                    response = await self.client.get(
                        "/v1/profiles/codex-profile/codex-sessions", headers=headers,
                    )
                    self.assertEqual(200, response.status)
                    self.assertEqual("auto", (await response.json())["mode"])
                    response = await self.client.put(
                        "/v1/profiles/codex-profile/codex-binding", headers=headers,
                        json={"mode": "locked", "threadId": thread_id},
                    )
                    self.assertEqual(200, response.status)
                    response = await self.client.post(
                        "/v1/profiles/codex-profile/codex-sessions", headers=headers,
                        json={"lock": True},
                    )
                    self.assertEqual(201, response.status)

                    first = await self.client.ws_connect("/v1/pty", headers=headers)
                    await first.send_json({
                        "type": "open", "profileId": "codex-profile",
                        "clientId": "android-a", "clientType": "android",
                    })
                    ready = await first.receive(timeout=3)
                    self.assertEqual(thread_id, ready.json()["codexThreadId"])
                    await first.receive(timeout=3)  # boot output

                    second = await self.client.ws_connect("/v1/pty", headers=headers)
                    await second.send_json({
                        "type": "open", "profileId": "codex-profile",
                        "codexThreadId": other_id,
                        "clientId": "desktop-b", "clientType": "desktop",
                    })
                    conflict = await second.receive(timeout=3)
                    self.assertEqual("codex_thread_conflict", conflict.json()["code"])
                    self.assertEqual(thread_id, self.manager.get_by_profile("codex-profile").codex_thread_id)
                    await second.close()
                    await first.close()
            finally:
                relay_bridge.PROFILE_STORE = previous_store

    async def test_draining_profile_is_blocked_until_old_session_disappears(self) -> None:
        previous_store = relay_bridge.PROFILE_STORE
        previous_drain_path = relay_bridge.DRAIN_STATE_PATH
        with tempfile.TemporaryDirectory(prefix="relayterm-aio-drain-") as directory:
            root = Path(directory)
            state_path = root / "drain_state.json"
            state_path.write_text(json.dumps({
                "version": 1,
                "primary": None,
                "drains": [{"profileIds": ["draining-profile"]}],
            }), encoding="utf-8")
            relay_bridge.DRAIN_STATE_PATH = str(state_path)
            relay_bridge.PROFILE_STORE = ProfileStore(root / "profiles.json", directory)
            relay_bridge.PROFILE_STORE.save([
                Profile("draining-profile", "Draining", directory),
            ])
            headers = {"Authorization": "Bearer AIO_TOKEN"}
            try:
                blocked = await self.client.ws_connect("/v1/pty", headers=headers)
                await blocked.send_json({
                    "type": "open", "profileId": "draining-profile",
                    "clientId": "desktop-drain", "clientType": "desktop",
                })
                error = await blocked.receive(timeout=3)
                self.assertEqual("profile_draining", error.json()["code"])
                self.assertIsNone(self.manager.get_by_profile("draining-profile"))
                await blocked.close()

                state_path.write_text(json.dumps({
                    "version": 1, "primary": None, "drains": [],
                }), encoding="utf-8")
                allowed = await self.client.ws_connect("/v1/pty", headers=headers)
                await allowed.send_json({
                    "type": "open", "profileId": "draining-profile",
                    "clientId": "desktop-new", "clientType": "desktop",
                })
                ready = await allowed.receive(timeout=3)
                self.assertEqual("ready", ready.json()["type"])
                await allowed.receive(timeout=3)
                await allowed.close()
            finally:
                relay_bridge.PROFILE_STORE = previous_store
                relay_bridge.DRAIN_STATE_PATH = previous_drain_path


if __name__ == "__main__":
    unittest.main()
