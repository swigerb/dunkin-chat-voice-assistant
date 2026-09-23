"""Order resume: the grace hold after a transport drop, the resume handshake,
rehydration of the new upstream, and the silent-guest nudge."""
import asyncio
import json
import logging
import re
import sys
import unittest
import unittest.mock
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.append(str(Path(__file__).resolve().parents[1]))

from fake_realtime import FakeClock, MiddleTierHarness, rate_limited_done  # noqa: E402

from config_loader import get_config  # noqa: E402
from order_state import order_state_singleton  # noqa: E402
from session_manager import SessionManager, resume_id_fingerprint  # noqa: E402

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

    def _session(self, ws):
        """A session whose browser has been given a resume id (announced)."""
        sid = self.sm.create_session(ws)
        self.assertIsNotNone(self.sm.issue_resume_id(sid))
        return sid

    def tearDown(self):
        for sid in list(self.sm._detached) + list(self.sm._attached):
            self.sm.end_session(sid)

    def test_config_ships_the_decided_grace_hold(self):
        cfg = get_config()["resume"]
        self.assertEqual((cfg["enabled"], cfg["grace_seconds"], cfg["max_detached"]), (True, 120, 20))
        sm = SessionManager()
        self.assertEqual((sm.resume_enabled, sm.grace_seconds, sm.max_detached), (True, 120, 20))
        self.assertEqual((cfg["first_frame_timeout_seconds"], sm.first_frame_timeout_seconds), (2, 2.0))

    def test_defaults_without_a_resume_block(self):
        import session_manager
        with unittest.mock.patch.object(session_manager, "_resume_cfg", {}):
            sm = SessionManager()
        self.assertEqual((sm.resume_enabled, sm.grace_seconds, sm.max_detached), (True, 120, 20))
        self.assertEqual(sm.first_frame_timeout_seconds, 2.0)

    def test_a_dropped_socket_holds_the_order(self):
        ws = fake_ws()
        sid = self._session(ws)
        self.sm.detach_session(ws, sid)
        self.assertIn(sid, order_state_singleton.sessions)
        self.assertTrue(self.sm.is_detached(sid))
        self.assertIsNone(self.sm.get_session_id(ws))
        self.assertEqual((self.sm.active_session_count, self.sm.detached_session_count), (0, 1))

    def test_a_session_never_given_a_resume_id_ends_on_drop(self):
        ws = fake_ws()
        sid = self.sm.create_session(ws)
        self.sm.detach_session(ws, sid)
        self.assertNotIn(sid, order_state_singleton.sessions)
        self.assertEqual(self.sm.detached_session_count, 0)

    def test_the_hold_expires_after_the_grace_period(self):
        ws = fake_ws()
        sid = self._session(ws)
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
        sid = self._session(ws)
        self.clock.advance(250)             # 250 s of silence, then the drop
        self.sm.detach_session(ws, sid)
        self.assertEqual(self.sm.detached_expires_at(sid), self.clock() + 50)
        self.clock.advance(49.9)
        self.assertEqual(self.sm.sweep_detached(), 0)
        self.clock.advance(0.1)
        self.assertEqual(self.sm.sweep_detached(), 1)

    def test_a_drop_with_the_idle_budget_spent_ends_the_session(self):
        ws = fake_ws()
        sid = self._session(ws)
        self.clock.advance(300)
        self.sm.detach_session(ws, sid)
        self.assertNotIn(sid, order_state_singleton.sessions)
        self.assertEqual(self.sm.detached_session_count, 0)

    async def test_the_idle_check_expires_held_sessions(self):
        ws = fake_ws()
        sid = self._session(ws)
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
            sid = self._session(ws)
            self.clock.advance(1)
            self.sm.detach_session(ws, sid)
            sids.append(sid)
        self.assertNotIn(sids[0], order_state_singleton.sessions)
        self.assertEqual([s for s in sids if self.sm.is_detached(s)], sids[1:])

    def test_resume_disabled_or_zero_grace_ends_on_drop(self):
        for attr, value in (("resume_enabled", False), ("grace_seconds", 0)):
            with self.subTest(**{attr: value}):
                ws = fake_ws()
                sid = self._session(ws)
                setattr(self.sm, attr, value)
                self.sm.detach_session(ws, sid)
                self.assertNotIn(sid, order_state_singleton.sessions)
                self.sm.resume_enabled, self.sm.grace_seconds = True, 120

    def test_a_socket_that_no_longer_carries_the_session_does_not_detach_it(self):
        ws, other = fake_ws(), fake_ws()
        sid = self._session(ws)
        self.sm.detach_session(other, sid)
        self.assertEqual(self.sm.get_session_id(ws), sid)
        self.assertFalse(self.sm.is_detached(sid))

    def test_ending_a_held_session_forgets_it(self):
        ws = fake_ws()
        sid = self._session(ws)
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


