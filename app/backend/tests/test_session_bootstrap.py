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
import unittest.mock
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


class SessionUpdateFallbackTests(_RealtimeHarness):
    """A rejected session.update must never silently cost us the tools.

    GA drops the WHOLE session.update when any one field is unsupported (a
    locked voice, an undeployed transcription model, `reasoning` on 1.5...).
    The middle tier correlates the `error` back to its update and immediately
    resends a minimal one -- instructions + tools only -- exactly once.
    """

    async def _until_browser(self, browser, event_type, timeout=5.0):
        async def recv():
            while True:
                msg = await browser.receive()
                if json.loads(msg.data).get("type") == event_type:
                    return
        await asyncio.wait_for(recv(), timeout)

    async def test_every_session_update_carries_an_event_id(self):
        browser = await self.client.ws_connect("/realtime")
        await self._until_browser(browser, "session.updated")
        await browser.send_json({"type": "extension.set_voice", "voice": "coral"})
        await browser.send_json(BROWSER_SESSION_UPDATE)
        await self._until(lambda: len(self._session_updates()) >= 3)

        ids = [e.get("event_id") for e in self._session_updates()]
        self.assertTrue(all(ids), ids)
        self.assertEqual(len(set(ids)), len(ids))
        await browser.close()

    async def test_rejected_bootstrap_triggers_exactly_one_minimal_fallback_that_registers_tools(self):
        self.fake.reject_keys = {"audio"}      # e.g. an undeployable transcription/voice setting
        browser = await self.client.ws_connect("/realtime")
        await self._until(lambda: len(self._fallbacks()) >= 1)
        events = await self._browser_events(browser)

        bootstrap = self._session_updates()[0]
        self.assertEqual([e["error"]["event_id"] for e in self.fake.errors], [bootstrap["event_id"]])
        fallbacks = self._fallbacks()
        self.assertEqual(len(fallbacks), 1)
        self.assertEqual(set(fallbacks[0]["session"]), {"type", "instructions", "tools", "tool_choice"})
        self.assertEqual(self.fake.session["tools"], [{"type": "function", "name": n} for n in TOOL_NAMES])
        self.assertEqual(self.fake.session["tool_choice"], "auto")
        self.assertEqual(self.fake.session["instructions"], SYSTEM_PROMPT)
        self.assertNotIn("error", [e["type"] for e in events], "a recovered rejection reached the browser")

        await browser.send_json(MIC_FRAME)            # the guest speaks: the model must have its tools
        await self._response_done(browser)
        self.assertEqual(self.fake.response_sessions[0]["tools"], TOOL_NAMES)
        await browser.close()

    async def test_rejected_browser_update_is_recovered_before_the_greeting(self):
        """The browser's own session.update (the one that triggers the greeting)
        is rejected: the fallback restores the tools and the greeting still runs."""
        browser = await self.client.ws_connect("/realtime")
        await self._until_browser(browser, "session.updated")
        self.fake.reject_keys = {"audio"}
        await browser.send_json(BROWSER_SESSION_UPDATE)
        await self._response_done(browser)

        self.assertEqual(len(self.fake.errors), 1)
        self.assertEqual(self.fake.errors[0]["error"]["event_id"], self._session_updates()[1]["event_id"])
        self.assertEqual(len(self._fallbacks()), 1)
        self.assertEqual(self.fake.response_sessions[0]["tools"], TOOL_NAMES)
        self.assertEqual(sum(e["type"] == "conversation.item.create" for e in self.fake.received), 1)
        await browser.close()

    async def test_rejection_without_event_id_is_recovered_and_reasoning_turned_off(self):
        """gpt-realtime-1.5 rejects `reasoning` with no error.event_id and no param."""
        self.rtmt.reasoning_effort = "low"
        self.fake.reject_keys = {"reasoning"}
        self.fake.echo_event_id = False
        browser = await self.client.ws_connect("/realtime")
        await self._until(lambda: len(self._fallbacks()) >= 1)
        events = await self._browser_events(browser)

        self.assertIn("reasoning", self._session_updates()[0]["session"])
        self.assertEqual(len(self._fallbacks()), 1)
        self.assertNotIn("reasoning", self._fallbacks()[0]["session"])
        self.assertEqual(self.fake.session["tools"], [{"type": "function", "name": n} for n in TOOL_NAMES])
        self.assertNotIn("error", [e["type"] for e in events])
        self.assertTrue(self.rtmt._reasoning_rejected)
        self.assertFalse(self.rtmt.reasoning_enabled())

        await browser.send_json(BROWSER_SESSION_UPDATE)   # later updates no longer carry `reasoning`
        await self._response_done(browser)
        self.assertNotIn("reasoning", self._session_updates()[-1]["session"])
        self.assertEqual(len(self.fake.errors), 1)
        await browser.close()

    async def test_unrelated_rejection_does_not_turn_reasoning_off(self):
        self.rtmt.reasoning_effort = "low"
        self.fake.reject_keys = {"audio"}
        browser = await self.client.ws_connect("/realtime")
        await self._until(lambda: len(self._fallbacks()) >= 1)
        self.assertIn("reasoning", self._session_updates()[0]["session"])
        self.assertFalse(self.rtmt._reasoning_rejected)
        self.assertTrue(self.rtmt.reasoning_enabled())
        await browser.close()

    async def test_forced_reasoning_on_a_non_reasoning_deployment_still_registers_tools(self):
        """Operator sets reasoning_model=true on a 1.5 deployment: 1.5 rejects it (no event_id,
        no param), the fallback still registers the tools, and later updates drop `reasoning`."""
        self.rtmt.deployment = "gpt-realtime-1.5"
        self.rtmt.reasoning_model = True
        self.rtmt.reasoning_effort = "minimal"
        self.fake.reject_keys = {"reasoning"}
        self.fake.echo_event_id = False
        browser = await self.client.ws_connect("/realtime")
        await self._until(lambda: len(self._fallbacks()) >= 1)
        events = await self._browser_events(browser)

        self.assertEqual(self._session_updates()[0]["session"]["reasoning"], {"effort": "minimal"})
        self.assertEqual(len(self._fallbacks()), 1)
        self.assertEqual(self.fake.session["tools"], [{"type": "function", "name": n} for n in TOOL_NAMES])
        self.assertNotIn("error", [e["type"] for e in events])
        await browser.send_json(BROWSER_SESSION_UPDATE)
        await self._response_done(browser)
        self.assertNotIn("reasoning", self._session_updates()[-1]["session"])
        self.assertEqual(self.fake.response_sessions[0]["tools"], TOOL_NAMES)
        await browser.close()

    async def _assert_rejected_fallback_does_not_loop(self):
        self.fake.reject_every_update = True
        browser = await self.client.ws_connect("/realtime")
        await self._until(lambda: len(self._fallbacks()) >= 1)
        events = await self._browser_events(browser, duration=0.5)

        self.assertEqual(len(self._session_updates()), 2, "bootstrap + exactly one fallback")
        self.assertEqual(len(self._fallbacks()), 1)
        errors = [e for e in events if e["type"] == "error"]
        self.assertEqual(len(errors), 1, "the fallback's own rejection must reach the browser, once")
        if self.fake.echo_event_id:
            self.assertEqual(errors[0]["error"]["event_id"], self._fallbacks()[0]["event_id"])

        # A new original still gets its own (single) fallback -- and no more.
        await browser.send_json(BROWSER_SESSION_UPDATE)
        await self._until(lambda: len(self._session_updates()) >= 4)
        events = await self._browser_events(browser, duration=0.5)
        self.assertEqual(len(self._session_updates()), 4)
        self.assertEqual(len(self._fallbacks()), 2)
        self.assertEqual(sum(e["type"] == "error" for e in events), 1)
        await browser.close()

    async def test_rejected_fallback_does_not_loop(self):
        await self._assert_rejected_fallback_does_not_loop()

    async def test_rejected_fallback_without_event_id_does_not_loop(self):
        self.fake.echo_event_id = False
        await self._assert_rejected_fallback_does_not_loop()

    async def test_unrelated_errors_do_not_trigger_fallback(self):
        browser = await self.client.ws_connect("/realtime")
        await self._until_browser(browser, "session.updated")     # nothing of ours in flight now
        await browser.send_json({"type": "conversation.item.delete", "item_id": "nope", "event_id": "client_evt_1"})
        await browser.send_json({"type": "input_audio_buffer.commit"})
        await self._until(lambda: len(self.fake.errors) >= 2)
        events = await self._browser_events(browser)

        self.assertEqual(self._fallbacks(), [])
        self.assertEqual(len(self._session_updates()), 1)
        self.assertEqual(sorted(e["error"]["code"] for e in events if e["type"] == "error"),
                         ["input_audio_buffer_commit_empty", "item_not_found"])
        await browser.close()


