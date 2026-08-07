"""Tests for per-connection tool-call isolation (concurrency fix) and defensive error paths.

Sprint 1 — validates that:
1. Two concurrent sessions do not see each other's pending tool calls.
2. An unknown tool name logs an error and returns cleanly (no KeyError).
3. A missing call_id in tools_pending logs and returns cleanly.
"""

import asyncio
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.append(str(Path(__file__).resolve().parents[1]))

from rtmt import RTMiddleTier, RTToolCall, Tool, ToolResult, ToolResultDirection


def _make_ws_message(data: dict) -> SimpleNamespace:
    """Create a fake aiohttp WSMessage with .data as a JSON string."""
    return SimpleNamespace(data=json.dumps(data))


def _make_middleware() -> RTMiddleTier:
    """Create a minimal RTMiddleTier for testing."""
    from azure.core.credentials import AzureKeyCredential

    rt = RTMiddleTier(
        endpoint="https://fake.openai.azure.com",
        deployment="gpt-4o-realtime",
        credentials=AzureKeyCredential("fake-key"),
    )
    return rt


async def _register_and_execute_tool_call(
    rt: RTMiddleTier,
    tools_pending: dict,
    call_id: str,
    tool_name: str,
    client_ws: AsyncMock,
    server_ws: AsyncMock,
    session_id: str,
):
    """Simulate the full lifecycle: output_item.added → conversation.item.created → output_item.done."""
    # 1. output_item.added
    msg_added = _make_ws_message({
        "type": "response.output_item.added",
        "item": {"type": "function_call", "call_id": call_id},
    })
    await rt._process_message_to_client(msg_added, client_ws, server_ws, tools_pending)

    # 2. conversation.item.created
    msg_created = _make_ws_message({
        "type": "conversation.item.created",
        "previous_item_id": f"prev_{call_id}",
        "item": {"type": "function_call", "call_id": call_id},
    })
    await rt._process_message_to_client(msg_created, client_ws, server_ws, tools_pending)

    # 3. response.output_item.done
    msg_done = _make_ws_message({
        "type": "response.output_item.done",
        "item": {
            "type": "function_call",
            "call_id": call_id,
            "name": tool_name,
            "arguments": json.dumps({"item": "coffee"}),
        },
    })
    # Patch _session_map to return session_id for the client_ws
    rt._session_map[client_ws] = session_id
    await rt._process_message_to_client(msg_done, client_ws, server_ws, tools_pending)


class TestToolCallConcurrency(unittest.TestCase):
    """Two concurrent sessions must NOT see each other's pending tool calls."""

    def test_concurrent_sessions_isolated(self):
        """Drive two sessions with interleaved tool calls — each only sees its own."""
        rt = _make_middleware()

        # Register a tool
        async def fake_tool(args, session_id):
            return ToolResult("ok", ToolResultDirection.TO_SERVER)

        rt.tools["get_order"] = Tool(target=fake_tool, schema={})

        # Two independent tools_pending dicts (simulating two connections)
        pending_a: dict[str, RTToolCall] = {}
        pending_b: dict[str, RTToolCall] = {}

        ws_a = AsyncMock()
        ws_a.closed = False
        ws_b = AsyncMock()
        ws_b.closed = False
        server_ws_a = AsyncMock()
        server_ws_b = AsyncMock()

        async def run_interleaved():
            # Session A registers call "call_A1"
            msg_a1 = _make_ws_message({
                "type": "conversation.item.created",
                "previous_item_id": "prev_a1",
                "item": {"type": "function_call", "call_id": "call_A1"},
            })
            rt._session_map[ws_a] = "session_a"
            await rt._process_message_to_client(msg_a1, ws_a, server_ws_a, pending_a)

            # Session B registers call "call_B1"
            msg_b1 = _make_ws_message({
                "type": "conversation.item.created",
                "previous_item_id": "prev_b1",
                "item": {"type": "function_call", "call_id": "call_B1"},
            })
            rt._session_map[ws_b] = "session_b"
            await rt._process_message_to_client(msg_b1, ws_b, server_ws_b, pending_b)

            # Assert isolation — each dict only has its own call
            self.assertIn("call_A1", pending_a)
            self.assertNotIn("call_B1", pending_a)
            self.assertIn("call_B1", pending_b)
            self.assertNotIn("call_A1", pending_b)

            # Session A's response.done clears only A's pending
            msg_done_a = _make_ws_message({
                "type": "response.done",
                "response": {"output": [{"type": "function_call", "call_id": "call_A1", "name": "get_order"}]},
            })
            with patch("rtmt.order_state_singleton") as mock_os:
                mock_os.advance_round_trip.return_value = None
                await rt._process_message_to_client(msg_done_a, ws_a, server_ws_a, pending_a)

            # A is cleared, B is untouched
            self.assertEqual(len(pending_a), 0)
            self.assertIn("call_B1", pending_b)

        asyncio.run(run_interleaved())

    def test_shared_state_would_fail(self):
        """Mutation check: if we used a single shared dict, sessions would corrupt each other."""
        rt = _make_middleware()

        async def fake_tool(args, session_id):
            return ToolResult("ok", ToolResultDirection.TO_SERVER)

        rt.tools["get_order"] = Tool(target=fake_tool, schema={})

        # SHARED dict — simulating the old bug
        shared_pending: dict[str, RTToolCall] = {}

        ws_a = AsyncMock()
        ws_a.closed = False
        ws_b = AsyncMock()
        ws_b.closed = False
        server_ws_a = AsyncMock()
        server_ws_b = AsyncMock()

        async def run_shared():
            rt._session_map[ws_a] = "session_a"
            rt._session_map[ws_b] = "session_b"

            # Both sessions add to the SAME dict
            msg_a = _make_ws_message({
                "type": "conversation.item.created",
                "previous_item_id": "prev_a",
                "item": {"type": "function_call", "call_id": "call_A"},
            })
            await rt._process_message_to_client(msg_a, ws_a, server_ws_a, shared_pending)

            msg_b = _make_ws_message({
                "type": "conversation.item.created",
                "previous_item_id": "prev_b",
                "item": {"type": "function_call", "call_id": "call_B"},
            })
            await rt._process_message_to_client(msg_b, ws_b, server_ws_b, shared_pending)

            # Both calls are in the same dict — demonstrating the bug
            self.assertIn("call_A", shared_pending)
            self.assertIn("call_B", shared_pending)

            # Session A clears — this wipes B's call too!
            msg_done = _make_ws_message({
                "type": "response.done",
                "response": {"output": [{"type": "function_call", "call_id": "call_A", "name": "get_order"}]},
            })
            with patch("rtmt.order_state_singleton") as mock_os:
                mock_os.advance_round_trip.return_value = None
                await rt._process_message_to_client(msg_done, ws_a, server_ws_a, shared_pending)

            # Bug: B's call was wiped by A's response.done
            self.assertNotIn("call_B", shared_pending)

        asyncio.run(run_shared())


