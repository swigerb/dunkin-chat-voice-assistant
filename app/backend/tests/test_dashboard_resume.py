"""Crew dashboard vs order resume: a session is published once, a resumed
session keeps publishing under the same identity (no second car), and an
ended or expired session is cleared from the dashboard."""
import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.append(str(Path(__file__).resolve().parents[1]))

from fake_realtime import FakeClock, MiddleTierHarness  # noqa: E402

from drive_thru.simulator import DriveThruSimulator  # noqa: E402
from order_state import order_state_singleton  # noqa: E402
from rtmt import Tool, ToolResult, ToolResultDirection  # noqa: E402
from session_manager import SessionManager  # noqa: E402

SESSION_UPDATE = {"type": "session.update", "session": {"turn_detection": {"type": "server_vad"}}}


def fake_ws():
    ws = MagicMock()
    ws.close = AsyncMock()
    return ws


def fake_dashboard():
    return MagicMock(assign_session=AsyncMock(), record_order_update=AsyncMock(), release_session=AsyncMock())


async def flush():
    for _ in range(3):
        await asyncio.sleep(0)


class SessionPublishingTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.clock = FakeClock()
        self.sm = SessionManager(clock=self.clock)
        self.sm.idle_timeout_seconds, self.sm.grace_seconds, self.sm.resume_enabled = 300, 120, True
        self.dash = fake_dashboard()
        self.sm.dashboard = self.dash

    def tearDown(self):
        self.sm.dashboard = None
        for sid in list(self.sm._detached) + list(self.sm._attached):
            self.sm.end_session(sid)

    async def test_a_session_is_published_once_when_its_conversation_starts(self):
        sid = self.sm.create_session(fake_ws())
        await flush()
        self.dash.assign_session.assert_not_awaited()      # a provisional socket is not a guest
        self.sm.mark_greeting_sent(sid)
        self.sm.mark_greeting_sent(sid)
        await flush()
        self.dash.assign_session.assert_awaited_once_with(sid)

    async def test_order_updates_only_for_published_sessions(self):
        sid = self.sm.create_session(fake_ws())
        self.sm.publish_order(sid, {"items": []})
        self.sm.mark_greeting_sent(sid)
        self.sm.publish_order(sid, {"finalTotal": 2.5})
        await flush()
        self.dash.record_order_update.assert_awaited_once_with(sid, {"finalTotal": 2.5})

    async def test_ending_a_published_session_clears_it_once(self):
        sid = self.sm.create_session(fake_ws())
        other = self.sm.create_session(fake_ws())
        self.sm.mark_greeting_sent(sid)
        self.sm.end_session(sid, "guest ended the session")
        self.sm.end_session(sid)
        self.sm.end_session(other)                          # never published
        await flush()
        self.dash.release_session.assert_awaited_once_with(sid)
        self.assertNotIn(sid, self.sm._published)

    async def test_a_resume_keeps_the_same_identity(self):
        ws = fake_ws()
        sid = self.sm.create_session(ws)
        rid = self.sm.issue_resume_id(sid)
        self.sm.mark_greeting_sent(sid)
        self.sm.detach_session(ws, sid)
        new_ws = fake_ws()
        provisional = self.sm.create_session(new_ws)
        outcome = self.sm.resume(new_ws, rid)
        self.assertTrue(outcome.accepted)
        self.sm.mark_greeting_sent(sid)
        self.sm.publish_order(sid, {"finalTotal": 1.0})
        await flush()
        self.dash.assign_session.assert_awaited_once_with(sid)
        self.dash.record_order_update.assert_awaited_once_with(sid, {"finalTotal": 1.0})
        self.dash.release_session.assert_not_awaited()     # neither the resumed nor the provisional
        self.assertNotIn(provisional, self.sm._published)

    async def test_an_expired_hold_is_cleared(self):
        ws = fake_ws()
        sid = self.sm.create_session(ws)
        self.sm.issue_resume_id(sid)
        self.sm.mark_greeting_sent(sid)
        self.sm.detach_session(ws, sid)
        await flush()
        self.dash.release_session.assert_not_awaited()     # held, still on the dashboard
        self.clock.advance(120)
        await self.sm.close_idle_sessions()
        await flush()
        self.dash.release_session.assert_awaited_once_with(sid)

    async def test_without_a_dashboard_nothing_is_published(self):
        self.sm.dashboard = None
        sid = self.sm.create_session(fake_ws())
        self.sm.mark_greeting_sent(sid)
        self.sm.publish_order(sid, {})
        self.sm.end_session(sid)
        self.assertEqual(self.sm._published, set())

    async def test_a_failing_dashboard_does_not_break_the_session(self):
        self.dash.assign_session.side_effect = RuntimeError("dashboard down")
        sid = self.sm.create_session(fake_ws())
        with self.assertLogs("coffee-chat", level="WARNING") as logs:
            self.sm.mark_greeting_sent(sid)
            await flush()
        self.assertIn("dashboard down", "\n".join(logs.output))
        self.assertTrue(self.sm.has_sent_greeting(sid))
        self.assertEqual(self.sm._dashboard_tasks, set())