class UpstreamErrorLoggingTests(unittest.IsolatedAsyncioTestCase):
    """Errors that are not ours to recover are logged, not swallowed."""

    def _rtmt(self):
        rtmt = RTMiddleTier(endpoint="https://example", deployment="gpt-realtime-2.1",
                            credentials=AzureKeyCredential("k"), voice_choice="marin")
        rtmt.transcription_model = "whisper-1"
        return rtmt

    async def _to_client(self, rtmt, event, guard=None):
        msg = MagicMock()
        msg.data = json.dumps(event)
        server_ws = MagicMock()
        server_ws.send_str = unittest.mock.AsyncMock()
        return await rtmt._process_message_to_client(msg, MagicMock(), server_ws, {}, guard), server_ws

    async def test_transcription_failure_is_logged_with_the_model(self):
        rtmt = self._rtmt()
        event = {"type": "conversation.item.input_audio_transcription.failed",
                 "error": {"code": "DeploymentNotFound", "message": "no such deployment"}}
        with self.assertLogs("coffee-chat", level="ERROR") as logs:
            out, _ = await self._to_client(rtmt, event)
        self.assertIsNotNone(out, "the event still reaches the browser")
        self.assertIn("model=whisper-1", logs.output[0])
        self.assertIn("DeploymentNotFound", logs.output[0])

    async def test_unrecovered_error_is_logged_and_forwarded(self):
        from rtmt import _SessionUpdateGuard
        event = {"type": "error", "error": {"type": "invalid_request_error", "code": "item_not_found",
                                            "param": "item_id", "event_id": "client_evt"}}
        with self.assertLogs("coffee-chat", level="ERROR") as logs:
            out, server_ws = await self._to_client(self._rtmt(), event, _SessionUpdateGuard())
        self.assertEqual(json.loads(out)["error"]["code"], "item_not_found")
        server_ws.send_str.assert_not_awaited()
        self.assertIn("item_not_found", logs.output[0])

    async def test_recovered_error_is_consumed_and_fallback_sent_upstream(self):
        from rtmt import _SessionUpdateGuard
        rtmt = self._rtmt()
        rtmt.system_message = SYSTEM_PROMPT
        guard = _SessionUpdateGuard()
        guard.track(rtmt.build_bootstrap_session_update(event_id="boot1"))
        event = {"type": "error", "error": {"type": "invalid_request_error", "code": "invalid_value",
                                            "param": "session.audio", "event_id": "boot1"}}
        with self.assertLogs("coffee-chat", level="ERROR"):
            out, server_ws = await self._to_client(rtmt, event, guard)
        self.assertIsNone(out)
        sent = json.loads(server_ws.send_str.await_args.args[0])
        self.assertTrue(sent["event_id"].startswith("dunkin_fallback_"))
        self.assertEqual(sent["session"]["instructions"], SYSTEM_PROMPT)


    async def test_paramless_rejection_of_an_update_without_reasoning_keeps_reasoning(self):
        """e.g. cannot_update_voice on a voice-only update: no event_id, no param.
        Recovered with a fallback, but it says nothing about `reasoning`."""
        from rtmt import _SessionUpdateGuard
        rtmt = self._rtmt()
        rtmt.reasoning_effort = "low"
        guard = _SessionUpdateGuard()
        guard.track(rtmt.build_voice_update("cedar"))
        event = {"type": "error", "error": {"type": "invalid_request_error", "code": "cannot_update_voice",
                                            "message": "Cannot update a conversation's voice if assistant audio is present."}}
        with self.assertLogs("coffee-chat", level="ERROR"):
            out, server_ws = await self._to_client(rtmt, event, guard)
        self.assertIsNone(out)
        server_ws.send_str.assert_awaited_once()
        self.assertFalse(rtmt._reasoning_rejected)
        self.assertTrue(rtmt.reasoning_enabled())