class TestDefensiveToolLookups(unittest.TestCase):
    """Defensive .get() paths must log and return cleanly — no exceptions."""

    def test_missing_call_id_logs_warning(self):
        """response.output_item.done with unknown call_id logs and returns None."""
        rt = _make_middleware()
        tools_pending: dict[str, RTToolCall] = {}  # empty — no calls registered

        ws = AsyncMock()
        ws.closed = False
        server_ws = AsyncMock()
        rt._session_map[ws] = "test_session"

        msg = _make_ws_message({
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "call_id": "nonexistent_call",
                "name": "get_order",
                "arguments": "{}",
            },
        })

        async def run():
            with self.assertLogs("coffee-chat", level="WARNING") as cm:
                result = await rt._process_message_to_client(msg, ws, server_ws, tools_pending)
            self.assertIsNone(result)
            self.assertTrue(any("nonexistent_call" in m for m in cm.output))

        asyncio.run(run())

    def test_unknown_tool_logs_error(self):
        """response.output_item.done with an unknown tool name logs error and returns None."""
        rt = _make_middleware()
        tools_pending: dict[str, RTToolCall] = {
            "call_123": RTToolCall("call_123", "prev_item"),
        }

        ws = AsyncMock()
        ws.closed = False
        server_ws = AsyncMock()
        rt._session_map[ws] = "test_session"

        msg = _make_ws_message({
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "call_id": "call_123",
                "name": "nonexistent_tool",
                "arguments": "{}",
            },
        })

        async def run():
            with self.assertLogs("coffee-chat", level="ERROR") as cm:
                result = await rt._process_message_to_client(msg, ws, server_ws, tools_pending)
            self.assertIsNone(result)
            self.assertTrue(any("nonexistent_tool" in m for m in cm.output))
            # Server should NOT have been called
            server_ws.send_json.assert_not_called()

        asyncio.run(run())

    def test_successful_tool_call_lifecycle(self):
        """Sanity: a valid tool call still works end-to-end."""
        rt = _make_middleware()

        async def fake_get_order(args, session_id):
            return ToolResult(json.dumps({"items": []}), ToolResultDirection.TO_SERVER)

        rt.tools["get_order"] = Tool(target=fake_get_order, schema={})

        tools_pending: dict[str, RTToolCall] = {}
        ws = AsyncMock()
        ws.closed = False
        server_ws = AsyncMock()

        async def run():
            await _register_and_execute_tool_call(
                rt, tools_pending, "call_good", "get_order", ws, server_ws, "sess1"
            )
            # Server should have received the function_call_output
            server_ws.send_json.assert_called()
            call_args = server_ws.send_json.call_args[0][0]
            self.assertEqual(call_args["type"], "conversation.item.create")
            self.assertEqual(call_args["item"]["call_id"], "call_good")

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