def add_item(sid, name="Boston Kreme Donut"):
    order_state_singleton.handle_order_update(sid, "add", name, "", 1, 1.99)


class ResumeCredentialTests(unittest.TestCase):

    def setUp(self):
        self.clock = FakeClock()
        self.sm = SessionManager(clock=self.clock)
        self.sm.idle_timeout_seconds, self.sm.grace_seconds, self.sm.resume_enabled = 300, 120, True

    def tearDown(self):
        for sid in list(self.sm._detached) + list(self.sm._attached):
            self.sm.end_session(sid)

    def _dropped(self):
        ws = fake_ws()
        sid = self.sm.create_session(ws)
        rid = self.sm.issue_resume_id(sid)
        self.sm.detach_session(ws, sid)
        return sid, rid

    def test_ids_are_256_bit_urlsafe_and_stored_only_as_digests(self):
        sid = self.sm.create_session(fake_ws())
        rid = self.sm.issue_resume_id(sid)
        self.assertRegex(rid, r"^[A-Za-z0-9_-]{43}$")
        stored = list(self.sm._resume_digests.values()) + list(self.sm._resume_index)
        self.assertNotIn(rid, stored)
        self.assertNotIn(rid, repr(self.sm.__dict__))

    def test_reissuing_rotates_the_id(self):
        sid = self.sm.create_session(fake_ws())
        first = self.sm.issue_resume_id(sid)
        second = self.sm.issue_resume_id(sid)
        self.assertNotEqual(first, second)
        self.assertEqual(len(self.sm._resume_index), 1, "the old id is forgotten, not just shadowed")
        self.assertEqual(self.sm.resume(fake_ws(), first).reason, "unknown")

    def test_no_id_when_disabled_or_for_an_unknown_session(self):
        self.assertIsNone(self.sm.issue_resume_id("no-such-session"))
        sid = self.sm.create_session(fake_ws())
        self.sm.resume_enabled = False
        self.assertIsNone(self.sm.issue_resume_id(sid))

    def test_resume_reattaches_the_order_and_rotates_the_id(self):
        sid, rid = self._dropped()
        add_item(sid)
        new_ws = fake_ws()
        provisional = self.sm.create_session(new_ws)
        outcome = self.sm.resume(new_ws, rid)
        self.assertTrue(outcome.accepted)
        self.assertEqual(outcome.session_id, sid)
        self.assertNotIn(outcome.resume_id, (None, rid))
        self.assertEqual(self.sm.get_session_id(new_ws), sid)
        self.assertFalse(self.sm.is_detached(sid))
        self.assertNotIn(provisional, order_state_singleton.sessions, "the provisional session is ended")
        self.assertEqual(len(order_state_singleton.get_order_summary(sid).items), 1)
        self.assertIsNone(outcome.stale_ws)

    def test_an_id_is_single_use(self):
        sid, rid = self._dropped()
        ws = fake_ws()
        self.sm.create_session(ws)
        self.assertTrue(self.sm.resume(ws, rid).accepted)
        other = fake_ws()
        self.sm.create_session(other)
        self.assertEqual(self.sm.resume(other, rid).reason, "unknown")
        self.assertEqual(self.sm.get_session_id(ws), sid)

    def test_rejection_reasons(self):
        sid, rid = self._dropped()
        ws = fake_ws()
        self.sm.create_session(ws)
        for presented, reason in ((None, "malformed"), (123, "malformed"), ("x" * 31, "malformed"),
                                  ("x" * 129, "malformed"), ("A" * 43, "unknown")):
            with self.subTest(presented=presented):
                self.assertEqual(self.sm.resume(ws, presented).reason, reason)
        self.sm.resume_enabled = False
        self.assertEqual(self.sm.resume(ws, rid).reason, "disabled")
        self.assertIn(sid, order_state_singleton.sessions, "a rejection doesn't touch the held order")

    def test_an_expired_hold_is_rejected_and_ended(self):
        sid, rid = self._dropped()
        self.clock.advance(120)
        ws = fake_ws()
        self.sm.create_session(ws)
        self.assertEqual(self.sm.resume(ws, rid).reason, "expired")
        self.assertNotIn(sid, order_state_singleton.sessions)
        self.assertNotIn(sid, self.sm._resume_digests, "ending a session forgets its resume id")
        self.assertNotIn(sid, self.sm._resume_index.values())

    def test_an_idle_session_is_rejected_as_expired(self):
        stale = fake_ws()
        sid = self.sm.create_session(stale)
        rid = self.sm.issue_resume_id(sid)
        self.clock.advance(301)           # still attached, idle check not run yet
        ws = fake_ws()
        self.sm.create_session(ws)
        self.assertEqual(self.sm.resume(ws, rid).reason, "expired")
        self.assertNotIn(sid, order_state_singleton.sessions)

    def test_a_still_attached_socket_is_superseded(self):
        stale = fake_ws()
        sid = self.sm.create_session(stale)
        rid = self.sm.issue_resume_id(sid)
        self.sm.mark_greeting_sent(sid)
        ws = fake_ws()
        self.sm.create_session(ws)
        outcome = self.sm.resume(ws, rid)
        self.assertTrue(outcome.accepted)
        self.assertIs(outcome.stale_ws, stale)
        self.assertTrue(outcome.conversation_started)
        self.assertIsNone(self.sm.get_session_id(stale))
        self.sm.detach_session(stale, sid)     # the stale socket's own close
        self.assertEqual(self.sm.get_session_id(ws), sid)
        self.assertFalse(self.sm.is_detached(sid))

    def test_conversation_started_is_false_before_the_greeting(self):
        _, rid = self._dropped()
        ws = fake_ws()
        self.sm.create_session(ws)
        self.assertFalse(self.sm.resume(ws, rid).conversation_started)

    def test_resuming_the_session_the_socket_already_carries_is_rejected(self):
        ws = fake_ws()
        sid = self.sm.create_session(ws)
        rid = self.sm.issue_resume_id(sid)
        self.assertEqual(self.sm.resume(ws, rid).reason, "unknown")

    def test_fingerprint_is_the_only_logged_form(self):
        self.assertEqual(resume_id_fingerprint(None), "none")
        self.assertRegex(resume_id_fingerprint("A" * 43), r"^[0-9a-f]{8}$")
        _, rid = self._dropped()
        ws = fake_ws()
        self.sm.create_session(ws)
        with self.assertLogs("coffee-chat", level="INFO") as logs:
            outcome = self.sm.resume(ws, rid)
        text = "\n".join(logs.output)
        self.assertNotIn(rid, text)
        self.assertNotIn(outcome.resume_id, text)
        self.assertIn(resume_id_fingerprint(rid), text)