class SessionUpdateGuardTests(unittest.TestCase):

    def _error(self, event_id=None, param=None, type_="invalid_request_error"):
        return {"type": "error", "error": {"type": type_, "code": "invalid_value", "param": param,
                                           "event_id": event_id}}

    def test_track_adds_event_id_and_keeps_an_existing_one(self):
        from rtmt import _SessionUpdateGuard
        guard = _SessionUpdateGuard()
        added = json.loads(guard.track(json.dumps({"type": "session.update", "session": {}})))
        self.assertTrue(added["event_id"].startswith("dunkin_su_"))
        kept = json.loads(guard.track(json.dumps({"type": "session.update", "event_id": "mine", "session": {}})))
        self.assertEqual(kept["event_id"], "mine")

    def test_correlation_rules(self):
        from rtmt import _SessionUpdateGuard
        guard = _SessionUpdateGuard()
        self.assertIsNone(guard.correlate(self._error()), "nothing in flight")
        guard.stamp({"type": "session.update", "event_id": "su1", "session": {}})
        self.assertIsNone(guard.correlate(self._error(event_id="client_evt")), "explicitly someone else's")
        self.assertIsNone(guard.correlate(self._error(param="item_id")), "not a session field")
        self.assertIsNone(guard.correlate(self._error(type_="server_error")), "not a validation error")
        self.assertEqual(guard.correlate(self._error(param="session.reasoning")), "su1")

        guard.stamp({"type": "session.update", "event_id": "su2", "session": {}})
        guard.on_session_updated()
        self.assertIsNone(guard.correlate(self._error()), "su2 was acknowledged")
        self.assertEqual(guard.correlate(self._error(event_id="su2")), "su2", "echoed id always correlates")

    def test_uncorrelated_rejection_goes_to_the_oldest_in_flight(self):
        from rtmt import _SessionUpdateGuard
        guard = _SessionUpdateGuard()
        guard.stamp({"type": "session.update", "event_id": "su1", "session": {}})
        guard.stamp({"type": "session.update", "event_id": "su2", "session": {}})
        self.assertEqual(guard.correlate(self._error()), "su1")
        self.assertEqual(guard.correlate(self._error()), "su2")
        self.assertIsNone(guard.correlate(self._error()))

    def test_fallback_is_linked_to_its_original(self):
        from rtmt import _SessionUpdateGuard
        guard = _SessionUpdateGuard()
        fb = json.loads(guard.track(json.dumps({"type": "session.update", "session": {"tools": []}}), fallback_of="su1"))
        self.assertTrue(fb["event_id"].startswith("dunkin_fallback_"))
        self.assertEqual(guard.original_of(fb["event_id"]), "su1")
        self.assertEqual(guard.payload_of(fb["event_id"]), {"tools": []})

    def test_tracking_is_bounded(self):
        from rtmt import _SessionUpdateGuard
        guard = _SessionUpdateGuard()
        for i in range(_SessionUpdateGuard._MAX_TRACKED + 10):
            guard.stamp({"type": "session.update", "event_id": f"su{i}", "session": {}})
        self.assertEqual(len(guard._sent), _SessionUpdateGuard._MAX_TRACKED)
        self.assertEqual(guard.payload_of("su0"), {})

    def test_one_fallback_per_original(self):
        from rtmt import _SessionUpdateGuard
        guard = _SessionUpdateGuard()
        self.assertTrue(guard.claim_fallback("su1"))
        self.assertFalse(guard.claim_fallback("su1"))
        self.assertTrue(guard.claim_fallback("su2"))


