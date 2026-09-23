"""Per-connection session lifecycle for the cloud realtime path (rtmt.py).

Tracks which order session each browser socket carries, the guest's last
activity, and closes sessions the guest has abandoned::

    create_session ──► ATTACHED ──(idle > security.idle_timeout_seconds)──► close 4000 ──► ENDED
                          └──(socket closes)───────────────────────────────────────────► ENDED

The idle clock runs from the guest's last *activity*: the guest speaking
(speech_started / a transcript from upstream) or a non-audio control frame
from the browser. Mic audio frames stream constantly, silence included, so they
don't count; neither does anything the middle tier or the model does on its
own (rate-limit retries, greetings).

The edge path (rtmt_local.RTLocalPipeline) has its own connection handling and
does not use this module.
"""

import asyncio
import logging
import time
from collections.abc import Callable

from aiohttp import web

from config_loader import get_config
from order_state import order_state_singleton

logger = logging.getLogger("coffee-chat")

_security_cfg = get_config().get("security", {}) or {}

# Close code for an intentional idle close. Application range (4000-4999) so the
# browser can tell it apart from transport errors (1001/1006/1011) and must not
# auto-reconnect into a live-mic session. Mirrors WS_CLOSE_IDLE_TIMEOUT in
# app/frontend/src/hooks/useRealtime.tsx. An idle close ends the session: the
# order is deleted.
IDLE_CLOSE_CODE = 4000
IDLE_CLOSE_REASON = "idle_timeout"


class SessionManager:
    """Maps browser sockets to order sessions and enforces the idle timeout."""

    def __init__(self, clock: Callable[[], float] | None = None):
        # ws -> session_id for sessions with a live socket.
        self._session_map: dict[web.WebSocketResponse, str] = {}
        # session_id -> the ws it is attached to (reverse of _session_map).
        self._attached: dict[str, web.WebSocketResponse] = {}
        self._sent_greeting: set[str] = set()
        self._last_activity: dict[str, float] = {}
        self._idle_check_task: asyncio.Task | None = None
        self._clock: Callable[[], float] = clock or time.monotonic

        self.idle_timeout_seconds = float(_security_cfg.get("idle_timeout_seconds", 300))
        self.idle_check_interval_seconds = float(_security_cfg.get("idle_check_interval_seconds", 15))

    @property
    def active_session_count(self) -> int:
        return len(self._session_map)

    def now(self) -> float:
        return self._clock()

    def touch_activity(self, session_id: str | None) -> None:
        """Record guest activity; drives the idle clock."""
        if session_id is not None:
            self._last_activity[session_id] = self._clock()

    def last_activity(self, session_id: str) -> float | None:
        return self._last_activity.get(session_id)

    def create_session(self, ws: web.WebSocketResponse) -> str:
        """Create a new order session and map it to the socket."""
        session_id = order_state_singleton.create_session()
        self._session_map[ws] = session_id
        self._attached[session_id] = ws
        self._last_activity[session_id] = self._clock()
        return session_id

    def get_session_id(self, ws: web.WebSocketResponse) -> str | None:
        return self._session_map.get(ws)

    def has_sent_greeting(self, session_id: str | None) -> bool:
        return session_id is not None and session_id in self._sent_greeting

    def mark_greeting_sent(self, session_id: str | None) -> None:
        if session_id is not None:
            self._sent_greeting.add(session_id)

    def end_session(self, session_id: str | None, reason: str = "ended") -> None:
        """Permanently end a session: delete the order and all per-session state."""
        if session_id is None:
            return
        ws = self._attached.pop(session_id, None)
        if ws is not None and self._session_map.get(ws) == session_id:
            del self._session_map[ws]
        order_state_singleton.delete_session(session_id)
        self._sent_greeting.discard(session_id)
        self._last_activity.pop(session_id, None)
        logger.info("Session %s ended (%s)", session_id, reason)

    def cleanup_session(self, ws: web.WebSocketResponse, session_id: str | None, reason: str = "socket closed") -> None:
        """The socket closed: end its session (a no-op if it already ended)."""
        mapped = self._session_map.pop(ws, None)
        if mapped is None or session_id is None or mapped != session_id:
            return
        if self._attached.get(session_id) is ws:
            del self._attached[session_id]
        self.end_session(session_id, reason)

    async def close_idle_sessions(self) -> int:
        """End sessions idle beyond the timeout, then close their sockets with 4000.
        The session is ended before the close so the socket's own close handling
        finds nothing left to clean up."""
        now = self._clock()
        idle_timeout = self.idle_timeout_seconds
        idle_pairs = [(ws, sid) for ws, sid in list(self._session_map.items())
                      if now - self._last_activity.get(sid, now) > idle_timeout]
        for ws, sid in idle_pairs:
            logger.warning("Closing idle session %s (no guest activity for > %.0fs)", sid, idle_timeout)
            self.end_session(sid, IDLE_CLOSE_REASON)
            try:
                await ws.close(code=IDLE_CLOSE_CODE, message=IDLE_CLOSE_REASON.encode())
            except Exception:
                pass
        return len(idle_pairs)

    async def _idle_check_loop(self) -> None:
        while True:
            try:
                await self.close_idle_sessions()
            except Exception as e:
                logger.warning("Idle check error: %s", e)
            await asyncio.sleep(self.idle_check_interval_seconds)

    def start_idle_checker(self) -> None:
        """Start the background idle checker. Safe to call more than once."""
        if self._idle_check_task is None or self._idle_check_task.done():
            self._idle_check_task = asyncio.ensure_future(self._idle_check_loop())

    async def stop_idle_checker(self) -> None:
        task, self._idle_check_task = self._idle_check_task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