class MiddleTierResumeTests(unittest.IsolatedAsyncioTestCase):

    async def _fresh(self, h):
        browser = await h.connect()
        await browser.send(SESSION_UPDATE)
        meta = await browser.wait_type("extension.session_metadata")
        await h.settle()
        return browser, h.sessions.get_session_id(self._server_ws(h, meta)), meta

    @staticmethod
    def _server_ws(h, meta):
        for ws, sid in h.sessions._session_map.items():
            if order_state_singleton.get_session_identifiers(sid).session_token == meta["sessionToken"]:
                return ws
        raise AssertionError("no session for that token")

    async def test_a_fresh_session_is_announced_with_a_resume_id(self):
        async with MiddleTierHarness() as h:
            browser, sid, meta = await self._fresh(h)
            self.assertRegex(meta["resumeId"], r"^[A-Za-z0-9_-]{43}$")
            self.assertEqual(len(browser.of_type("extension.session_metadata")), 1)
            h.sessions.end_session(sid)

    async def test_the_announcement_waits_for_the_first_frame_decision(self):
        def configure(rtmt):
            rtmt.sessions.first_frame_timeout_seconds = 0.3
        async with MiddleTierHarness(configure=configure) as h:
            browser = await h.connect()
            await browser.drain(0.15)
            self.assertEqual(browser.of_type("extension.session_metadata"), [], "held until the decision")
            meta = await browser.wait_type("extension.session_metadata", timeout=1)
            self.assertIn("resumeId", meta)

    async def test_resume_restores_the_order_on_a_new_socket(self):
        async with MiddleTierHarness() as h:
            a, sid, meta = await self._fresh(h)
            add_item(sid)
            await a.ws.close()
            await h.settle()
            self.assertTrue(h.sessions.is_detached(sid))

            b = await h.connect()
            await b.send({"type": "extension.resume", "resume_id": meta["resumeId"]})
            resumed = await b.wait_type("extension.session_resumed")
            self.assertEqual([i["item"] for i in resumed["order_summary"]["items"]], ["Boston Kreme Donut"])
            self.assertEqual(resumed["session_token"], meta["sessionToken"])
            self.assertNotEqual(resumed["resume_id"], meta["resumeId"])
            self.assertEqual(set(resumed) >= {"round_trip_index", "round_trip_token"}, True)
            await b.drain(0.2)
            self.assertEqual(b.of_type("extension.session_metadata"), [], "exactly one announcement")
            self.assertEqual(list(h.sessions._session_map.values()), [sid])
            self.assertEqual(h.sessions.detached_session_count, 0)
            # The resumed socket now owns the session: "New order" ends that order.
            await b.send({"type": "extension.end_session"})
            self.assertEqual(await b.wait_closed(), (1000, "session_ended"))
            await h.settle()
            self.assertNotIn(sid, order_state_singleton.sessions)

    async def test_a_used_or_bad_id_is_rejected_and_a_fresh_session_announced(self):
        async with MiddleTierHarness() as h:
            a, sid, meta = await self._fresh(h)
            await a.ws.close()
            await h.settle()
            b = await h.connect()
            await b.send({"type": "extension.resume", "resume_id": meta["resumeId"]})
            await b.wait_type("extension.session_resumed")
            for presented, reason in ((meta["resumeId"], "unknown"), ("short", "malformed")):
                with self.subTest(reason=reason):
                    c = await h.connect()
                    await h.settle()   # upstream session.created handled first: the rejection must announce
                    await c.send({"type": "extension.resume", "resume_id": presented})
                    rejected = await c.wait_type("extension.resume_rejected")
                    self.assertEqual(rejected["reason"], reason)
                    fresh = await c.wait_type("extension.session_metadata")
                    self.assertNotEqual(fresh["sessionToken"], meta["sessionToken"])
                    self.assertIn("resumeId", fresh)
                    await c.ws.close()
            h.sessions.end_session(sid)

    async def test_an_expired_hold_is_rejected(self):
        async with MiddleTierHarness() as h:
            a, sid, meta = await self._fresh(h)
            await a.ws.close()
            await h.settle()
            h.clock.advance(120)
            b = await h.connect()
            await b.send({"type": "extension.resume", "resume_id": meta["resumeId"]})
            self.assertEqual((await b.wait_type("extension.resume_rejected"))["reason"], "expired")
            self.assertNotIn(sid, order_state_singleton.sessions)

    async def test_a_late_resume_is_rejected_and_the_socket_re_announced(self):
        async with MiddleTierHarness() as h:
            a, sid, meta = await self._fresh(h)
            last = h.sessions.last_activity(sid)
            h.clock.advance(10)
            await a.send({"type": "extension.resume", "resume_id": meta["resumeId"]})
            rejected = await a.wait_type("extension.resume_rejected")
            self.assertEqual(rejected["reason"], "not_first_frame")
            again = await a.wait_for(lambda e: e.get("type") == "extension.session_metadata"
                                     and e.get("resumeId") != meta["resumeId"])
            self.assertEqual(again["sessionToken"], meta["sessionToken"])
            self.assertEqual(h.sessions.get_session_id(self._server_ws(h, meta)), sid)
            await h.settle()
            self.assertEqual(h.sessions.last_activity(sid), last, "a resume frame is not activity")
            h.sessions.end_session(sid)

    async def test_a_still_open_socket_is_closed_4002_superseded(self):
        async with MiddleTierHarness() as h:
            a, sid, meta = await self._fresh(h)
            b = await h.connect()
            await b.send({"type": "extension.resume", "resume_id": meta["resumeId"]})
            await b.wait_type("extension.session_resumed")
            self.assertEqual(await a.wait_closed(), (4002, "superseded"))
            await h.settle()
            self.assertEqual(list(h.sessions._session_map.values()), [sid])
            self.assertFalse(h.sessions.is_detached(sid))
            self.assertIn(sid, order_state_singleton.sessions)
            h.sessions.end_session(sid)

    async def test_end_session_closes_1000_and_deletes_the_order(self):
        async with MiddleTierHarness() as h:
            a, sid, meta = await self._fresh(h)
            add_item(sid)
            await a.send({"type": "extension.end_session"})
            self.assertEqual(await a.wait_closed(), (1000, "session_ended"))
            await h.settle()
            self.assertNotIn(sid, order_state_singleton.sessions)
            self.assertEqual(h.sessions.detached_session_count, 0)
            b = await h.connect()
            await b.send({"type": "extension.resume", "resume_id": meta["resumeId"]})
            self.assertEqual((await b.wait_type("extension.resume_rejected"))["reason"], "unknown")

    async def test_resuming_does_not_extend_the_idle_budget(self):
        async with MiddleTierHarness() as h:
            a, sid, meta = await self._fresh(h)
            last = h.sessions.last_activity(sid)
            h.clock.advance(200)
            await a.ws.close()
            await h.settle()
            h.clock.advance(50)
            b = await h.connect()
            await b.send({"type": "extension.resume", "resume_id": meta["resumeId"]})
            await b.wait_type("extension.session_resumed")
            await h.settle()
            self.assertEqual(h.sessions.last_activity(sid), last)
            h.clock.advance(51)
            await h.sessions.close_idle_sessions()
            self.assertEqual(await b.wait_closed(), (4000, "idle_timeout"))

    async def test_resume_ids_never_reach_the_logs(self):
        async with MiddleTierHarness() as h:
            with self.assertLogs("coffee-chat", level=logging.DEBUG) as logs:
                a, sid, meta = await self._fresh(h)
                await a.ws.close()
                await h.settle()
                b = await h.connect()
                await b.send({"type": "extension.resume", "resume_id": meta["resumeId"]})
                resumed = await b.wait_type("extension.session_resumed")
                c = await h.connect()
                await c.send({"type": "extension.resume", "resume_id": meta["resumeId"]})
                await c.wait_type("extension.resume_rejected")
                await h.settle()
            text = "\n".join(logs.output)
            for rid in (meta["resumeId"], resumed["resume_id"]):
                self.assertNotIn(rid, text)
            self.assertIsNotNone(re.search(resume_id_fingerprint(meta["resumeId"]), text))
            h.sessions.end_session(sid)


