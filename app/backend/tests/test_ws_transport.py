"""Browser websocket transport regressions (ported from Sonic 7be01f4).

aiohttp 3.14.2/3.14.3 (aio-libs/aiohttp#13274) reject the first
permessage-deflate data frame that follows an inbound PONG with close 1002
"Received frame with non-zero reserved bits". Chromium always offers deflate, so
a browser socket that answered one ping (from aiohttp, a proxy or an ingress)
dies on its next message. These tests drive the real middle tiers with that
client framing.
"""

import asyncio
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.append(str(Path(__file__).resolve().parents[1]))
sys.path.append(str(Path(__file__).resolve().parent))

import aiohttp
import yaml
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer
from azure.core.credentials import AzureKeyCredential
from test_session_bootstrap import BROWSER_SESSION_UPDATE, FakeGARealtime

import rtmt as rtmt_module
from rtmt import RTMiddleTier
from rtmt_local import RTLocalPipeline

BACKEND = Path(__file__).resolve().parents[1]


class BrowserSocketTransportTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.fake = FakeGARealtime()
        self.fake_server = TestServer(self.fake.app())
        await self.fake_server.start_server()
        self.rtmt = RTMiddleTier(
            endpoint=str(self.fake_server.make_url("")),
            deployment="gpt-realtime-test",
            credentials=AzureKeyCredential("test-key"),
            voice_choice="marin",
        )
        self.rtmt.system_message = "sys"
        app = web.Application()
        self.rtmt.attach_to_app(app, "/realtime")
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.fake_server.close()

    async def _until(self, predicate, timeout=5.0):
        async def poll():
            while not predicate():
                await asyncio.sleep(0.01)
        await asyncio.wait_for(poll(), timeout)

    def _session_updates(self):
        # [0] is the server bootstrap; anything after it came from the browser.
        return [e for e in self.fake.received if e["type"] == "session.update"]

    async def test_handshake_does_not_negotiate_permessage_deflate(self):
        """Chromium always offers permessage-deflate; the server must decline it."""
        browser = await self.client.ws_connect("/realtime", compress=15)
        self.assertEqual(browser.compress, 0, "server accepted permessage-deflate")
        self.assertNotIn("Sec-WebSocket-Extensions", browser._response.headers)
        await browser.close()

    async def test_first_data_frame_after_a_pong_is_accepted(self):
        """PONG first, then a data frame: aiohttp 3.14.3 closes 1002 if deflate is on."""
        browser = await self.client.ws_connect("/realtime", compress=15, autoping=False)
        await browser.pong(b"")
        await browser.send_str(json.dumps(BROWSER_SESSION_UPDATE))

        async def read_until_close():
            while True:
                msg = await browser.receive()
                if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
                    return msg
        reader = asyncio.create_task(read_until_close())
        await self._until(lambda: len(self._session_updates()) >= 2 or reader.done())
        if reader.done():
            close = reader.result()
            self.fail(f"server killed the socket: {close.data} {close.extra}")
        reader.cancel()
        await browser.close()

    async def test_upstream_socket_does_not_offer_deflate(self):
        browser = await self.client.ws_connect("/realtime")
        await self._until(lambda: self.fake.request_headers)
        headers = {k.lower(): v for k, v in self.fake.request_headers[0].items()}
        self.assertNotIn("sec-websocket-extensions", headers)
        await browser.close()


class EdgeSocketTransportTests(unittest.IsolatedAsyncioTestCase):
    """USE_LOCAL_PIPELINE serves the same browser client on /realtime."""

    async def test_edge_handshake_does_not_negotiate_permessage_deflate(self):
        pipeline = RTLocalPipeline(voice_choice="marin")
        app = web.Application()
        pipeline.attach_to_app(app, "/realtime")
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            browser = await client.ws_connect("/realtime", compress=15)
            self.assertEqual(browser.compress, 0, "edge server accepted permessage-deflate")
            await browser.close()
        finally:
            await client.close()


class CompressionConfigTests(unittest.TestCase):

    def test_browser_socket_compression_defaults_off(self):
        self.assertIs(rtmt_module._WS_COMPRESS, False)

    def test_config_yaml_keeps_compression_off(self):
        cfg = yaml.safe_load((BACKEND / "config.yaml").read_text(encoding="utf-8"))
        self.assertIs(cfg["connection"]["ws_compression"], False)

    def test_compression_is_off_unless_configured_on(self):
        self.assertIs(rtmt_module.ws_compression_enabled({}), False)
        self.assertIs(rtmt_module.ws_compression_enabled({"connection": None}), False)
        self.assertIs(rtmt_module.ws_compression_enabled({"connection": {}}), False)
        self.assertIs(rtmt_module.ws_compression_enabled({"connection": {"ws_compression": True}}), True)

    def test_upstream_ws_connect_pins_compression_off(self):
        rtmt = RTMiddleTier("https://fake.openai.azure.com", "d", AzureKeyCredential("k"))
        captured = {}

        class _Stop(Exception):
            pass

        def fake_ws_connect(self_, *args, **kwargs):
            captured.update(kwargs)
            raise _Stop

        with patch.object(aiohttp.ClientSession, "ws_connect", fake_ws_connect):
            with self.assertRaises(_Stop):
                asyncio.run(rtmt._forward_messages(SimpleNamespace(headers={})))
        self.assertEqual(captured.get("compress"), 0)


if __name__ == "__main__":
    unittest.main()
