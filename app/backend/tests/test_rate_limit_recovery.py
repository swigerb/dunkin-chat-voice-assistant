"""Rate-limited responses are retried instead of leaving the guest in silence.

The demos share Azure OpenAI quota. Live, a rate-limited turn arrives as

    response.done {status: "failed", status_details: {error: {
        type: "invalid_request_error", code: "inference_rate_limit_exceeded",
        message: "too many tokens"}}}

with no output, so the guest heard nothing. The ladder, per failed response:
retry 1 silently; on a 2nd failure tell the browser (it plays a local apology
clip) and retry again; on a 3rd tell the browser it's final. These tests drive
the real `_process_message_to_client` against fake sockets.
"""
import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.append(str(Path(__file__).resolve().parents[1]))

from aiohttp import WSMsgType, web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402
from azure.core.credentials import AzureKeyCredential  # noqa: E402

from config_loader import get_config  # noqa: E402
from rtmt import (  # noqa: E402
    RateLimitSettings,
    RTMiddleTier,
    RTToolCall,
    Tool,
    ToolResult,
    ToolResultDirection,
    _SessionUpdateGuard,
    is_rate_limit_error,
    parse_retry_hint,
)


def rate_limited_done(message="too many tokens", code="inference_rate_limit_exceeded"):
    return {"type": "response.done", "response": {
        "id": "resp_1", "status": "failed", "output": [],
        "status_details": {"type": "failed", "error": {
            "type": "invalid_request_error", "code": code, "message": message}}}}


SPEECH_STARTED = {"type": "input_audio_buffer.speech_started", "audio_start_ms": 100, "item_id": "item_1"}


class Harness:
    """One upstream connection: middle tier + fake sockets + injectable sleep."""

    def __init__(self, gated=False, settings=None):
        self.rtmt = RTMiddleTier("https://example.invalid", "gpt-realtime-2.1", AzureKeyCredential("k"))
        if settings is not None:
            self.rtmt.rate_limit = settings
        self.delays: list[float] = []
        self.gate = asyncio.Event()
        if not gated:
            self.gate.set()

        async def fake_sleep(delay):
            self.delays.append(delay)
            await self.gate.wait()

        self.rtmt._sleep = fake_sleep
        self.recovery = self.rtmt.new_rate_limit_recovery("sess-1")
        self.server = MagicMock()
        self.server.closed = False
        self.server.send_json = AsyncMock()
        self.server.send_str = AsyncMock()
        self.client = MagicMock()
        self.client.closed = False
        self.client.send_json = AsyncMock()
        self.pending: dict[str, RTToolCall] = {}
        self.guard = _SessionUpdateGuard()

    async def feed(self, event):
        msg = MagicMock()
        msg.data = json.dumps(event)
        out = await self.rtmt._process_message_to_client(msg, self.client, self.server, self.pending,
                                                         self.guard, self.recovery)
        await self.settle()
        return out

    async def settle(self):
        for _ in range(3):
            await asyncio.sleep(0)
        task = self.recovery._retry if self.recovery is not None else None
        if task is not None and self.gate.is_set():
            await asyncio.gather(task, return_exceptions=True)

    def upstream(self, event_type):
        return [c.args[0] for c in self.server.send_json.await_args_list if c.args[0].get("type") == event_type]

    def browser_rate_limit_events(self):
        return [c.args[0] for c in self.client.send_json.await_args_list
                if c.args[0].get("type") == "extension.rate_limited"]


class LadderTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_rate_limit_is_retried_silently_after_the_default_delay(self):
        h = Harness()
        with self.assertLogs("coffee-chat", level="WARNING") as logs:
            await h.feed(rate_limited_done())
        self.assertEqual(h.delays, [1.5])
        self.assertEqual(h.upstream("response.create"), [{"type": "response.create"}])
        self.assertEqual(h.browser_rate_limit_events(), [], "retry 1 is silent")
        self.assertIn("inference_rate_limit_exceeded", logs.output[0])
        self.assertIn("retry_hint=None", logs.output[0])

    async def test_second_failure_tells_the_browser_and_retries_after_4s(self):
        h = Harness()
        await h.feed(rate_limited_done())
        await h.feed({"type": "response.created", "response": {"id": "resp_2"}})
        await h.feed(rate_limited_done())
        self.assertEqual(h.delays, [1.5, 4.0])
        self.assertEqual(h.browser_rate_limit_events(), [{"type": "extension.rate_limited", "attempt": 1}])
        self.assertEqual(len(h.upstream("response.create")), 2)

    async def test_third_failure_is_final_and_stops_retrying(self):
        h = Harness()
        for _ in range(3):
            await h.feed(rate_limited_done())
        self.assertEqual(h.browser_rate_limit_events(), [
            {"type": "extension.rate_limited", "attempt": 1},
            {"type": "extension.rate_limited", "attempt": 2, "final": True},
        ])
        self.assertEqual(len(h.upstream("response.create")), 2, "no retry after the final notice")
        self.assertFalse(h.recovery.retry_pending)
        # The guest's next turn gets a fresh ladder (silent retry again).
        await h.feed(SPEECH_STARTED)
        await h.feed(rate_limited_done())
        self.assertEqual(len(h.upstream("response.create")), 3)
        self.assertEqual(len(h.browser_rate_limit_events()), 2)

    async def test_after_the_final_notice_the_next_failure_starts_over(self):
        h = Harness()
        for _ in range(4):
            await h.feed(rate_limited_done())
        self.assertEqual(len(h.browser_rate_limit_events()), 2, "4th failure = silent retry 1 of a new ladder")
        self.assertEqual(len(h.upstream("response.create")), 3)

    async def test_an_incomplete_response_is_never_retried(self):
        """Audio from an incomplete response was already heard; a retry would repeat it."""
        h = Harness()
        event = rate_limited_done()
        event["response"]["status"] = "incomplete"
        await h.feed(event)
        self.assertEqual(h.upstream("response.create"), [])

    async def test_a_successful_response_resets_the_ladder(self):
        h = Harness()
        await h.feed(rate_limited_done())
        await h.feed({"type": "response.done", "response": {"id": "r", "status": "completed", "output": []}})
        await h.feed(rate_limited_done())
        self.assertEqual(h.browser_rate_limit_events(), [], "a new failed response starts at silent retry 1")
        self.assertEqual(h.delays, [1.5, 1.5])

    async def test_max_retries_is_configurable(self):
        h = Harness(settings=RateLimitSettings(max_retries=1))
        await h.feed(rate_limited_done())
        await h.feed(rate_limited_done())
        self.assertEqual(h.browser_rate_limit_events(), [{"type": "extension.rate_limited", "attempt": 1, "final": True}])
        self.assertEqual(len(h.upstream("response.create")), 1)


class CancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_guest_speech_cancels_a_pending_retry(self):
        h = Harness(gated=True)
        await h.feed(rate_limited_done())
        self.assertTrue(h.recovery.retry_pending)
        out = await h.feed(SPEECH_STARTED)
        self.assertIsNotNone(out, "speech_started still reaches the browser")
        h.gate.set()
        await h.settle()
        self.assertEqual(h.upstream("response.create"), [], "VAD owns the next response")
        self.assertFalse(h.recovery.retry_pending)
        self.assertEqual(h.recovery.failures, 0)

    async def test_a_new_response_cancels_a_pending_retry(self):
        h = Harness(gated=True)
        await h.feed(rate_limited_done())
        await h.feed({"type": "response.created", "response": {"id": "resp_vad"}})
        h.gate.set()
        await h.settle()
        self.assertEqual(h.upstream("response.create"), [])
        self.assertEqual(h.recovery.failures, 0)

    async def test_our_own_retry_response_does_not_reset_the_ladder(self):
        h = Harness()
        await h.feed(rate_limited_done())
        await h.feed({"type": "response.created", "response": {"id": "resp_retry"}})
        self.assertEqual(h.recovery.failures, 1)

    async def test_closing_the_connection_cancels_a_pending_retry(self):
        h = Harness(gated=True)
        await h.feed(rate_limited_done())
        self.assertTrue(h.recovery.cancel("connection closed"))
        h.gate.set()
        await h.settle()
        self.assertEqual(h.upstream("response.create"), [])

    async def test_no_retry_on_a_closed_upstream(self):
        h = Harness(gated=True)
        await h.feed(rate_limited_done())
        h.server.closed = True
        h.gate.set()
        await h.settle()
        self.assertEqual(h.upstream("response.create"), [])


class ScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_rate_limit_failures_are_left_alone(self):
        h = Harness()
        out = await h.feed(rate_limited_done(message="boom", code="server_error"))
        self.assertIsNotNone(out)
        self.assertEqual(h.upstream("response.create"), [])
        self.assertEqual(h.browser_rate_limit_events(), [])
        self.assertEqual(h.delays, [])

    async def test_non_rate_limit_error_event_is_still_logged_and_forwarded(self):
        h = Harness()
        with self.assertLogs("coffee-chat", level="ERROR"):
            out = await h.feed({"type": "error", "error": {"type": "invalid_request_error", "code": "item_not_found"}})
        self.assertEqual(json.loads(out)["error"]["code"], "item_not_found")
        self.assertEqual(h.delays, [])

    async def test_uncorrelated_rate_limit_error_event_enters_the_ladder(self):
        h = Harness()
        out = await h.feed({"type": "error", "error": {
            "type": "rate_limit_exceeded", "code": "rate_limit_exceeded",
            "message": "Rate limit reached. Please try again in 2.5s."}})
        self.assertIsNone(out, "consumed; the browser gets extension.rate_limited instead")
        self.assertEqual(h.delays, [2.5])
        self.assertEqual(len(h.upstream("response.create")), 1)

    async def test_rate_limit_errors_on_our_session_update_stay_with_the_fallback(self):
        h = Harness()
        h.rtmt.system_message = "prompt"
        h.guard.track(h.rtmt.build_bootstrap_session_update(event_id="boot1"))
        error = {"type": "error", "error": {"type": "invalid_request_error", "code": "rate_limit_exceeded",
                                            "event_id": "boot1", "message": "try again in 1s"}}
        with self.assertLogs("coffee-chat", level="ERROR"):
            first = await h.feed(error)
            second = await h.feed(error)  # fallback already claimed: correlated but not consumed
        self.assertIsNone(first)
        fallback = json.loads(h.server.send_str.await_args_list[0].args[0])
        self.assertTrue(fallback["event_id"].startswith("dunkin_fallback_"))
        self.assertIsNotNone(second)
        self.assertEqual(h.upstream("response.create"), [])
        self.assertEqual(h.recovery.failures, 0)

    async def test_disabled_recovery_changes_nothing(self):
        h = Harness(settings=RateLimitSettings(enabled=False))
        self.assertIsNone(h.recovery)
        await h.feed(rate_limited_done())
        self.assertEqual(h.upstream("response.create"), [])


class ToolFollowUpTests(unittest.IsolatedAsyncioTestCase):
    def _with_tool(self, h):
        target = AsyncMock(return_value=ToolResult('{"status":"ok"}', ToolResultDirection.TO_SERVER))
        h.rtmt.tools["update_order"] = Tool(target=target, schema={"name": "update_order"})
        h.pending["call_1"] = RTToolCall("call_1", "prev")
        return target

    async def _run_tool(self, h):
        await h.feed({"type": "response.output_item.done", "item": {
            "type": "function_call", "call_id": "call_1", "name": "update_order", "arguments": "{}"}})

    async def test_retried_follow_up_does_not_run_the_tool_again(self):
        h = Harness()
        target = self._with_tool(h)
        await self._run_tool(h)
        await h.feed({"type": "response.done", "response": {"id": "r1", "status": "completed", "output": [
            {"type": "function_call", "call_id": "call_1"}]}})
        self.assertEqual(len(h.upstream("response.create")), 1, "the normal tool follow-up")
        await h.feed(rate_limited_done())  # the follow-up was rate-limited
        self.assertEqual(len(h.upstream("response.create")), 2)
        target.assert_awaited_once()
        self.assertEqual(len(h.upstream("conversation.item.create")), 1)

    async def test_a_failed_response_with_tools_pending_is_followed_up_once(self):
        h = Harness(gated=True)
        self._with_tool(h)
        await self._run_tool(h)
        await h.feed(rate_limited_done())
        self.assertEqual(h.upstream("response.create"), [], "the retry waits for its delay")
        h.gate.set()
        await h.settle()
        self.assertEqual(len(h.upstream("response.create")), 1)
        self.assertEqual(h.pending, {})


class HintTests(unittest.IsolatedAsyncioTestCase):
    def test_parse_retry_hint(self):
        self.assertEqual(parse_retry_hint("Rate limit reached. Please try again in 2.5s."), 2.5)
        self.assertEqual(parse_retry_hint("Try again in 20 seconds."), 20.0)
        self.assertEqual(parse_retry_hint("retry after 800 ms"), 0.8)
        self.assertEqual(parse_retry_hint("please retry after 3 sec"), 3.0)
        self.assertIsNone(parse_retry_hint("too many tokens"))
        self.assertIsNone(parse_retry_hint("try again in 5 minutes"))
        self.assertIsNone(parse_retry_hint(None))

    def test_is_rate_limit_error(self):
        self.assertTrue(is_rate_limit_error({"code": "inference_rate_limit_exceeded"}))
        self.assertTrue(is_rate_limit_error({"type": "rate_limit_exceeded", "code": None}))
        self.assertFalse(is_rate_limit_error({"type": "invalid_request_error", "code": "server_error"}))
        self.assertFalse(is_rate_limit_error(None))

    async def test_first_retry_hint_is_clamped_to_half_a_second_to_five(self):
        for message, expected in (("try again in 100ms", 0.5), ("try again in 30s", 5.0), ("try again in 2s", 2.0)):
            h = Harness()
            await h.feed(rate_limited_done(message=message))
            self.assertEqual(h.delays, [expected], message)

    async def test_second_retry_hint_is_clamped_to_two_to_eight(self):
        for message, expected in (("try again in 100ms", 2.0), ("try again in 30s", 8.0), ("try again in 3s", 3.0)):
            h = Harness()
            await h.feed(rate_limited_done())
            await h.feed(rate_limited_done(message=message))
            self.assertEqual(h.delays[1], expected, message)

    async def test_warning_log_carries_the_hint(self):
        h = Harness()
        with self.assertLogs("coffee-chat", level="WARNING") as logs:
            await h.feed(rate_limited_done(message="try again in 2.5s"))
        self.assertIn("retry_hint=2.5s", logs.output[0])


