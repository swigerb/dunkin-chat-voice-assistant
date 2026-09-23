"""Idle close: a voice session with no guest activity for
security.idle_timeout_seconds is ended (order deleted) and its socket closed
with 4000 "idle_timeout", so the browser knows not to auto-reconnect.

Guest activity = the guest speaking (speech_started / a transcript from
upstream) or a control frame from the browser. Mic audio frames stream
constantly, silence included, and don't count; neither do middle-tier or model
actions such as a rate-limit retry.
"""
import asyncio
import sys
import unittest
import unittest.mock
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.append(str(Path(__file__).resolve().parents[1]))

from fake_realtime import FakeClock, MiddleTierHarness  # noqa: E402

from config_loader import get_config  # noqa: E402
from order_state import order_state_singleton  # noqa: E402
from session_manager import (  # noqa: E402
    IDLE_CLOSE_CODE,
    IDLE_CLOSE_REASON,
    SessionManager,
)

AUDIO = {"type": "input_audio_buffer.append", "audio": "AAAA"}
SESSION_UPDATE = {"type": "session.update", "session": {"turn_detection": {"type": "server_vad"}}}


def fake_ws():
    ws = MagicMock()
    ws.close = AsyncMock()
    return ws


class SessionManagerIdleTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.clock = FakeClock()
        self.sm = SessionManager(clock=self.clock)
        self.sm.idle_timeout_seconds = 300

    async def test_idle_session_is_ended_and_closed_with_4000(self):
        ws = fake_ws()
        sid = self.sm.create_session(ws)
        self.clock.advance(301)
        closed = await self.sm.close_idle_sessions()
        self.assertEqual(closed, 1)
        ws.close.assert_awaited_once_with(code=4000, message=b"idle_timeout")
        self.assertNotIn(sid, order_state_singleton.sessions)
        self.assertIsNone(self.sm.get_session_id(ws))
        self.assertEqual(self.sm.active_session_count, 0)

    async def test_session_at_the_timeout_is_kept(self):
        ws = fake_ws()
        sid = self.sm.create_session(ws)
        self.clock.advance(300)
        self.assertEqual(await self.sm.close_idle_sessions(), 0)
        ws.close.assert_not_awaited()
        self.assertIn(sid, order_state_singleton.sessions)
        self.sm.end_session(sid)

    async def test_activity_restarts_the_idle_clock(self):
        ws = fake_ws()
        sid = self.sm.create_session(ws)
        self.clock.advance(250)
        self.sm.touch_activity(sid)
        self.clock.advance(250)
        self.assertEqual(await self.sm.close_idle_sessions(), 0)
        self.clock.advance(51)
        self.assertEqual(await self.sm.close_idle_sessions(), 1)

    async def test_only_the_idle_session_is_closed(self):
        idle_ws, busy_ws = fake_ws(), fake_ws()
        self.sm.create_session(idle_ws)
        busy = self.sm.create_session(busy_ws)
        self.clock.advance(200)
        self.sm.touch_activity(busy)
        self.clock.advance(101)
        await self.sm.close_idle_sessions()
        idle_ws.close.assert_awaited_once()
        busy_ws.close.assert_not_awaited()
        self.assertEqual(self.sm.get_session_id(busy_ws), busy)
        self.sm.end_session(busy)

    async def test_socket_close_after_the_idle_close_is_a_no_op(self):
        ws = fake_ws()
        sid = self.sm.create_session(ws)
        self.clock.advance(301)
        await self.sm.close_idle_sessions()
        self.sm.cleanup_session(ws, sid)       # the forwarder's finally
        self.assertNotIn(sid, order_state_singleton.sessions)

    async def test_idle_checker_task_runs_and_stops(self):
        self.sm.idle_check_interval_seconds = 0.01
        ws = fake_ws()
        self.sm.create_session(ws)
        self.clock.advance(301)
        self.sm.start_idle_checker()
        for _ in range(20):
            await asyncio.sleep(0.01)
            if ws.close.await_count:
                break
        ws.close.assert_awaited_once_with(code=IDLE_CLOSE_CODE, message=IDLE_CLOSE_REASON.encode())
        await self.sm.stop_idle_checker()
        self.assertIsNone(self.sm._idle_check_task)

    def test_config_ships_a_five_minute_idle_timeout(self):
        self.assertEqual(get_config()["security"]["idle_timeout_seconds"], 300)
        self.assertEqual(SessionManager().idle_timeout_seconds, 300)
        self.assertEqual((IDLE_CLOSE_CODE, IDLE_CLOSE_REASON), (4000, "idle_timeout"))

    def test_defaults_without_a_security_block(self):
        import session_manager
        with unittest.mock.patch.object(session_manager, "_security_cfg", {}):
            sm = SessionManager()
        self.assertEqual((sm.idle_timeout_seconds, sm.idle_check_interval_seconds), (300, 15))