class NoLoopTests(unittest.TestCase):

    def test_publishing_outside_an_event_loop_is_skipped(self):
        sm = SessionManager(clock=FakeClock())
        sm.dashboard = fake_dashboard()
        sid = sm.create_session(fake_ws())
        with self.assertLogs("coffee-chat", level="WARNING"):
            sm.mark_greeting_sent(sid)
        sm.dashboard = None
        sm.end_session(sid)


class SimulatorReleaseTests(unittest.IsolatedAsyncioTestCase):

    async def test_release_takes_the_car_off_the_lane_without_counting_an_order(self):
        sim = DriveThruSimulator(max_cars=4)
        queue = sim.subscribe()
        car = await sim.assign_session("s1")
        orders_before = len(sim._order_timestamps)
        while not queue.empty():
            queue.get_nowait()
        await sim.release_session("s1")
        event = queue.get_nowait().as_dict()
        self.assertEqual((event["type"], event["sessionId"], event["carId"]), ("session.ended", "s1", car.car_id))
        self.assertNotIn(car.car_id, [c["carId"] for c in event["cars"]])
        self.assertIn("metrics", event)
        self.assertNotIn(car, sim._cars)
        self.assertEqual(len(sim._order_timestamps), orders_before)
        await sim.release_session("s1")                   # already gone: no event
        await sim.release_session("unknown")
        self.assertTrue(queue.empty())


