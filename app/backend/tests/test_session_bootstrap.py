"""Session bootstrap / greeting / voice-lock regressions (ported from Sonic).

The failure being guarded against: the browser's socket is auto-reconnected
(react-use-websocket `shouldReconnect: () => true`) while the mic is live. The
fresh upstream realtime session runs on service defaults -- no tools, generic
instructions, server VAD auto-responding -- and the model speaks before the
browser's session.update arrives. Once assistant audio exists, GA rejects any
session.update whose voice differs from the current one with
`cannot_update_voice`, and it rejects the whole event, so tools/tool_choice/
instructions are never registered and every order silently goes nowhere.

These tests drive the real middle tier end to end against a fake GA realtime
server that enforces the two service behaviours involved:
  * server VAD auto-creates a response as soon as mic audio arrives;
  * a session.update carrying a *different* voice after assistant audio is
    rejected wholesale with `cannot_update_voice` (same voice / no voice is OK).
"""

import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.append(str(Path(__file__).resolve().parents[1]))

from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer
from azure.core.credentials import AzureKeyCredential

import rtmt as rtmt_module
from rtmt import RTMiddleTier, Tool

SYSTEM_PROMPT = "You are a Dunkin crew member taking drive-thru orders."
# Dunkin registers exactly these three tools (tools.py attach_tools_rtmt).
TOOL_NAMES = ["search", "update_order", "get_order"]

# Exactly what app/frontend/src/hooks/useRealtime.tsx startSession() sends
# (App.tsx sets enableInputAudioTranscription: true).
BROWSER_SESSION_UPDATE = {
    "type": "session.update",
    "session": {
        "turn_detection": {"type": "server_vad", "threshold": 0.7, "prefix_padding_ms": 300, "silence_duration_ms": 500},
        "input_audio_transcription": {"model": "whisper-1"},
    },
}
MIC_FRAME = {"type": "input_audio_buffer.append", "audio": "AAAA"}


class FakeGARealtime:
    """Minimal stand-in for /openai/v1/realtime with the GA rules that matter here."""

    def __init__(self):
        self.session = {"tools": [], "tool_choice": "auto", "instructions": "default assistant", "voice": "alloy"}
        self.assistant_audio = False
        self.vad_fired = False
        self.received: list[dict] = []
        self.errors: list[dict] = []
        self.response_sessions: list[dict] = []
        self.request_headers: list[dict] = []
        # When False the fake never acknowledges a session.update.
        self.ack_session_updates = True
        # GA rejects a session.update wholesale if ANY field is unsupported.
        # reject_keys: top-level GA session keys that get an update rejected.
        # echo_event_id=False mimics gpt-realtime-1.5 rejecting `reasoning`
        # (no error.event_id, no param).
        self.reject_keys: set[str] = set()
        self.reject_every_update = False
        self.echo_event_id = True

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/openai/v1/realtime", self.handler)
        return app

    def _snapshot(self) -> dict:
        return {**self.session, "tools": [t.get("name") for t in self.session["tools"]]}

    async def handler(self, request: web.Request) -> web.WebSocketResponse:
        self.request_headers.append(dict(request.headers))
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"type": "session.created", "session": {"type": "realtime", **self._snapshot()}})
        async for msg in ws:
            event = json.loads(msg.data)
            self.received.append(event)
            kind = event.get("type")
            if kind == "session.update":
                await self._session_update(ws, event["session"], event.get("event_id"))
            elif kind == "input_audio_buffer.append" and not self.vad_fired:
                self.vad_fired = True  # server VAD: speech detected -> auto response
                await self._respond(ws)
            elif kind == "response.create":
                await self._respond(ws)
            elif kind == "conversation.item.delete":
                # Unrelated client event rejected, with its event_id echoed.
                await self._error(ws, "item_not_found", "item_id", event.get("event_id"), "No such item.")
            elif kind == "input_audio_buffer.commit":
                # Unrelated rejection with no event_id and no param.
                await self._error(ws, "input_audio_buffer_commit_empty", None, None, "Buffer too small.")
        return ws

    async def _error(self, ws, code, param, event_id, text) -> None:
        error = {"type": "error", "event_id": "event_srv", "error": {
            "type": "invalid_request_error", "code": code, "message": text, "param": param,
            "event_id": event_id}}
        self.errors.append(error)
        await ws.send_json(error)

    async def _session_update(self, ws, session: dict, event_id: str | None = None) -> None:
        rejected = sorted(k for k in session if k in self.reject_keys)
        if self.reject_every_update or rejected:
            key = rejected[0] if rejected else "instructions"
            await self._error(ws, "invalid_value",
                              f"session.{key}" if self.echo_event_id else None,
                              event_id if self.echo_event_id else None,
                              "Unsupported option for this model.")
            return
        voice = (session.get("audio") or {}).get("output", {}).get("voice")
        if voice is not None and self.assistant_audio and voice != self.session["voice"]:
            error = {"type": "error", "error": {
                "type": "invalid_request_error", "code": "cannot_update_voice",
                "message": "Cannot update a conversation's voice if assistant audio is present."}}
            self.errors.append(error)
            await ws.send_json(error)
            return
        for key in ("tools", "tool_choice", "instructions"):
            if key in session:
                self.session[key] = session[key]
        if voice is not None:
            self.session["voice"] = voice
        if self.ack_session_updates:
            await ws.send_json({"type": "session.updated", "session": {"type": "realtime", **self._snapshot()}})

    async def _respond(self, ws) -> None:
        self.response_sessions.append(self._snapshot())
        await ws.send_json({"type": "response.created", "response": {"id": "resp"}})
        await ws.send_json({"type": "response.output_audio.delta", "delta": "AAAA"})
        self.assistant_audio = True
        await ws.send_json({"type": "response.output_audio.done"})
        await ws.send_json({"type": "response.done", "response": {"id": "resp", "output": [
            {"type": "message", "content": [{"type": "audio", "transcript": "hi"}]}]}})