class MiddleTierIdleTests(unittest.IsolatedAsyncioTestCase):
    """Through the real forwarder: what counts as guest activity."""

    async def _session(self, h):
        browser = await h.connect()
        await browser.send(SESSION_UPDATE)
        await browser.wait_type("extension.session_metadata")
        await h.settle()
        return browser, next(iter(h.sessions._session_map.values()))

    async def test_idle_socket_is_closed_4000_and_order_deleted(self):
        async with MiddleTierHarness() as h:
            browser, sid = await self._session(h)
            h.clock.advance(301)
            await h.sessions.close_idle_sessions()
            code, reason = await browser.wait_closed()
            self.assertEqual((code, reason), (4000, "idle_timeout"))
            self.assertNotIn(sid, order_state_singleton.sessions)
            await h.settle()
            self.assertTrue(h.upstream.sockets[-1].closed, "upstream socket closed too")

    async def test_idle_checker_runs_with_the_app(self):
        async with MiddleTierHarness() as h:
            task = h.sessions._idle_check_task
            self.assertIsNotNone(task)
            self.assertFalse(task.done())
        self.assertTrue(task.done(), "stopped on app cleanup")

    async def test_upstream_connect_failure_does_not_leak_the_session(self):
        async with MiddleTierHarness() as h:
            h.rtmt.endpoint = "http://127.0.0.1:9"      # nothing listens here
            before = set(order_state_singleton.sessions)
            ws = await h.client.ws_connect("/realtime")
            await ws.receive(timeout=5)
            await h.settle()
            self.assertEqual(set(order_state_singleton.sessions) - before, set())
            self.assertEqual(h.sessions.active_session_count, 0)

    async def test_mic_audio_frames_are_not_guest_activity(self):
        async with MiddleTierHarness() as h:
            browser, sid = await self._session(h)
            h.clock.advance(290)
            for _ in range(5):
                await browser.send(AUDIO)
            await h.settle()
            self.assertEqual(len(h.upstream.frames(kind="input_audio_buffer.append")), 5)
            h.clock.advance(11)
            await h.sessions.close_idle_sessions()
            self.assertEqual((await browser.wait_closed())[0], 4000)

    async def test_guest_speech_from_upstream_is_activity(self):
        async with MiddleTierHarness() as h:
            browser, sid = await self._session(h)
            h.clock.advance(290)
            await h.upstream.push({"type": "input_audio_buffer.speech_started", "audio_start_ms": 1, "item_id": "i"})
            await browser.wait_type("input_audio_buffer.speech_started")
            h.clock.advance(11)
            self.assertEqual(await h.sessions.close_idle_sessions(), 0)
            self.assertIn(sid, order_state_singleton.sessions)

    async def test_guest_transcript_from_upstream_is_activity(self):
        async with MiddleTierHarness() as h:
            browser, sid = await self._session(h)
            h.clock.advance(290)
            await h.upstream.push({"type": "conversation.item.input_audio_transcription.completed",
                                   "item_id": "i", "content_index": 0, "transcript": "a medium latte"})
            await browser.wait_type("conversation.item.input_audio_transcription.completed")
            h.clock.advance(11)
            self.assertEqual(await h.sessions.close_idle_sessions(), 0)

    async def test_browser_control_frame_is_activity(self):
        async with MiddleTierHarness() as h:
            browser, sid = await self._session(h)
            h.clock.advance(290)
            await browser.send({"type": "input_audio_buffer.clear"})
            await h.settle()
            h.clock.advance(11)
            self.assertEqual(await h.sessions.close_idle_sessions(), 0)

    async def test_rate_limit_retry_is_not_guest_activity(self):
        async with MiddleTierHarness() as h:
            browser, sid = await self._session(h)
            h.clock.advance(290)
            h.upstream.rate_limit_next = 1
            await h.upstream.push({"type": "response.done", "response": {
                "id": "x", "status": "failed", "output": [], "status_details": {"error": {
                    "type": "invalid_request_error", "code": "inference_rate_limit_exceeded",
                    "message": "too many tokens"}}}})
            await h.settle(20)
            self.assertGreaterEqual(len(h.upstream.frames(kind="response.create")), 2, "retry was sent")
            h.clock.advance(11)
            self.assertEqual(await h.sessions.close_idle_sessions(), 1)


if __name__ == "__main__":
    unittest.main()