class MiddleTierDashboardTests(unittest.IsolatedAsyncioTestCase):
    """The real middle tier publishing to a real DriveThruSimulator."""

    def _configure(self, rtmt):
        self.sim = DriveThruSimulator(max_cars=4)
        rtmt.sessions.dashboard = self.sim
        self.queue = self.sim.subscribe()

        async def update_order(args, session_id):
            order_state_singleton.handle_order_update(session_id, "add", "Iced Coffee", "medium", 1, 3.49)
            summary = order_state_singleton.get_order_summary(session_id).model_dump_json()
            return ToolResult(summary, ToolResultDirection.TO_CLIENT, server_text="added")

        rtmt.tools["update_order"] = Tool(target=update_order, schema={
            "type": "function", "name": "update_order", "parameters": {"type": "object", "properties": {}}})

        async def show_card(args):
            return ToolResult(json.dumps({"items": ["not an order"]}), ToolResultDirection.TO_CLIENT)

        rtmt.tools["show_card"] = Tool(target=show_card, schema={
            "type": "function", "name": "show_card", "parameters": {"type": "object", "properties": {}}})

    def events(self):
        out = []
        while not self.queue.empty():
            out.append(self.queue.get_nowait().as_dict())
        return out

    async def order_via_tool(self, h, browser, call_id, name="update_order"):
        item = {"type": "function_call", "name": name, "call_id": call_id, "arguments": "{}"}
        await h.upstream.push({"type": "conversation.item.created", "previous_item_id": "p", "item": item})
        await h.upstream.push({"type": "response.output_item.done", "item": item})
        await browser.wait_type("extension.middle_tier_tool_response")
        await h.settle()

    async def test_resume_keeps_one_car_and_ending_clears_it(self):
        async with MiddleTierHarness(configure=self._configure) as h:
            a = await h.connect()
            await a.send(SESSION_UPDATE)
            meta = await a.wait_type("extension.session_metadata")
            await a.wait_type("response.done")               # greeted: the conversation started
            await h.settle()
            sid = next(iter(h.sessions._session_map.values()))
            await self.order_via_tool(h, a, "c1")
            first = self.events()
            assigned = [e for e in first if e["type"] == "session.assigned"]
            self.assertEqual(len(assigned), 1)
            car_id = next(c["carId"] for c in assigned[0]["cars"] if c["sessionId"] == sid)
            update = next(e for e in first if e["type"] == "dashboard.order_update")
            self.assertEqual((update["sessionId"], update["carId"]), (sid, car_id))

            await a.ws.close()
            await h.settle()
            self.assertEqual([e["type"] for e in self.events()], [], "a held session stays on the dashboard")

            b = await h.connect()
            await b.send({"type": "extension.resume", "resume_id": meta["resumeId"]})
            await b.wait_type("extension.session_resumed")
            await b.send(SESSION_UPDATE)
            await h.settle()
            await self.order_via_tool(h, b, "c2")
            resumed = self.events()
            self.assertNotIn("session.assigned", [e["type"] for e in resumed], "no second car on resume")
            self.assertNotIn("session.ended", [e["type"] for e in resumed], "the provisional session was never shown")
            update = next(e for e in resumed if e["type"] == "dashboard.order_update")
            self.assertEqual((update["sessionId"], update["carId"]), (sid, car_id))
            self.assertEqual(len(update["orderSummary"]["items"]), 1)
            snapshot = [e for e in resumed if e["type"] == "lane.snapshot"][-1]
            self.assertEqual([c["carId"] for c in snapshot["cars"] if c["sessionId"] == sid], [car_id])

            await b.send({"type": "extension.end_session"})
            await b.wait_closed()
            await h.settle()
            ended = [e for e in self.events() if e["type"] == "session.ended"]
            self.assertEqual(len(ended), 1)
            self.assertEqual((ended[0]["sessionId"], ended[0]["carId"]), (sid, car_id))
            self.assertNotIn(car_id, [c["carId"] for c in ended[0]["cars"]])

    async def test_only_update_order_results_are_published(self):
        async with MiddleTierHarness(configure=self._configure) as h:
            a = await h.connect()
            await a.send(SESSION_UPDATE)
            await a.wait_type("response.done")
            await h.settle()
            sid = next(iter(h.sessions._session_map.values()))
            await self.order_via_tool(h, a, "c9", name="show_card")
            self.assertNotIn("dashboard.order_update", [e["type"] for e in self.events()])
            h.sessions.end_session(sid)

    async def test_an_expired_hold_is_cleared_from_the_dashboard(self):
        async with MiddleTierHarness(configure=self._configure) as h:
            a = await h.connect()
            await a.send(SESSION_UPDATE)
            await a.wait_type("response.done")
            await h.settle()
            sid = next(iter(h.sessions._session_map.values()))
            await a.ws.close()
            await h.settle()
            self.events()
            h.clock.advance(h.sessions.grace_seconds)
            await h.sessions.close_idle_sessions()
            await h.settle()
            ended = [e for e in self.events() if e["type"] == "session.ended"]
            self.assertEqual([e["sessionId"] for e in ended], [sid])

    async def test_an_idle_close_clears_the_session(self):
        async with MiddleTierHarness(configure=self._configure) as h:
            a = await h.connect()
            await a.send(SESSION_UPDATE)
            await a.wait_type("response.done")
            await h.settle()
            sid = next(iter(h.sessions._session_map.values()))
            self.events()
            h.clock.advance(h.sessions.idle_timeout_seconds + 1)
            await h.sessions.close_idle_sessions()
            self.assertEqual((await a.wait_closed())[0], 4000)
            await h.settle()
            self.assertEqual([e["sessionId"] for e in self.events() if e["type"] == "session.ended"], [sid])


if __name__ == "__main__":
    unittest.main()