class _RealtimeHarness(unittest.IsolatedAsyncioTestCase):
    """Real middle tier <-> FakeGARealtime, driven through a fake browser socket."""

    async def asyncSetUp(self):
        self.fake = FakeGARealtime()
        self.fake_server = TestServer(self.fake.app())
        await self.fake_server.start_server()

        self.rtmt = RTMiddleTier(
            endpoint=str(self.fake_server.make_url("")),
            deployment="gpt-realtime-test",
            credentials=AzureKeyCredential("test-key"),
            voice_choice="shimmer",
        )
        self.rtmt.system_message = SYSTEM_PROMPT
        for name in TOOL_NAMES:
            self.rtmt.tools[name] = Tool(target=MagicMock(), schema={"type": "function", "name": name})

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

    async def _response_done(self, browser, timeout=5.0):
        async def recv():
            while True:
                msg = await browser.receive()
                if json.loads(msg.data).get("type") == "response.done":
                    return
        await asyncio.wait_for(recv(), timeout)

    def _session_updates(self):
        return [e for e in self.fake.received if e["type"] == "session.update"]

    def _fallbacks(self):
        return [e for e in self._session_updates() if str(e.get("event_id", "")).startswith("dunkin_fallback")]

    async def _browser_events(self, browser, duration=0.3):
        events = []
        try:
            while True:
                msg = await asyncio.wait_for(browser.receive(), duration)
                if msg.type != WSMsgType.TEXT:
                    return events
                events.append(json.loads(msg.data))
        except TimeoutError:
            return events