class FallbackPayloadTests(unittest.TestCase):

    def test_fallback_is_minimal_even_with_reasoning_and_voice_configured(self):
        rtmt = RTMiddleTier(endpoint="https://example", deployment="gpt-realtime-2.1",
                            credentials=AzureKeyCredential("k"), voice_choice="marin")
        rtmt.system_message = SYSTEM_PROMPT
        rtmt.reasoning_effort = "low"
        rtmt.parallel_tool_calls = False
        rtmt.transcription_model = "whisper-1"
        for name in TOOL_NAMES:
            rtmt.tools[name] = Tool(target=MagicMock(), schema={"type": "function", "name": name})
        self.assertIn("reasoning", json.loads(rtmt.build_bootstrap_session_update())["session"])

        fallback = json.loads(rtmt.build_fallback_session_update())
        self.assertEqual(fallback["type"], "session.update")
        self.assertTrue(fallback["event_id"].startswith("dunkin_fallback_"))
        self.assertEqual(fallback["session"], {
            "type": "realtime",
            "instructions": SYSTEM_PROMPT,
            "tools": [{"type": "function", "name": n} for n in TOOL_NAMES],
            "tool_choice": "auto",
        })

    def test_builders_stamp_distinct_event_ids(self):
        rtmt = RTMiddleTier(endpoint="https://example", deployment="gpt-realtime-2.1",
                            credentials=AzureKeyCredential("k"), voice_choice="marin")
        ids = [json.loads(rtmt.build_bootstrap_session_update())["event_id"],
               json.loads(rtmt.build_bootstrap_session_update())["event_id"],
               json.loads(rtmt.build_voice_update("cedar"))["event_id"],
               json.loads(rtmt.build_fallback_session_update())["event_id"]]
        self.assertTrue(all(ids))
        self.assertEqual(len(set(ids)), 4)
        self.assertEqual(json.loads(rtmt.build_bootstrap_session_update(event_id="x"))["event_id"], "x")


