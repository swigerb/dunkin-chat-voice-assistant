"""Order resume: the grace hold after a transport drop, the resume handshake,
rehydration of the new upstream, and the silent-guest nudge."""
import asyncio
import sys
import unittest
import unittest.mock
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.append(str(Path(__file__).resolve().parents[1]))

from fake_realtime import FakeClock, MiddleTierHarness, rate_limited_done  # noqa: E402

from config_loader import get_config  # noqa: E402
from order_state import order_state_singleton  # noqa: E402
from session_manager import SessionManager  # noqa: E402

SESSION_UPDATE = {"type": "session.update", "session": {"turn_detection": {"type": "server_vad"}}}


def fake_ws():
    ws = MagicMock()
    ws.close = AsyncMock()
    return ws


class GraceHoldTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.clock = FakeClock()
        self.sm = SessionManager(clock=self.clock)
        self.sm.idle_timeout_seconds = 300
        self.sm.grace_seconds = 120
        self.sm.max_detached = 20
        self.sm.resume_enabled = True

    def tearDown(self):
        for sid in list(self.sm._detached) + list(self.sm._attached):
            self.sm.end_session(sid)

    def test_config_ships_the_decided_grace_hold(self):
        cfg = get_config()["resume"]
        self.assertEqual((cfg["enabled"], cfg["grace_seconds"], cfg["max_detached"]), (True, 120, 20))
        sm = SessionManager()
        self.assertEqual((sm.resume_enabled, sm.grace_seconds, sm.max_detached), (True, 120, 20))

    def test_defaults_without_a_resume_block(self):
        import session_manager
        with unittest.mock.patch.object(session_manager, "_resume_cfg", {}):
            sm = SessionManager()
        self.assertEqual((sm.resume_enabled, sm.grace_seconds, sm.max_detached), (True, 120, 20))

    def test_a_dropped_socket_holds_the_order(self):
        ws = fake_ws()
        sid = self.sm.create_session(ws)
        self.sm.detach_session(ws, sid)
        self.assertIn(sid, order_state_singleton.sessions)
        self.assertTrue(self.sm.is_detached(sid))
        self.assertIsNone(self.sm.get_session_id(ws))
        self.assertEqual((self.sm.active_session_count, self.sm.detached_session_count), (0, 1))

    def test_the_hold_expires_after_the_grace_period(self):
        ws = fake_ws()
        sid = self.sm.create_session(ws)
        self.sm.detach_session(ws, sid)
        self.clock.advance(119.9)
        self.assertEqual(self.sm.sweep_detached(), 0)
        self.assertIn(sid, order_state_singleton.sessions)
        self.clock.advance(0.1)
        self.assertEqual(self.sm.sweep_detached(), 1)
        self.assertNotIn(sid, order_state_singleton.sessions)
        self.assertFalse(self.sm.is_detached(sid))

    def test_the_idle_clock_keeps_running_while_detached(self):
        ws = fake_ws()
        sid = self.sm.create_session(ws)
        self.clock.advance(250)             # 250 s of silence, then the drop
        self.sm.detach_session(ws, sid)
        self.assertEqual(self.sm.detached_expires_at(sid), self.clock() + 50)
        self.clock.advance(49.9)
        self.assertEqual(self.sm.sweep_detached(), 0)
        self.clock.advance(0.1)
        self.assertEqual(self.sm.sweep_detached(), 1)

    def test_a_drop_with_the_idle_budget_spent_ends_the_session(self):
        ws = fake_ws()
        sid = self.sm.create_session(ws)
        self.clock.advance(300)
        self.sm.detach_session(ws, sid)
        self.assertNotIn(sid, order_state_singleton.sessions)
        self.assertEqual(self.sm.detached_session_count, 0)

    async def test_the_idle_check_expires_held_sessions(self):
        ws = fake_ws()
        sid = self.sm.create_session(ws)
        self.sm.detach_session(ws, sid)
        self.clock.advance(120)
        await self.sm.close_idle_sessions()
        self.assertNotIn(sid, order_state_singleton.sessions)
        ws.close.assert_not_awaited()

    def test_oldest_held_session_is_evicted_beyond_max_detached(self):
        self.sm.max_detached = 2
        sids = []
        for _ in range(3):
            ws = fake_ws()
            sid = self.sm.create_session(ws)
            self.clock.advance(1)
            self.sm.detach_session(ws, sid)
            sids.append(sid)
        self.assertNotIn(sids[0], order_state_singleton.sessions)
        self.assertEqual([s for s in sids if self.sm.is_detached(s)], sids[1:])

    def test_resume_disabled_or_zero_grace_ends_on_drop(self):
        for attr, value in (("resume_enabled", False), ("grace_seconds", 0)):
            with self.subTest(**{attr: value}):
                setattr(self.sm, attr, value)
                ws = fake_ws()
                sid = self.sm.create_session(ws)
                self.sm.detach_session(ws, sid)
                self.assertNotIn(sid, order_state_singleton.sessions)
                self.sm.resume_enabled, self.sm.grace_seconds = True, 120

    def test_a_socket_that_no_longer_carries_the_session_does_not_detach_it(self):
        ws, other = fake_ws(), fake_ws()
        sid = self.sm.create_session(ws)
        self.sm.detach_session(other, sid)
        self.assertEqual(self.sm.get_session_id(ws), sid)
        self.assertFalse(self.sm.is_detached(sid))

    def test_ending_a_held_session_forgets_it(self):
        ws = fake_ws()
        sid = self.sm.create_session(ws)
        self.sm.detach_session(ws, sid)
        self.sm.end_session(sid, "test")
        self.assertFalse(self.sm.is_detached(sid))
        self.assertNotIn(sid, order_state_singleton.sessions)


class MiddleTierDetachTests(unittest.IsolatedAsyncioTestCase):

    async def test_browser_drop_detaches_and_closes_the_upstream(self):
        async with MiddleTierHarness() as h:
            browser = await h.connect()
            await browser.send(SESSION_UPDATE)
            await browser.wait_type("extension.session_metadata")
            sid = next(iter(h.sessions._session_map.values()))
            await browser.ws.close()
            await h.settle()
            self.assertTrue(h.sessions.is_detached(sid))
            self.assertIn(sid, order_state_singleton.sessions)
            self.assertTrue(h.upstream.sockets[-1].closed)
            h.sessions.end_session(sid)

    async def test_a_pending_rate_limit_retry_is_cancelled_on_detach(self):
        cancelled = asyncio.Event()

        def configure(rtmt):
            async def blocked_sleep(delay):
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
            rtmt._sleep = blocked_sleep

        async with MiddleTierHarness(configure=configure) as h:
            browser = await h.connect()
            await browser.send(SESSION_UPDATE)
            await browser.wait_type("extension.session_metadata")
            sid = next(iter(h.sessions._session_map.values()))
            await browser.wait_type("response.done")      # the greeting has finished
            await h.upstream.push(rate_limited_done("r_x"))
            await h.settle()
            self.assertFalse(cancelled.is_set())
            await browser.ws.close()
            await asyncio.wait_for(cancelled.wait(), 2)
            await h.settle()
            self.assertTrue(h.sessions.is_detached(sid))
            h.sessions.end_session(sid)


if __name__ == "__main__":
    unittest.main()
