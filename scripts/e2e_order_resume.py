"""Real-browser end-to-end check for order resume and the crew dashboard.

Runs the BUILT guest frontend (app/backend/static) and the BUILT crew dashboard
(app/backend/static/crew) in headless Chromium/Edge against the REAL middle tier
(RTMiddleTier + the real update_order/get_order tools + the real DriveThruSimulator
behind /dashboard) and a scripted GA realtime upstream (tests/fake_realtime.py).
No Azure services, no network beyond 127.0.0.1.

Scenarios
  1. drop 1011 mid-conversation -> auto-reconnect -> same ticket and session; the new
     upstream sees the bootstrap session.update -> one system rehydration item (with
     the order) -> no greeting; the mic restarts without a tap; one nudge
     response.create after resume.nudge_after_seconds of silence (shortened here).
     The crew dashboard keeps ONE car and ONE order card for the session throughout.
  2. same drop, but getUserMedia refuses without a gesture -> "tap to continue"
     fallback; the tap restarts the mic on the same session, no greeting.
  2b. the guest taps while the socket is still reconnecting -> extension.resume is
     still the literal first frame, the queued set_voice + session.update after it.
  3. page reload in the same tab -> ticket restored from sessionStorage's resume id,
     tap to continue, same session, no greeting.
  3b. "Start a new order" -> the session ends (1000 session_ended), the dashboard car
     and order card are cleared, a fresh session starts on a new socket.
  4. idle close 4000 -> no reconnect, id cleared, car cleared from the dashboard;
     a tap starts a fresh session.
  5. strict autoplay policy: the mic still auto-restarts after a drop; after a reload
     it waits for a tap.
  Throughout: the resume id never appears in a URL or in the server logs.

Not part of the default test run (needs a browser). To run:
    .\\.venv\\Scripts\\python.exe -m pip install playwright
    cd app/frontend && npm run build && cd ../employee-dashboard && npm run build && cd ../..
    .\\.venv\\Scripts\\python.exe scripts/e2e_order_resume.py [--channel msedge|chrome|chromium] [--headed]
`--channel chromium` needs `python -m playwright install chromium`; msedge/chrome use the
installed browser. Exit code 0 = all checks passed.
"""

import argparse
import asyncio
import json
import logging
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "app" / "backend"
STATIC = BACKEND / "static"
sys.path[:0] = [str(BACKEND), str(BACKEND / "tests")]

from aiohttp import WSMsgType, web  # noqa: E402
from aiohttp.test_utils import TestServer  # noqa: E402
from azure.core.credentials import AzureKeyCredential  # noqa: E402
from fake_realtime import ScriptedUpstream  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

from dashboard import dashboard_socket  # noqa: E402
from drive_thru import DriveThruSimulator  # noqa: E402
from order_state import order_state_singleton  # noqa: E402
from rtmt import RTMiddleTier, Tool  # noqa: E402
from tools import (  # noqa: E402
    get_order,
    get_order_tool_schema,
    update_order,
    update_order_tool_schema,
)

NUDGE_AFTER_SECONDS = 4.0
COFFEE = {"action": "add", "item_name": "Iced Coffee", "size": "medium", "quantity": 1, "price": 3.29}
DONUT = {"action": "add", "item_name": "Glazed Donut", "size": "standard", "quantity": 1, "price": 1.49}
TICKET = ("Medium Iced Coffee", "Glazed Donut")
RESUME_KEY = "dunkin.resumeId"
REHYDRATION_MARKER = "[Connection restored]"
NUDGE_MARKER = "quiet since their connection came back"
GREETING_MARKER = "greet the guest with"
RESUMED = "Reconnected — your order is still here."
RESUMED_TAP = "Reconnected — your order is still here. Tap the mic to continue."
IDLE = "Your session ended after 5 minutes of inactivity. Tap the mic to start a new order."
NEW_ORDER = "Start a new order"

