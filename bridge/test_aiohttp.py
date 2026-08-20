from __future__ import annotations

import asyncio
import unittest

from aiohttp import WSMsgType
from aiohttp.test_utils import TestClient, TestServer

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


if __name__ == "__main__":
    unittest.main()