class ReasoningAndTranscriptionConfigTests(unittest.TestCase):

    def _rtmt(self, deployment="gpt-realtime-2.1", **attrs):
        rtmt = RTMiddleTier("https://fake.openai.azure.com", deployment, AzureKeyCredential("k"), voice_choice="coral")
        rtmt.system_message = SYSTEM_PROMPT
        rtmt.tools["update_order"] = Tool(target=MagicMock(), schema={"type": "function", "name": "update_order"})
        for key, value in attrs.items():
            setattr(rtmt, key, value)
        return rtmt

    def _bootstrap(self, rtmt):
        return json.loads(rtmt.build_bootstrap_session_update())["session"]

    def test_reasoning_not_sent_when_unconfigured(self):
        session = self._bootstrap(self._rtmt())
        self.assertNotIn("reasoning", session)
        self.assertNotIn("parallel_tool_calls", session)

    def test_reasoning_sent_on_a_reasoning_deployment(self):
        session = self._bootstrap(self._rtmt(reasoning_effort="low", parallel_tool_calls=False))
        self.assertEqual(session["reasoning"], {"effort": "low"})
        self.assertIs(session["parallel_tool_calls"], False)
        self.assertEqual(self._bootstrap(self._rtmt(reasoning_effort="none"))["reasoning"], {"effort": "none"})

    def test_reasoning_survives_the_ga_translation_of_a_browser_update(self):
        rtmt = self._rtmt(reasoning_effort="low")
        session = rtmt._build_session(dict(BROWSER_SESSION_UPDATE["session"]))
        self.assertEqual(session["reasoning"], {"effort": "low"})

    def test_rollback_to_1_5_never_sends_reasoning(self):
        """1.5 rejects `reasoning` (any effort) and parallel_tool_calls=true -- with the tools."""
        for deployment in ("gpt-realtime-1.5", "gpt-realtime", "gpt-realtime-2025-08-28", "gpt-realtime-mini",
                           "gpt-4o-realtime-preview"):
            with self.subTest(deployment=deployment):
                rtmt = self._rtmt(deployment, reasoning_effort="high", parallel_tool_calls=True)
                session = self._bootstrap(rtmt)
                self.assertNotIn("reasoning", session)
                self.assertNotIn("parallel_tool_calls", session)
                self.assertEqual(session["tools"][0]["name"], "update_order")
                self.assertFalse(rtmt.reasoning_enabled())

    def test_deployment_name_check(self):
        from rtmt import deployment_supports_reasoning
        for name in ("gpt-realtime-2", "gpt-realtime-2.1", "GPT-Realtime-2.1", "my-custom-dunkin", "", None):
            self.assertTrue(deployment_supports_reasoning(name), name)
        for name in ("gpt-realtime-1.5", "gpt-realtime-1", "gpt-realtime", "gpt-realtime-2025-08-28",
                     "gpt-realtime-mini-2025-10-06", "gpt-4o-realtime-preview", " gpt-realtime-1.5 ", "GPT-Realtime-1.5"):
            self.assertFalse(deployment_supports_reasoning(name), name)

    def test_disabled_efforts_are_silent_but_unknown_ones_warn(self):
        from rtmt import normalize_reasoning_effort
        for value in ("", "off", "disabled", "false", "null", False):
            with self.subTest(value=value), self.assertNoLogs("coffee-chat", level="WARNING"):
                self.assertIsNone(normalize_reasoning_effort(value))
        with self.assertLogs("coffee-chat", level="WARNING"):
            self.assertIsNone(normalize_reasoning_effort("turbo"))

    def test_client_cannot_inject_reasoning(self):
        rtmt = self._rtmt("gpt-realtime-1.5")
        session = rtmt._build_session({"reasoning": {"effort": "high"}, "parallel_tool_calls": True})
        self.assertNotIn("reasoning", session)
        self.assertNotIn("parallel_tool_calls", session)
        # On a reasoning deployment the server value wins over the client's.
        session = self._rtmt(reasoning_effort="low")._build_session({"reasoning": {"effort": "xhigh"}})
        self.assertEqual(session["reasoning"], {"effort": "low"})

    def test_runtime_rejection_stops_reasoning(self):
        rtmt = self._rtmt(reasoning_effort="low")
        rtmt._reasoning_rejected = True
        self.assertNotIn("reasoning", self._bootstrap(rtmt))
        rtmt.reasoning_model = True                     # a live rejection beats the explicit switch
        self.assertNotIn("reasoning", self._bootstrap(rtmt))

    def test_explicit_reasoning_model_switch_beats_the_name_check(self):
        from rtmt import configure_realtime_model
        cases = [
            # (deployment, config reasoning_model, env switch, reasoning sent?)
            ("gpt-realtime-2.1", "auto", None, True),
            ("gpt-realtime-1.5", "auto", None, False),
            ("gpt-realtime-2.1", False, None, False),          # YAML `false`
            ("gpt-realtime-2.1", "auto", "false", False),
            ("dunkin-prod", "auto", None, True),               # unknown name: assumed reasoning (fallback guards it)
            ("dunkin-prod", "auto", "false", False),
            ("gpt-4o-dunkin", "auto", None, False),
            ("gpt-4o-dunkin", "false", "true", True),          # env wins over config
            ("gpt-realtime-1.5", True, "", True),              # empty env = use config
            ("gpt-realtime-1.5", "bogus", None, False),        # unknown value = auto
        ]
        for deployment, cfg_switch, env_switch, sent in cases:
            with self.subTest(deployment=deployment, cfg=cfg_switch, env=env_switch):
                env = {} if env_switch is None else {"AZURE_OPENAI_REALTIME_REASONING_MODEL": env_switch}
                rtmt = configure_realtime_model(
                    self._rtmt(deployment), {"reasoning_effort": "low", "parallel_tool_calls": False,
                                             "reasoning_model": cfg_switch}, environ=env)
                session = self._bootstrap(rtmt)
                self.assertEqual("reasoning" in session, sent)
                self.assertEqual("parallel_tool_calls" in session, sent)
                self.assertEqual(rtmt.reasoning_enabled(), sent)
                self.assertEqual(session["tools"][0]["name"], "update_order")

    def test_effort_off_or_empty_never_sends_reasoning_even_when_forced(self):
        from rtmt import configure_realtime_model
        for effort_cfg, effort_env in (("", None), ("off", None), ("low", "off"), (None, None)):
            with self.subTest(cfg=effort_cfg, env=effort_env):
                env = {"AZURE_OPENAI_REALTIME_REASONING_MODEL": "true"}
                if effort_env is not None:
                    env["AZURE_OPENAI_REALTIME_REASONING_EFFORT"] = effort_env
                rtmt = configure_realtime_model(self._rtmt(), {"reasoning_effort": effort_cfg}, environ=env)
                self.assertNotIn("reasoning", self._bootstrap(rtmt))
                self.assertFalse(rtmt.reasoning_enabled())

    def test_configure_realtime_model(self):
        from rtmt import configure_realtime_model
        cases = [
            # (config, env, expected effort, expected transcription model)
            ({}, {}, None, "whisper-1"),
            ({"reasoning_effort": "low", "transcription_model": "gpt-4o-transcribe"}, {}, "low", "gpt-4o-transcribe"),
            ({"reasoning_effort": "low"}, {"AZURE_OPENAI_REALTIME_REASONING_EFFORT": "medium"}, "medium", "whisper-1"),
            ({"reasoning_effort": "low"}, {"AZURE_OPENAI_REALTIME_REASONING_EFFORT": "off"}, None, "whisper-1"),
            ({"reasoning_effort": "low"}, {"AZURE_OPENAI_REALTIME_REASONING_EFFORT": ""}, "low", "whisper-1"),
            ({"reasoning_effort": " LOW "}, {}, "low", "whisper-1"),
            ({"reasoning_effort": ""}, {}, None, "whisper-1"),
            ({"reasoning_effort": False}, {}, None, "whisper-1"),      # YAML `off` parses to False
            ({"reasoning_effort": "turbo"}, {}, None, "whisper-1"),
            ({"transcription_model": "whisper-1"},
             {"AZURE_OPENAI_REALTIME_TRANSCRIPTION_MODEL": "my-transcribe-deployment"}, None, "my-transcribe-deployment"),
        ]
        for cfg, env, effort, transcription in cases:
            with self.subTest(cfg=cfg, env=env):
                rtmt = configure_realtime_model(self._rtmt(), cfg, environ=env)
                self.assertEqual(rtmt.reasoning_effort, effort)
                self.assertEqual(rtmt.transcription_model, transcription)
                session = self._bootstrap(rtmt)
                self.assertEqual(session["audio"]["input"]["transcription"], {"model": transcription})
                self.assertEqual(session.get("reasoning"), None if effort is None else {"effort": effort})

    def test_transcription_model_overrides_the_browser_value(self):
        rtmt = self._rtmt(transcription_model="my-transcribe-deployment")
        session = rtmt._build_session(dict(BROWSER_SESSION_UPDATE["session"]))
        self.assertEqual(session["audio"]["input"]["transcription"], {"model": "my-transcribe-deployment"})

    def test_parallel_tool_calls(self):
        from rtmt import configure_realtime_model
        for cfg, expected in ((None, None), (False, False), (True, True)):
            with self.subTest(cfg=cfg):
                rtmt = configure_realtime_model(self._rtmt(), {"reasoning_effort": "low", "parallel_tool_calls": cfg},
                                                environ={})
                self.assertEqual(self._bootstrap(rtmt).get("parallel_tool_calls"), expected)

    def test_shipped_config_is_rollback_safe_and_uses_whisper(self):
        import yaml

        from rtmt import configure_realtime_model
        cfg = yaml.safe_load((Path(__file__).resolve().parents[1] / "config.yaml").read_text(encoding="utf-8"))
        model_cfg = cfg["model"]
        self.assertEqual(model_cfg["transcription_model"], "whisper-1")
        self.assertEqual(model_cfg["reasoning_effort"], "low")
        self.assertEqual(model_cfg["reasoning_model"], "auto")
        self.assertIsNone(model_cfg["parallel_tool_calls"])
        on_21 = self._bootstrap(configure_realtime_model(self._rtmt("gpt-realtime-2.1"), model_cfg, environ={}))
        self.assertEqual(on_21["reasoning"], {"effort": "low"})
        on_15 = self._bootstrap(configure_realtime_model(self._rtmt("gpt-realtime-1.5"), model_cfg, environ={}))
        self.assertNotIn("reasoning", on_15)
        self.assertNotIn("parallel_tool_calls", on_15)

    def test_datazone_deployment_name_is_a_reasoning_model(self):
        """Dunkin runs on `gpt-realtime-2.1-dz` (DataZoneStandard). With the shipped
        `reasoning_model: auto` the name check alone must send `reasoning`; a
        suffixed 1.5 rollback must still not."""
        import yaml

        from rtmt import configure_realtime_model, deployment_supports_reasoning
        model_cfg = yaml.safe_load((Path(__file__).resolve().parents[1] / "config.yaml")
                                   .read_text(encoding="utf-8"))["model"]
        self.assertTrue(deployment_supports_reasoning("gpt-realtime-2.1-dz"))
        rtmt = configure_realtime_model(self._rtmt("gpt-realtime-2.1-dz"), model_cfg, environ={})
        self.assertTrue(rtmt.reasoning_enabled())
        self.assertEqual(self._bootstrap(rtmt)["reasoning"], {"effort": "low"})
        rollback = configure_realtime_model(self._rtmt("gpt-realtime-1.5-dz"), model_cfg, environ={})
        self.assertNotIn("reasoning", self._bootstrap(rollback))


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