class SessionBootstrapTests(_RealtimeHarness):

    async def test_reconnected_socket_with_live_mic_still_registers_tools(self):
        """The production failure: mic audio reaches the upstream before the
        browser's session.update. The model must still have our tools."""
        browser = await self.client.ws_connect("/realtime")
        await browser.send_json(MIC_FRAME)
        await self._response_done(browser)          # VAD auto-response
        await browser.send_json(BROWSER_SESSION_UPDATE)
        await self._response_done(browser)          # greeting

        first = self.fake.response_sessions[0]
        self.assertEqual(first["tools"], TOOL_NAMES,
                         "the model answered the guest with no tools registered")
        self.assertEqual(first["tool_choice"], "auto")
        self.assertEqual(first["instructions"], SYSTEM_PROMPT)
        self.assertEqual(self.fake.errors, [], "a session.update was rejected")
        self.assertEqual(self.fake.session["tools"], [{"type": "function", "name": n} for n in TOOL_NAMES])
        self.assertEqual(self.fake.session["tool_choice"], "auto")
        await browser.close()

    async def test_first_upstream_frame_is_the_server_session_config(self):
        browser = await self.client.ws_connect("/realtime")
        await browser.send_json(MIC_FRAME)
        await self._until(lambda: len(self.fake.received) >= 2)

        first = self.fake.received[0]
        self.assertEqual(first["type"], "session.update")
        session = first["session"]
        self.assertEqual([t["name"] for t in session["tools"]], TOOL_NAMES)
        self.assertEqual(session["tool_choice"], "auto")
        self.assertEqual(session["instructions"], SYSTEM_PROMPT)
        self.assertEqual(session["audio"]["output"]["voice"], "shimmer")
        self.assertEqual(session["audio"]["input"]["turn_detection"], BROWSER_SESSION_UPDATE["session"]["turn_detection"])
        self.assertEqual(session["audio"]["input"]["transcription"], {"model": "whisper-1"})
        self.assertEqual(session["type"], "realtime")
        await browser.close()

    async def test_mic_audio_alone_does_not_trigger_the_greeting(self):
        """The old code greeted on the first browser frame of ANY kind, so a
        reconnect with a live mic greeted before the session was configured."""
        browser = await self.client.ws_connect("/realtime")
        await browser.send_json(MIC_FRAME)
        await self._response_done(browser)
        await asyncio.sleep(0.2)
        self.assertFalse(any(e["type"] == "conversation.item.create" for e in self.fake.received))
        await browser.close()

    async def test_greeting_follows_the_browser_session_update_and_its_ack(self):
        browser = await self.client.ws_connect("/realtime")
        await browser.send_json(BROWSER_SESSION_UPDATE)
        await self._response_done(browser)

        kinds = [e["type"] for e in self.fake.received]
        self.assertEqual(kinds, ["session.update", "session.update", "conversation.item.create", "response.create"])
        self.assertEqual(self.fake.response_sessions[0]["tools"], TOOL_NAMES)
        self.assertEqual(self.fake.response_sessions[0]["voice"], "shimmer")
        await browser.close()

    async def test_greeting_waits_for_session_updated(self):
        """No ack yet -> no greeting. After the timeout it greets anyway."""
        self.fake.ack_session_updates = False
        with patch.object(rtmt_module, "_SESSION_CONFIGURED_TIMEOUT_SEC", 0.5):
            browser = await self.client.ws_connect("/realtime")
            await browser.send_json(BROWSER_SESSION_UPDATE)
            await asyncio.sleep(0.25)
            self.assertFalse(any(e["type"] == "conversation.item.create" for e in self.fake.received),
                             "greeted before the service confirmed the session config")
            await self._response_done(browser)
        self.assertEqual(sum(e["type"] == "conversation.item.create" for e in self.fake.received), 1)
        await browser.close()

    async def test_greeting_is_sent_once(self):
        browser = await self.client.ws_connect("/realtime")
        await browser.send_json(BROWSER_SESSION_UPDATE)
        await self._response_done(browser)
        await browser.send_json(BROWSER_SESSION_UPDATE)          # mic re-toggle
        await self._until(lambda: len(self._session_updates()) >= 3)
        await asyncio.sleep(0.2)
        self.assertEqual(sum(e["type"] == "conversation.item.create" for e in self.fake.received), 1)
        await browser.close()

    async def test_session_update_after_assistant_audio_is_not_rejected_for_voice(self):
        """voice_choice is process-wide, so another tab can change it mid-call.
        A re-sent session.update (mic re-toggle) must not carry a new voice."""
        browser = await self.client.ws_connect("/realtime")
        await browser.send_json(BROWSER_SESSION_UPDATE)
        await self._response_done(browser)          # greeting -> assistant audio present
        self.rtmt.voice_choice = "coral"
        await browser.send_json(BROWSER_SESSION_UPDATE)
        await self._until(lambda: len(self._session_updates()) >= 3)
        await asyncio.sleep(0.1)

        self.assertEqual(self.fake.errors, [])
        last = self._session_updates()[-1]["session"]
        self.assertNotIn("voice", (last.get("audio") or {}).get("output", {}))
        self.assertEqual([t["name"] for t in last["tools"]], TOOL_NAMES)
        await browser.close()

    async def test_voice_picker_after_assistant_audio_is_deferred(self):
        browser = await self.client.ws_connect("/realtime")
        await browser.send_json(BROWSER_SESSION_UPDATE)
        await self._response_done(browser)
        before = len(self._session_updates())
        await browser.send_json({"type": "extension.set_voice", "voice": "coral"})
        await asyncio.sleep(0.2)

        self.assertEqual(len(self._session_updates()), before, "voice change was sent and would be rejected")
        self.assertEqual(self.fake.errors, [])
        self.assertEqual(self.rtmt.voice_choice, "coral")
        await browser.close()

    async def test_voice_picker_before_assistant_audio_is_applied(self):
        browser = await self.client.ws_connect("/realtime")
        await self._until(lambda: len(self._session_updates()) >= 1)
        await browser.send_json({"type": "extension.set_voice", "voice": "coral"})
        await self._until(lambda: len(self._session_updates()) >= 2)
        await asyncio.sleep(0.1)

        self.assertEqual(self.fake.session["voice"], "coral")
        self.assertEqual(self.fake.errors, [])
        await browser.close()

    async def test_voice_choice_sent_before_start_session_is_used_for_the_greeting(self):
        """App.tsx sends extension.set_voice BEFORE startSession(), so the
        greeting is spoken in the picked voice rather than locking the old one."""
        browser = await self.client.ws_connect("/realtime")
        await browser.send_json({"type": "extension.set_voice", "voice": "coral"})
        await browser.send_json(BROWSER_SESSION_UPDATE)
        await self._response_done(browser)
        self.assertEqual(self.fake.response_sessions[0]["voice"], "coral")
        self.assertEqual(self.fake.errors, [])
        await browser.close()

    async def test_bootstrap_does_not_trigger_an_unprompted_greeting(self):
        """Greeting belongs to the browser's session.update (mic pressed), not to
        the bootstrap session.updated that arrives as soon as the page connects."""
        browser = await self.client.ws_connect("/realtime")
        await self._until(lambda: len(self._session_updates()) >= 1)
        await asyncio.sleep(0.3)
        self.assertFalse(any(e["type"] == "conversation.item.create" for e in self.fake.received))
        self.assertEqual(self.fake.response_sessions, [])

        await browser.send_json(BROWSER_SESSION_UPDATE)
        await self._response_done(browser)
        self.assertEqual(sum(e["type"] == "conversation.item.create" for e in self.fake.received), 1)
        self.assertEqual(self.fake.response_sessions[0]["tools"], TOOL_NAMES)
        await browser.close()

    async def test_client_request_id_header_is_forwarded_with_auth(self):
        """ba8c94d: the api-key assignment used to overwrite the headers dict,
        dropping x-ms-client-request-id (needed to correlate with AOAI logs)."""
        browser = await self.client.ws_connect("/realtime", headers={"x-ms-client-request-id": "dunkin-req-42"})
        await self._until(lambda: len(self.fake.request_headers) >= 1)
        headers = {k.lower(): v for k, v in self.fake.request_headers[0].items()}
        self.assertEqual(headers.get("x-ms-client-request-id"), "dunkin-req-42")
        self.assertEqual(headers.get("api-key"), "test-key")
        await browser.close()