class RateLimitingUpstream:
    """/openai/v1/realtime that rate-limits the first `fail` responses."""

    def __init__(self, fail: int):
        self.fail = fail
        self.response_creates = 0

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/openai/v1/realtime", self.handler)
        return app

    async def handler(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"type": "session.created", "session": {"type": "realtime"}})
        async for msg in ws:
            kind = json.loads(msg.data).get("type")
            if kind == "session.update":
                await ws.send_json({"type": "session.updated", "session": {"type": "realtime"}})
            elif kind == "response.create":
                self.response_creates += 1
                await ws.send_json({"type": "response.created", "response": {"id": f"r{self.response_creates}"}})
                if self.response_creates <= self.fail:
                    await ws.send_json(rate_limited_done())
                else:
                    await ws.send_json({"type": "response.done", "response": {
                        "id": f"r{self.response_creates}", "status": "completed", "output": []}})
        return ws


class EndToEndTests(unittest.IsolatedAsyncioTestCase):
    """Browser socket -> real middle tier (_forward_messages) -> fake upstream."""

    async def _run(self, fail: int):
        upstream = RateLimitingUpstream(fail)
        upstream_server = TestServer(upstream.app())
        await upstream_server.start_server()
        rtmt = RTMiddleTier(str(upstream_server.make_url("")), "gpt-realtime-2.1", AzureKeyCredential("k"))
        delays = []

        async def fast_sleep(delay):
            delays.append(delay)

        rtmt._sleep = fast_sleep
        app = web.Application()
        rtmt.attach_to_app(app, "/realtime")
        client = TestClient(TestServer(app))
        await client.start_server()
        events = []
        try:
            browser = await client.ws_connect("/realtime")
            await browser.send_json({"type": "session.update", "session": {"turn_detection": {"type": "server_vad"}}})
            try:
                while True:
                    msg = await asyncio.wait_for(browser.receive(), 1.0)
                    if msg.type != WSMsgType.TEXT:
                        break
                    events.append(json.loads(msg.data))
            except TimeoutError:
                pass
            await browser.close()
        finally:
            await client.close()
            await upstream_server.close()
        return upstream, delays, [e for e in events if e["type"] == "extension.rate_limited"]

    async def test_greeting_survives_two_rate_limits(self):
        upstream, delays, notices = await self._run(fail=2)
        self.assertEqual(upstream.response_creates, 3, "greeting + 2 retries")
        self.assertEqual(delays, [1.5, 4.0])
        self.assertEqual(notices, [{"type": "extension.rate_limited", "attempt": 1}])

    async def test_persistent_rate_limit_ends_with_the_final_notice(self):
        upstream, _, notices = await self._run(fail=10)
        self.assertEqual(upstream.response_creates, 3)
        self.assertEqual(notices[-1], {"type": "extension.rate_limited", "attempt": 2, "final": True})


class SettingsTests(unittest.TestCase):
    def test_config_values_are_read(self):
        settings = RateLimitSettings.from_config({"enabled": True, "retry_delay_seconds": 0.75,
                                                  "second_retry_delay_seconds": 6, "max_retries": 3}, environ={})
        self.assertEqual((settings.retry_delay, settings.second_retry_delay, settings.max_retries), (0.75, 6.0, 3))

    def test_config_yaml_ships_the_ladder(self):
        settings = RateLimitSettings.from_config(get_config()["resilience"]["rate_limit"], environ={})
        self.assertEqual((settings.enabled, settings.retry_delay, settings.second_retry_delay, settings.max_retries),
                         (True, 1.5, 4.0, 2))

    def test_env_override(self):
        self.assertFalse(RateLimitSettings.from_config({"enabled": True},
                                                       environ={"RATE_LIMIT_RECOVERY_ENABLED": "false"}).enabled)
        self.assertTrue(RateLimitSettings.from_config({"enabled": False},
                                                      environ={"RATE_LIMIT_RECOVERY_ENABLED": "true"}).enabled)
        self.assertTrue(RateLimitSettings.from_config(None, environ={}).enabled)


if __name__ == "__main__":
    unittest.main()
