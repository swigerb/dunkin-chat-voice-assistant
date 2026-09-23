"""Test doubles for the cloud realtime path: a scripted GA upstream and a harness
that runs the real middle tier (RTMiddleTier._forward_messages) between a test
browser socket and that upstream."""
import asyncio
import json
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

from aiohttp import WSMsgType, web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402
from azure.core.credentials import AzureKeyCredential  # noqa: E402

from rtmt import RTMiddleTier  # noqa: E402


def rate_limited_done(response_id="resp_rl"):
    return {"type": "response.done", "response": {
        "id": response_id, "status": "failed", "output": [],
        "status_details": {"type": "failed", "error": {
            "type": "invalid_request_error", "code": "inference_rate_limit_exceeded",
            "message": "too many tokens"}}}}


class FakeClock:
    def __init__(self, start: float = 1000.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class ScriptedUpstream:
    """/openai/v1/realtime. Every upstream connection is recorded; the test can
    push events down the most recent one. `rate_limit_next` makes the next N
    response.create calls fail with a rate-limit response.done."""

    def __init__(self):
        self.connections: list[list[dict]] = []
        self.sockets: list[web.WebSocketResponse] = []
        self.rate_limit_next = 0
        self.connected = asyncio.Event()

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/openai/v1/realtime", self.handler)
        return app

    async def handler(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        frames: list[dict] = []
        self.connections.append(frames)
        self.sockets.append(ws)
        self.connected.set()
        await ws.send_json({"type": "session.created", "session": {"type": "realtime"}})
        n = 0
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            event = json.loads(msg.data)
            frames.append(event)
            kind = event.get("type")
            if kind == "session.update":
                await ws.send_json({"type": "session.updated", "session": {"type": "realtime"}})
            elif kind == "response.create":
                n += 1
                rid = f"r{len(self.connections)}_{n}"
                await ws.send_json({"type": "response.created", "response": {"id": rid}})
                if self.rate_limit_next > 0:
                    self.rate_limit_next -= 1
                    await ws.send_json(rate_limited_done(rid))
                else:
                    await ws.send_json({"type": "response.done", "response": {
                        "id": rid, "status": "completed", "output": []}})
        return ws

    async def push(self, event: dict, index: int = -1) -> None:
        await self.sockets[index].send_json(event)

    def frames(self, index: int = -1, kind: str | None = None) -> list[dict]:
        frames = self.connections[index]
        return [f for f in frames if kind is None or f.get("type") == kind]


class Browser:
    """A test browser socket that keeps every event it receives."""

    def __init__(self, ws):
        self.ws = ws
        self.events: list[dict] = []
        self.close_code: int | None = None
        self.close_reason: str | None = None

    async def send(self, event: dict) -> None:
        await self.ws.send_json(event)

    async def wait_for(self, predicate, timeout: float = 2.0) -> dict:
        """Next event (already received or new) matching `predicate`."""
        for event in self.events:
            if predicate(event):
                return event
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError(f"no matching event; got {[e.get('type') for e in self.events]}")
            msg = await asyncio.wait_for(self.ws.receive(), remaining)
            if msg.type == WSMsgType.TEXT:
                event = json.loads(msg.data)
                self.events.append(event)
                if predicate(event):
                    return event
            elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED):
                self.close_code = self.ws.close_code
                self.close_reason = msg.extra if isinstance(msg.extra, str) else None
                raise ConnectionError(f"closed {self.close_code} {self.close_reason}")

    async def wait_type(self, kind: str, timeout: float = 2.0) -> dict:
        return await self.wait_for(lambda e: e.get("type") == kind, timeout)

    async def drain(self, seconds: float = 0.2) -> None:
        try:
            await self.wait_for(lambda e: False, seconds)
        except (TimeoutError, ConnectionError):
            pass

    async def wait_closed(self, timeout: float = 2.0) -> tuple[int | None, str | None]:
        try:
            await self.wait_for(lambda e: False, timeout)
        except ConnectionError:
            pass
        return self.close_code, self.close_reason

    def of_type(self, kind: str) -> list[dict]:
        return [e for e in self.events if e.get("type") == kind]


class MiddleTierHarness:
    """Real RTMiddleTier between test browsers and a ScriptedUpstream."""

    def __init__(self, configure=None):
        self.upstream = ScriptedUpstream()
        self.clock = FakeClock()
        self.sleeps: list[float] = []
        self._configure = configure

    async def __aenter__(self):
        self._upstream_server = TestServer(self.upstream.app())
        await self._upstream_server.start_server()
        self.rtmt = RTMiddleTier(str(self._upstream_server.make_url("")), "gpt-realtime-2.1",
                                 AzureKeyCredential("k"))
        self.rtmt.sessions._clock = self.clock

        async def fast_sleep(delay):
            self.sleeps.append(delay)
            await asyncio.sleep(0)

        self.rtmt._sleep = fast_sleep
        if self._configure is not None:
            self._configure(self.rtmt)
        app = web.Application()
        self.rtmt.attach_to_app(app, "/realtime")
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        return self

    async def __aexit__(self, *exc):
        await self.client.close()
        await self._upstream_server.close()

    async def connect(self) -> Browser:
        self.upstream.connected.clear()
        ws = await self.client.ws_connect("/realtime")
        await asyncio.wait_for(self.upstream.connected.wait(), 2.0)
        return Browser(ws)

    @property
    def sessions(self):
        return self.rtmt.sessions

    async def settle(self, rounds: int = 5) -> None:
        for _ in range(rounds):
            await asyncio.sleep(0.01)