class BuildSessionTests(unittest.TestCase):

    def _rtmt(self):
        rtmt = RTMiddleTier("https://fake.openai.azure.com", "gpt-realtime-test",
                            AzureKeyCredential("k"), voice_choice="shimmer")
        rtmt.system_message = SYSTEM_PROMPT
        rtmt.tools["update_order"] = Tool(target=MagicMock(), schema={"type": "function", "name": "update_order"})
        return rtmt

    def test_voice_locked_omits_voice_but_keeps_tools(self):
        session = self._rtmt()._build_session({}, voice_locked=True)
        self.assertNotIn("audio", session)
        self.assertEqual(session["tool_choice"], "auto")
        self.assertEqual(session["tools"][0]["name"], "update_order")
        self.assertEqual(session["instructions"], SYSTEM_PROMPT)

    def test_voice_locked_keeps_input_audio_settings(self):
        session = self._rtmt()._build_session(dict(BROWSER_SESSION_UPDATE["session"]), voice_locked=True)
        self.assertNotIn("output", session["audio"])
        self.assertIn("turn_detection", session["audio"]["input"])

    def test_unlocked_sets_voice(self):
        session = self._rtmt()._build_session({})
        self.assertEqual(session["audio"]["output"]["voice"], "shimmer")

    def test_bootstrap_payload_is_ga_shaped(self):
        payload = json.loads(self._rtmt().build_bootstrap_session_update())
        self.assertEqual(payload["type"], "session.update")
        session = payload["session"]
        self.assertEqual(session["type"], "realtime")
        for legacy in ("voice", "turn_detection", "input_audio_transcription", "temperature",
                       "max_response_output_tokens", "modalities"):
            self.assertNotIn(legacy, session)

    def test_bootstrap_does_not_mutate_the_shared_template(self):
        self._rtmt().build_bootstrap_session_update()
        self.assertEqual(set(rtmt_module._BOOTSTRAP_CLIENT_SESSION), {"turn_detection", "input_audio_transcription"})


if __name__ == "__main__":
    unittest.main()