class TranscriptRingTests(unittest.TestCase):

    def setUp(self):
        self.sm = SessionManager(clock=FakeClock())
        self.sid = self.sm.create_session(fake_ws())

    def tearDown(self):
        self.sm.end_session(self.sid)

    def test_config_ships_the_decided_history_and_nudge(self):
        cfg = get_config()["resume"]
        self.assertEqual((cfg["history_turns"], cfg["history_chars"], cfg["nudge_after_seconds"]), (6, 2000, 30))
        self.assertEqual((self.sm.history_turns, self.sm.history_chars, self.sm.nudge_after_seconds), (6, 2000, 30.0))
        import session_manager
        with unittest.mock.patch.object(session_manager, "_resume_cfg", {}):
            sm = SessionManager()
        self.assertEqual((sm.history_turns, sm.history_chars, sm.nudge_after_seconds), (6, 2000, 30.0))

    def test_keeps_only_the_last_history_turns(self):
        self.sm.history_turns = 3
        for i in range(5):
            self.sm.record_turn(self.sid, "guest", f"turn {i}")
        self.assertEqual(self.sm.recent_turns(self.sid), [("guest", "turn 2"), ("guest", "turn 3"), ("guest", "turn 4")])

    def test_ignores_blank_text_unknown_sessions_and_a_zero_history(self):
        for sid, text in ((self.sid, "  "), (self.sid, None), (None, "hi"), ("no-such-session", "hi")):
            self.sm.record_turn(sid, "guest", text)
        self.assertEqual(self.sm.recent_turns(self.sid), [])
        self.assertEqual(list(self.sm._transcripts), [], "nothing kept for unknown sessions")
        for turns in (0, -1):
            self.sm.history_turns = turns
            self.sm.record_turn(self.sid, "guest", "hello")
            self.assertEqual(self.sm.recent_turns(self.sid), [])

    def test_text_is_stripped_and_each_turn_capped(self):
        self.sm.history_chars = 5
        self.sm.record_turn(self.sid, "crew", "  abcdefgh  ")
        self.assertEqual(self.sm.recent_turns(self.sid), [("crew", "abcde")])

    def test_the_character_budget_keeps_the_newest_text(self):
        self.sm.history_chars = 10
        self.sm.record_turn(self.sid, "guest", "old turn")
        self.sm.record_turn(self.sid, "guest", "aaaa")
        self.sm.record_turn(self.sid, "crew", "bbbbbbbb")
        self.assertEqual(self.sm.recent_turns(self.sid), [("guest", "\u2026a"), ("crew", "bbbbbbbb")])

    def test_rehydration_item_carries_order_and_turns_as_a_system_message(self):
        add_item(self.sid, "Iced Coffee")
        self.sm.record_turn(self.sid, "guest", "An iced coffee please")
        self.sm.record_turn(self.sid, "crew", "Coming right up.")
        event = json.loads(self.sm.build_rehydration_item(self.sid))
        self.assertEqual(event["type"], "conversation.item.create")
        self.assertEqual((event["item"]["type"], event["item"]["role"]), ("message", "system"))
        text = event["item"]["content"][0]["text"]
        self.assertEqual(event["item"]["content"][0]["type"], "input_text")
        self.assertIn("Do NOT greet", text)
        self.assertIn('"item":"Iced Coffee"', text)
        self.assertIn("Guest: An iced coffee please\nCrew: Coming right up.", text)

    def test_rehydration_without_turns_says_so(self):
        text = json.loads(self.sm.build_rehydration_item(self.sid))["item"]["content"][0]["text"]
        self.assertIn("(none recorded)", text)

    def test_nudge_item_is_a_system_message(self):
        event = json.loads(SessionManager.build_nudge_item())
        self.assertEqual((event["type"], event["item"]["role"]), ("conversation.item.create", "system"))
        self.assertIn("anything else", event["item"]["content"][0]["text"])

    def test_ending_a_session_forgets_its_turns(self):
        sid = self.sm.create_session(fake_ws())
        self.sm.record_turn(sid, "guest", "hello")
        self.sm.end_session(sid)
        self.assertNotIn(sid, self.sm._transcripts)