# Counts getUserMedia calls, lets a scenario make it refuse (as a browser that wants a
# fresh gesture would), and keeps every AudioContext so its state can be reported.
INIT_SCRIPT = """
(() => {
  window.__gum = 0; window.__gumFail = false; window.__ctxs = [];
  const md = navigator.mediaDevices, orig = md.getUserMedia.bind(md);
  md.getUserMedia = async c => {
    window.__gum++;
    if (window.__gumFail) throw new DOMException('needs a user gesture (e2e)', 'NotAllowedError');
    return orig(c);
  };
  const AC = window.AudioContext;
  window.AudioContext = class extends AC { constructor(...a) { super(...a); window.__ctxs.push(this); } };
  // Gesture probe. page.evaluate() runs as a user gesture, so probe from here instead.
  window.__probe = null;
  document.addEventListener('DOMContentLoaded', () => {
    const a = new AC();
    setTimeout(() => { window.__probe = a.state; a.close(); }, 300);
  });
})();
"""


class FakeUpstream(ScriptedUpstream):
    """ScriptedUpstream whose responses carry a spoken transcript (so the page sees
    the greeting finish and starts the mic), plus a way to make the 'model' call a tool."""

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
                output = [{"type": "message", "role": "assistant",
                           "content": [{"type": "output_audio", "transcript": "Welcome to Dunkin!"}]}]
                await ws.send_json({"type": "response.created", "response": {"id": rid}})
                await ws.send_json({"type": "response.done", "response": {
                    "id": rid, "status": "completed", "output": output}})
        return ws

    async def call_tool(self, name: str, args: dict, call_id: str) -> None:
        ws = self.sockets[-1]
        item = {"type": "function_call", "call_id": call_id, "name": name, "arguments": json.dumps(args)}
        await ws.send_json({"type": "response.created", "response": {"id": f"r_{call_id}"}})
        await ws.send_json({"type": "response.output_item.added", "item": item})
        await ws.send_json({"type": "conversation.item.created", "previous_item_id": "prev", "item": item})
        await ws.send_json({"type": "response.output_item.done", "item": item})
        await ws.send_json({"type": "response.done", "response": {
            "id": f"r_{call_id}", "status": "completed", "output": [item]}})


class LogCapture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record):
        try:
            self.lines.append(record.getMessage())
        except Exception:
            self.lines.append(str(record.msg))


class Checks:
    def __init__(self):
        self.rows: list[tuple[str, bool, str]] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.rows.append((name, bool(ok), detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}{(' - ' + detail) if detail else ''}", flush=True)
        return ok

    @property
    def failed(self) -> int:
        return sum(1 for _, ok, _ in self.rows if not ok)


async def until(pred, timeout: float, what: str, interval: float = 0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = pred()
        if asyncio.iscoroutine(value):
            value = await value
        if value:
            return value
        await asyncio.sleep(interval)
    raise TimeoutError(f"timed out after {timeout}s waiting for {what}")


def model_frames(conn: list[dict]) -> list[dict]:
    """Upstream frames minus the constant mic stream."""
    return [e for e in conn if e.get("type") not in ("input_audio_buffer.append", "input_audio_buffer.clear")]


def item_text(event: dict) -> str:
    item = event.get("item") or {}
    return " ".join(c.get("text", "") for c in item.get("content", []) if isinstance(c, dict))


def appends(conn: list[dict]) -> int:
    return sum(1 for e in conn if e.get("type") == "input_audio_buffer.append")


def greeted(conn: list[dict]) -> bool:
    return any(GREETING_MARKER in item_text(f) for f in model_frames(conn))


def only_nudge_creates(conn: list[dict]) -> bool:
    """Every response.create on this upstream directly follows the nudge item."""
    frames = model_frames(conn)
    return all(i > 0 and NUDGE_MARKER in item_text(frames[i - 1])
               for i, f in enumerate(frames) if f.get("type") == "response.create")


class Sockets:
    """Browser-side view of every /realtime websocket the page opens."""

    def __init__(self, page):
        self.all: list[dict] = []
        page.on("websocket", self._on_ws)

    def _on_ws(self, ws):
        if "/realtime" not in ws.url:
            return
        rec = {"url": ws.url, "sent": [], "received": [], "closed": False}
        self.all.append(rec)

        def parse(payload):
            try:
                return json.loads(payload)
            except Exception:
                return {"type": "<binary>"}

        ws.on("framesent", lambda p: rec["sent"].append(parse(p)))
        ws.on("framereceived", lambda p: rec["received"].append(parse(p)))
        ws.on("close", lambda _ws: rec.__setitem__("closed", True))

    def received(self, idx: int, type_: str) -> list[dict]:
        return [m for m in self.all[idx]["received"] if m.get("type") == type_] if idx < len(self.all) else []


class Crew:
    """The crew dashboard page and the simulator behind it."""

    def __init__(self, page, simulator: DriveThruSimulator):
        self.page = page
        self.sim = simulator

    async def order_cards(self, car_id: str | None = None) -> int:
        cards = self.page.get_by_text(re.compile(r"^Car CAR-"))
        if car_id is None:
            return await cards.count()
        return await self.page.get_by_text(f"Car {car_id}", exact=True).count()

    async def lane_has(self, car_id: str) -> bool:
        return await self.page.get_by_text(car_id, exact=True).count() > 0

    def car_for(self, sid: str):
        return self.sim._cars_by_session.get(sid)

    def voice_cars(self) -> int:
        return len(self.sim._cars_by_session)


async def start_servers():
    fake = FakeUpstream()
    fake_server = TestServer(fake.app())
    await fake_server.start_server()

    rtmt = RTMiddleTier(
        endpoint=str(fake_server.make_url("")),
        deployment="gpt-realtime-test",
        credentials=AzureKeyCredential("test-key"),
        voice_choice="marin",
    )
    rtmt.system_message = "You are Dunkin's crew member."
    rtmt.tools["update_order"] = Tool(schema=update_order_tool_schema, target=lambda a, s: update_order(a, s))
    rtmt.tools["get_order"] = Tool(schema=get_order_tool_schema, target=lambda a, s: get_order(a, s))
    sm = rtmt.sessions
    sm.nudge_after_seconds = NUDGE_AFTER_SECONDS
    simulator = DriveThruSimulator(max_cars=4)
    sm.dashboard = simulator

    async def index(_req):
        return web.FileResponse(STATIC / "index.html")

    async def crew_index(_req):
        return web.FileResponse(STATIC / "crew" / "index.html")

    app = web.Application()
    app["drive_thru_simulator"] = simulator
    rtmt.attach_to_app(app, "/realtime")
    app.router.add_get("/dashboard", dashboard_socket)
    app.add_routes([web.get("/", index), web.get("/crew/", crew_index)])
    app.router.add_static("/", path=STATIC)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return fake, fake_server, sm, simulator, runner, f"http://127.0.0.1:{port}/"


async def tap_mic(page, label: str):
    await page.get_by_role("button", name=label, exact=True).click()


async def drop_1011(sm, sid):
    ws = sm._attached[sid]
    await ws.close(code=1011, message=b"e2e transport drop")


async def on_ticket(page, *names: str) -> bool:
    for name in names:
        if await page.get_by_text(name, exact=True).count() == 0:
            return False
    return True


def attached_sid(sm):
    return next(iter(sm._attached), None)


async def open_crew(browser, url, simulator) -> Crew:
    context = await browser.new_context()
    page = await context.new_page()
    await page.goto(url + "crew/")
    await page.get_by_text("Live Orders", exact=True).wait_for(timeout=10000)
    return Crew(page, simulator)


async def scenario_resume(c: Checks, browser, url, fake, sm, simulator, logs: LogCapture):
    print("\n== 1-3b: drop 1011 / gesture fallback / early tap / reload / new order ==", flush=True)
    crew = await open_crew(browser, url, simulator)
    context = await browser.new_context(permissions=["microphone"])
    await context.add_init_script(INIT_SCRIPT)
    page = await context.new_page()
    console: list[str] = []
    page.on("console", lambda m: console.append(f"{m.type}: {m.text}"))
    socks = Sockets(page)
    conns = fake.connections
    base = len(conns)

    await page.goto(url)
    await until(lambda: len(conns) > base and sm.active_session_count >= 1, 10, "first upstream")
    c.check("no dashboard car before the conversation starts", crew.voice_cars() == 0)
    await tap_mic(page, "Start recording")
    await until(lambda: appends(conns[-1]) > 0, 15, "mic audio on upstream #1 (after the greeting)")
    sid = attached_sid(sm)
    first_id = await page.evaluate(f"sessionStorage.getItem('{RESUME_KEY}')")
    c.check("metadata resumeId stored in sessionStorage", bool(first_id) and len(first_id) >= 40)
    c.check("first socket sent no extension.resume (fresh tab)",
            all(m.get("type") != "extension.resume" for m in socks.all[0]["sent"]))
    c.check("greeting on the first upstream", greeted(conns[-1]))

    await fake.call_tool("update_order", COFFEE, "call_coffee")
    await fake.call_tool("update_order", DONUT, "call_donut")
    await until(lambda: on_ticket(page, *TICKET), 10, "two items on the ticket")
    items_before = [i.display for i in order_state_singleton.get_order_summary(sid).items]
    c.check("two items on the server order", items_before == list(TICKET), str(items_before))

    car = await until(lambda: crew.car_for(sid), 5, "dashboard car for the session")
    car_id = car.car_id
    await until(lambda: crew.order_cards(car_id), 5, "dashboard order card")
    await until(lambda: crew.page.get_by_text("2 items", exact=False).count(), 5, "card shows 2 items")
    c.check("dashboard: one car + one order card for the session",
            crew.voice_cars() == 1 and await crew.order_cards(car_id) == 1 and await crew.order_cards() == 1)

    gum_before = await page.evaluate("window.__gum")
    ctx_state_before = await page.evaluate("window.__ctxs.map(x => x.state)")

    # ── 1. transport drop 1011, auto-reconnect, auto mic ─────────────────────
    n_socks = len(socks.all)
    t_drop = time.monotonic()
    await drop_1011(sm, sid)
    await until(lambda: len(socks.all) > n_socks, 15, "browser auto-reconnect")
    resumed = await until(lambda: socks.received(n_socks, "extension.session_resumed"), 10, "session_resumed")
    t_resumed = time.monotonic()
    conn1 = conns[-1]
    c.check("auto-reconnected without a tap", True, f"{t_resumed - t_drop:.1f}s drop->resumed")
    sent1 = socks.all[n_socks]["sent"]
    c.check("literal first frame on the new socket is extension.resume",
            bool(sent1) and sent1[0] == {"type": "extension.resume", "resume_id": first_id},
            f"first={sent1[0].get('type') if sent1 else None}")
    c.check("same order_state session re-attached", attached_sid(sm) == sid)
    c.check("session_resumed carries the order",
            [i["display"] for i in resumed[0]["order_summary"]["items"]] == items_before)
    await until(lambda: appends(conn1) > 0, 10, "mic audio on the resumed upstream")
    c.check("mic auto-restarted (audio reaches the new upstream, no tap)", appends(conn1) > 0,
            f"getUserMedia calls {gum_before}->{await page.evaluate('window.__gum')}")
    try:
        await page.get_by_text(RESUMED, exact=True).first.wait_for(timeout=3000)
        notice_ok = True
    except Exception:
        notice_ok = False
    c.check(f"'{RESUMED}' notice shown", notice_ok)
    c.check("mic button shows 'Stop recording' (listening)",
            await page.get_by_role("button", name="Stop recording", exact=True).count() == 1)
    c.check("same ticket in the DOM", await on_ticket(page, *TICKET))
    rotated = await page.evaluate(f"sessionStorage.getItem('{RESUME_KEY}')")
    c.check("rotated resume_id stored", rotated == resumed[0]["resume_id"] and rotated != first_id)

    frames = model_frames(conn1)
    c.check("upstream #2 frame 1 = bootstrap session.update with tools",
            frames[0].get("type") == "session.update" and bool(frames[0]["session"].get("tools")))
    c.check("upstream #2 frame 2 = ONE system rehydration item containing the order",
            len(frames) > 1 and frames[1].get("type") == "conversation.item.create"
            and frames[1]["item"].get("role") == "system" and REHYDRATION_MARKER in item_text(frames[1])
            and "Iced Coffee" in item_text(frames[1]) and "Glazed Donut" in item_text(frames[1]),
            f"frame2={frames[1].get('type') if len(frames) > 1 else None}")
    c.check("exactly one rehydration item", sum(REHYDRATION_MARKER in item_text(f) for f in frames) == 1)
    await asyncio.sleep(0.5)
    c.check("dashboard after resume: still ONE car (same car id) and ONE order card",
            crew.voice_cars() == 1 and getattr(crew.car_for(sid), "car_id", None) == car_id
            and await crew.order_cards() == 1 and await crew.order_cards(car_id) == 1)

    # ── nudge: one response.create after NUDGE_AFTER_SECONDS of silence ──────
    await until(lambda: any(NUDGE_MARKER in item_text(f) for f in model_frames(conn1)),
                NUDGE_AFTER_SECONDS + 6, "nudge")
    t_nudge = time.monotonic()
    await asyncio.sleep(NUDGE_AFTER_SECONDS + 2)  # would a second nudge come?
    frames = model_frames(conn1)
    nudge_idx = next(i for i, f in enumerate(frames) if NUDGE_MARKER in item_text(f))
    creates = [i for i, f in enumerate(frames) if f.get("type") == "response.create"]
    c.check("no greeting on the resumed upstream (no response.create before the nudge, no greeting item)",
            all(i > nudge_idx for i in creates) and not greeted(conn1))
    c.check("exactly one nudge item + one response.create, after it",
            sum(NUDGE_MARKER in item_text(f) for f in frames) == 1 and creates == [nudge_idx + 1],
            f"nudge after {t_nudge - t_resumed:.1f}s (config {NUDGE_AFTER_SECONDS}s)")
    print(f"  gesture probe: AudioContext states before drop {ctx_state_before}, after resume "
          f"{await page.evaluate('window.__ctxs.map(x => x.state)')}", flush=True)

    # ── 2. drop again; this time getUserMedia wants a gesture ────────────────
    print("  -- 2: gesture fallback --", flush=True)
    await page.evaluate("window.__gumFail = true")
    n_socks = len(socks.all)
    await drop_1011(sm, sid)
    await until(lambda: socks.received(n_socks, "extension.session_resumed"), 15, "second session_resumed")
    conn2 = conns[-1]
    try:
        await page.get_by_text(RESUMED_TAP, exact=True).wait_for(timeout=5000)
        fallback_ok = True
    except Exception:
        fallback_ok = False
    c.check("mic refused -> 'Tap the mic to continue' fallback", fallback_ok)
    c.check("fallback leaves the mic off", await page.get_by_role("button", name="Start recording", exact=True).count() == 1)
    c.check("fallback keeps the ticket", await on_ticket(page, *TICKET))
    await page.evaluate("window.__gumFail = false")
    appends_before_tap = appends(conn2)
    await tap_mic(page, "Start recording")
    await until(lambda: appends(conn2) > appends_before_tap, 3, "mic audio right after the tap (no greeting wait)")
    c.check("tap restarts the mic on the same session", attached_sid(sm) == sid)
    await asyncio.sleep(1.0)
    c.check("no greeting after the tap", not greeted(conn2) and only_nudge_creates(conn2))

    # ── 2b. the guest taps while the socket is still reconnecting ────────────
    print("  -- 2b: tap while reconnecting --", flush=True)
    n_socks = len(socks.all)
    await drop_1011(sm, sid)
    await page.get_by_role("button", name="Start recording", exact=True).wait_for(timeout=3000)
    tapped_early = len(socks.all) == n_socks
    await tap_mic(page, "Start recording")
    await until(lambda: socks.received(n_socks, "extension.session_resumed"), 15, "session_resumed after an early tap")
    conn2b = conns[-1]
    sent2b = [m.get("type") for m in socks.all[n_socks]["sent"] if m.get("type") != "input_audio_buffer.append"]
    c.check("early tap: extension.resume still the literal first frame, queued set_voice + session.update after it",
            tapped_early and sent2b[:3] == ["extension.resume", "extension.set_voice", "session.update"],
            f"tapped before reopen={tapped_early}; first frames={sent2b[:4]}")
    await until(lambda: appends(conn2b) > 0, 5, "mic audio after the early tap")
    await asyncio.sleep(1.0)
    c.check("early tap: same session, ticket kept, no greeting",
            attached_sid(sm) == sid and await on_ticket(page, *TICKET)
            and not greeted(conn2b) and only_nudge_creates(conn2b))

    # ── 3. reload in the same tab ────────────────────────────────────────────
    print("  -- 3: reload in the same tab --", flush=True)
    base_conns = len(conns)
    await page.reload()
    await until(lambda: len(conns) > base_conns and attached_sid(sm) == sid, 15, "resume after reload")
    conn3 = conns[-1]
    fresh_ctx_state = await until(lambda: page.evaluate("window.__probe"), 5, "gesture probe")
    print(f"  gesture probe: an AudioContext created on the reloaded page without a gesture is "
          f"'{fresh_ctx_state}'", flush=True)
    await until(lambda: on_ticket(page, *TICKET), 5, "ticket after reload")
    c.check("reload: ticket restored in the same tab", await on_ticket(page, *TICKET))
    c.check("reload: 'Tap the mic to continue' (no auto mic without the page's refs)",
            await page.get_by_text(RESUMED_TAP, exact=True).count() == 1)
    await tap_mic(page, "Start recording")
    await until(lambda: appends(conn3) > 0, 5, "mic audio after the tap")
    await asyncio.sleep(1.0)
    c.check("reload: same session, no greeting",
            attached_sid(sm) == sid and not greeted(conn3) and only_nudge_creates(conn3))
    c.check("dashboard after drops + reload: still ONE car (same id) and ONE order card",
            crew.voice_cars() == 1 and getattr(crew.car_for(sid), "car_id", None) == car_id
            and await crew.order_cards() == 1)

    # ── 3b. Start a new order ────────────────────────────────────────────────
    print("  -- 3b: start a new order --", flush=True)
    n_socks = len(socks.all)
    await page.get_by_role("button", name=NEW_ORDER).click()
    await until(lambda: sid not in order_state_singleton.sessions, 5, "old order ended on the server")
    await until(lambda: len(socks.all) > n_socks, 10, "fresh socket after New order")
    ended = socks.all[n_socks - 1]
    c.check("new order: old socket sent extension.end_session",
            any(m.get("type") == "extension.end_session" for m in ended["sent"]))
    await until(lambda: crew.voice_cars() == 0, 5, "car released")
    await until(lambda: crew.page.get_by_text("No orders yet.", exact=True).count(), 5, "order card cleared")
    c.check("dashboard after New order: car and order card cleared",
            crew.voice_cars() == 0 and not await crew.lane_has(car_id) and await crew.order_cards() == 0)
    await until(lambda: attached_sid(sm) not in (None, sid), 10, "fresh session attached")
    c.check("new order: fresh session, no extension.resume, empty ticket",
            all(m.get("type") != "extension.resume" for m in socks.all[-1]["sent"])
            and not await on_ticket(page, TICKET[0]))
    base_conns = len(conns)
    await tap_mic(page, "Start recording")
    await until(lambda: greeted(conns[-1]), 10, "greeting on the fresh session")
    new_sid = attached_sid(sm)
    await until(lambda: crew.car_for(new_sid), 5, "dashboard car for the new session")
    c.check("new order: greeted, and exactly one new dashboard car", crew.voice_cars() == 1
            and crew.car_for(new_sid).car_id != car_id, f"new sid != old: {new_sid != sid}")

    all_ids = {m.get("resume_id") for s in socks.all for m in s["sent"] + s["received"] if m.get("resume_id")}
    all_ids |= {m.get("resumeId") for s in socks.all for m in s["received"] if m.get("resumeId")}
    all_ids.discard(None)
    c.check("resume ids never in a websocket URL", not any(i in s["url"] for s in socks.all for i in all_ids),
            f"{len(all_ids)} ids")
    c.check("resume ids never in the server logs", not any(i in line for line in logs.lines for i in all_ids),
            f"{len(logs.lines)} log lines scanned")
    errors = [m for m in console if m.startswith("error")]
    c.check("no browser console errors", not errors, "; ".join(errors[:3]))
    await context.close()
    await crew.page.context.close()


async def scenario_idle(c: Checks, browser, url, fake, sm, simulator):
    print("\n== 4: idle close 4000 ==", flush=True)
    crew = await open_crew(browser, url, simulator)
    context = await browser.new_context(permissions=["microphone"])
    await context.add_init_script(INIT_SCRIPT)
    page = await context.new_page()
    socks = Sockets(page)
    conns = fake.connections
    base = len(conns)
    await page.goto(url)
    await until(lambda: len(conns) > base and len(socks.all) == 1, 10, "first upstream")
    await tap_mic(page, "Start recording")
    await until(lambda: appends(conns[-1]) > 0, 15, "mic audio")
    sid = attached_sid(sm)
    await fake.call_tool("update_order", COFFEE, "call_idle_coffee")
    await until(lambda: on_ticket(page, TICKET[0]), 5, "ticket")
    car = await until(lambda: crew.car_for(sid), 5, "dashboard car")
    await until(lambda: crew.order_cards(car.car_id), 5, "dashboard order card")

    real_clock = sm._clock
    sm._clock = lambda: real_clock() + sm.idle_timeout_seconds + 5
    try:
        await sm.close_idle_sessions()
    finally:
        sm._clock = real_clock
    await page.get_by_text(IDLE, exact=True).wait_for(timeout=5000)
    await asyncio.sleep(3.0)
    c.check("idle: no auto-reconnect", len(socks.all) == 1, f"{len(socks.all)} sockets")
    c.check("idle: resume id cleared", await page.evaluate(f"sessionStorage.getItem('{RESUME_KEY}')") is None)
    c.check("idle: order gone on the server", sid not in order_state_singleton.sessions)
    c.check("idle: dashboard car and order card cleared",
            crew.car_for(sid) is None and not await crew.lane_has(car.car_id) and await crew.order_cards() == 0)

    base = len(conns)
    await tap_mic(page, "Start recording")
    await until(lambda: len(socks.all) == 2 and len(conns) > base, 10, "fresh socket after the tap")
    await until(lambda: greeted(conns[-1]), 10, "greeting on the fresh session")
    c.check("idle: tap opens a fresh session (no extension.resume sent)",
            all(m.get("type") != "extension.resume" for m in socks.all[1]["sent"]))
    c.check("idle: new session id", attached_sid(sm) not in (None, sid))
    c.check("idle: ticket empty", not await on_ticket(page, TICKET[0]))
    await context.close()
    await crew.page.context.close()


async def scenario_strict_autoplay(c: Checks, p, launch: dict, url, fake, sm):
    """Chromium with the autoplay policy that demands a user activation per document:
    shows whether the mic can come back after a drop with no new gesture."""
    print("\n== 5: strict autoplay policy (--autoplay-policy=document-user-activation-required) ==", flush=True)
    strict = {**launch, "args": [*launch["args"], "--autoplay-policy=document-user-activation-required"]}
    browser = await p.chromium.launch(**strict)
    try:
        context = await browser.new_context(permissions=["microphone"])
        await context.add_init_script(INIT_SCRIPT)
        page = await context.new_page()
        socks = Sockets(page)
        conns = fake.connections
        base = len(conns)
        await page.goto(url)
        await until(lambda: len(conns) > base and len(socks.all) == 1, 10, "first upstream")
        no_gesture = await until(lambda: page.evaluate("window.__probe"), 5, "gesture probe")
        c.check("strict: an AudioContext made without a gesture is suspended", no_gesture == "suspended", no_gesture)
        await tap_mic(page, "Start recording")
        await until(lambda: appends(conns[-1]) > 0, 15, "mic audio after the tap")
        sid = attached_sid(sm)
        await fake.call_tool("update_order", COFFEE, "call_strict_coffee")
        await until(lambda: on_ticket(page, TICKET[0]), 5, "ticket")

        n = len(socks.all)
        await drop_1011(sm, sid)
        await until(lambda: socks.received(n, "extension.session_resumed"), 15, "session_resumed")
        conn = conns[-1]
        try:
            await until(lambda: appends(conn) > 0, 5, "mic audio after the resume")
            auto = True
        except TimeoutError:
            auto = False
        states = await page.evaluate("window.__ctxs.map(x => x.state)")
        c.check("strict: mic auto-restarts after a drop with NO new gesture (AudioContext kept running)",
                auto, f"contexts {states}")

        base_conns = len(conns)
        await page.reload()
        await until(lambda: len(conns) > base_conns and attached_sid(sm) == sid, 15, "resume after reload")
        conn = conns[-1]
        await until(lambda: on_ticket(page, TICKET[0]), 5, "ticket after reload")
        await asyncio.sleep(1.0)
        c.check("strict: after a reload the mic stays off until a tap", appends(conn) == 0
                and await page.get_by_role("button", name="Start recording", exact=True).count() == 1)
        await tap_mic(page, "Start recording")
        await until(lambda: appends(conn) > 0, 5, "mic audio after the tap")
        c.check("strict: the tap brings the mic back on the same session", attached_sid(sm) == sid)
        await context.close()
    finally:
        await browser.close()


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--channel", default="msedge")
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()
    for built, how in ((STATIC / "index.html", "cd app/frontend && npm run build"),
                       (STATIC / "crew" / "index.html", "cd app/employee-dashboard && npm run build")):
        if not built.exists():
            print(f"Build first: {how}")
            return 2

    logs = LogCapture()
    logging.getLogger().addHandler(logs)
    logging.getLogger().setLevel(logging.DEBUG)
    for noisy in ("asyncio", "aiohttp.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    fake, fake_server, sm, simulator, runner, url = await start_servers()
    c = Checks()
    started = time.monotonic()
    try:
        async with async_playwright() as p:
            launch = {"headless": not args.headed,
                      "args": ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"]}
            if args.channel != "chromium":
                launch["channel"] = args.channel
            browser = await p.chromium.launch(**launch)
            print(f"browser: {args.channel} {browser.version} headless={not args.headed}; app {url}", flush=True)
            try:
                for run in (lambda: scenario_resume(c, browser, url, fake, sm, simulator, logs),
                            lambda: scenario_idle(c, browser, url, fake, sm, simulator),
                            lambda: scenario_strict_autoplay(c, p, launch, url, fake, sm)):
                    try:
                        await run()
                    except Exception as exc:
                        c.check("scenario completed", False, f"{type(exc).__name__}: {str(exc)[:300]}")
            finally:
                await browser.close()
    finally:
        await runner.cleanup()
        await fake_server.close()
    print(f"\n{len(c.rows) - c.failed}/{len(c.rows)} checks passed in {time.monotonic() - started:.1f}s", flush=True)
    return 1 if c.failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