GUEST_SAID = {"type": "conversation.item.input_audio_transcription.completed", "item_id": "i1",
              "content_index": 0, "transcript": "A medium iced coffee please"}
CREW_SAID = {"type": "response.done", "response": {"id": "r_t", "status": "completed", "output": [
    {"type": "message", "role": "assistant",
     "content": [{"type": "output_audio", "transcript": "One medium iced coffee, coming up."}]}]}}


def system_items(frames):
    return [f for f in frames if f.get("type") == "conversation.item.create"
            and (f.get("item") or {}).get("role") == "system"]


class MiddleTierRehydrationTests(unittest.IsolatedAsyncioTestCase):

    async def _conversation(self, h):
        """A greeted guest who ordered, then dropped. Returns (sid, metadata)."""
        a = await h.connect()
        await a.send(SESSION_UPDATE)
        meta = await a.wait_type("extension.session_metadata")
        await a.wait_type("response.done")               # the greeting
        sid = h.sessions.get_session_id(next(iter(h.sessions._attached.values())))
        await h.upstream.push(GUEST_SAID)
        await h.upstream.push(CREW_SAID)
        await a.wait_for(lambda e: e.get("type") == "response.done" and e["response"]["id"] == "r_t")
        add_item(sid, "Iced Coffee")
        await a.ws.close()
        await h.settle()
        return sid, meta

    async def _resume(self, h, meta):
        b = await h.connect()
        await b.send({"type": "extension.resume", "resume_id": meta["resumeId"]})
        await b.wait_type("extension.session_resumed")
        await h.settle()
        return b

    async def _wait_frames(self, h, predicate, timeout=1.0):
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if predicate(h.upstream.frames()):
                return
            await asyncio.sleep(0.01)
        raise AssertionError(f"upstream frames: {[f.get('type') for f in h.upstream.frames()]}")

    async def test_turns_are_recorded_from_transcripts_and_responses(self):
        async with MiddleTierHarness() as h:
            sid, _ = await self._conversation(h)
            self.assertEqual(h.sessions.recent_turns(sid), [
                ("guest", "A medium iced coffee please"), ("crew", "One medium iced coffee, coming up.")])
            h.sessions.end_session(sid)

    async def test_crew_turns_are_spoken_messages_only_with_a_text_fallback(self):
        async with MiddleTierHarness() as h:
            a = await h.connect()
            await a.send(SESSION_UPDATE)
            await a.wait_type("response.done")
            sid = next(iter(h.sessions._session_map.values()))
            await h.upstream.push({"type": "response.done", "response": {"id": "r_f", "status": "completed", "output": [
                {"type": "function_call", "name": "get_order", "call_id": "c1", "arguments": "{}"},
                {"type": "reasoning", "content": [{"type": "reasoning_text", "text": "internal thoughts"}]},
                {"type": "message", "content": [{"type": "output_text", "text": "Anything else?"}]}]}})
            await a.wait_for(lambda e: e.get("type") == "response.done" and e["response"]["id"] == "r_f")
            self.assertEqual(h.sessions.recent_turns(sid), [("crew", "Anything else?")])
            h.sessions.end_session(sid)

    async def test_a_mid_conversation_resume_rehydrates_and_does_not_greet(self):
        async with MiddleTierHarness() as h:
            h.sessions.nudge_after_seconds = 0
            sid, meta = await self._conversation(h)
            b = await self._resume(h, meta)
            frames = h.upstream.frames()
            self.assertEqual([f["type"] for f in frames], ["session.update", "conversation.item.create"],
                             "bootstrap first, then the rehydration, and no response.create")
            text = frames[1]["item"]["content"][0]["text"]
            self.assertEqual(frames[1]["item"]["role"], "system")
            for expected in ("Guest: A medium iced coffee please", "Crew: One medium iced coffee, coming up.",
                             "Iced Coffee", "Do NOT greet"):
                self.assertIn(expected, text)
            await b.send(SESSION_UPDATE)                    # the browser re-configures its session
            await b.drain(0.2)
            self.assertEqual(h.upstream.frames(kind="response.create"), [], "no greeting after a resume")
            self.assertEqual(len(system_items(h.upstream.frames())), 1, "no nudge when disabled")
            h.sessions.end_session(sid)

    async def test_a_resume_before_the_greeting_still_greets(self):
        def configure(rtmt):
            rtmt.sessions.first_frame_timeout_seconds = 0.05
        async with MiddleTierHarness(configure=configure) as h:
            a = await h.connect()
            meta = await a.wait_type("extension.session_metadata")
            sid = next(iter(h.sessions._session_map.values()))
            await a.ws.close()
            await h.settle()
            b = await self._resume(h, meta)
            self.assertEqual(system_items(h.upstream.frames()), [], "nothing to rehydrate")
            await b.send(SESSION_UPDATE)
            await b.wait_type("response.done")
            self.assertEqual(len(h.upstream.frames(kind="response.create")), 1, "the normal greeting")
            h.sessions.end_session(sid)

    async def test_a_silent_guest_is_nudged_once(self):
        async with MiddleTierHarness() as h:
            h.sessions.nudge_after_seconds = 0.1
            sid, meta = await self._conversation(h)
            before = h.sessions.last_activity(sid)
            await self._resume(h, meta)
            h.clock.advance(5)
            await self._wait_frames(h, lambda fs: any(f.get("type") == "response.create" for f in fs))
            frames = h.upstream.frames()
            nudge = system_items(frames)[-1]
            self.assertIn("anything else", nudge["item"]["content"][0]["text"])
            self.assertEqual(frames.index(nudge) + 1, frames.index(h.upstream.frames(kind="response.create")[0]))
            await asyncio.sleep(0.3)
            self.assertEqual(len(h.upstream.frames(kind="response.create")), 1, "only once")
            self.assertEqual(h.sessions.last_activity(sid), before, "the nudge is not guest activity")
            h.sessions.end_session(sid)

    async def test_the_nudge_waits_for_session_updated(self):
        async with MiddleTierHarness() as h:
            h.sessions.nudge_after_seconds = 0.05
            sid, meta = await self._conversation(h)
            h.upstream.ack_session_update = False
            await self._resume(h, meta)
            await asyncio.sleep(0.3)
            self.assertEqual(h.upstream.frames(kind="response.create"), [])
            await h.upstream.push({"type": "session.updated", "session": {"type": "realtime"}})
            await self._wait_frames(h, lambda fs: any(f.get("type") == "response.create" for f in fs))
            h.sessions.end_session(sid)

    async def test_guest_speech_a_transcript_or_a_browser_response_cancels_the_nudge(self):
        async def speech(h, b):
            await h.upstream.push({"type": "input_audio_buffer.speech_started", "audio_start_ms": 0, "item_id": "i2"})

        async def transcript(h, b):
            await h.upstream.push(dict(GUEST_SAID, item_id="i3", transcript="and a donut"))

        async def browser_response(h, b):
            await b.send({"type": "response.create"})

        for cause in (speech, transcript, browser_response):
            with self.subTest(cause=cause.__name__):
                async with MiddleTierHarness() as h:
                    h.sessions.nudge_after_seconds = 0.15
                    sid, meta = await self._conversation(h)
                    b = await self._resume(h, meta)
                    await cause(h, b)
                    await asyncio.sleep(0.4)
                    frames = h.upstream.frames()
                    self.assertEqual(len(system_items(frames)), 1, "only the rehydration, no nudge")
                    self.assertLessEqual(len(h.upstream.frames(kind="response.create")), 1)
                    h.sessions.end_session(sid)

    async def test_the_nudge_is_skipped_while_a_rate_limit_retry_is_pending(self):
        def configure(rtmt):
            async def blocked_sleep(delay):
                await asyncio.Event().wait()
            rtmt._sleep = blocked_sleep

        async with MiddleTierHarness(configure=configure) as h:
            h.sessions.nudge_after_seconds = 0.1
            sid, meta = await self._conversation(h)
            await self._resume(h, meta)
            await h.upstream.push(rate_limited_done("r_rl"))
            await h.settle()
            await asyncio.sleep(0.4)
            self.assertEqual(len(system_items(h.upstream.frames())), 1, "no nudge on top of a pending retry")
            self.assertEqual(h.upstream.frames(kind="response.create"), [])
            h.sessions.end_session(sid)


if __name__ == "__main__":
    unittest.main()
