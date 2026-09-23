"""update_order results must say plainly whether the order changed.

Live, at reasoning effort "none", gpt-realtime-2.1 folded the extra into the
drink ("Caramel Craze Latte with Extra Espresso Shot"), the extras guard
refused it with a plain-text apology, and the model still told the guest
"All set" (2/5 natural, 2/5 replayed). Rejections are now JSON with
status "rejected" and the corrected calls; successes carry status "ok".
"""
import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.append(str(Path(__file__).resolve().parents[1]))

import tools  # noqa: E402
from app import DUNKIN_SYSTEM_PROMPT  # noqa: E402
from order_state import order_state_singleton  # noqa: E402
from rtmt import (  # noqa: E402
    RTMiddleTier,
    RTToolCall,
    Tool,
    ToolResult,
    ToolResultDirection,
)
from tools import update_order, update_order_tool_schema  # noqa: E402

COMBINED = {"action": "add", "item_name": "Caramel Craze Latte with Extra Espresso Shot",
            "size": "Medium", "quantity": 2, "price": 5.99}


def _run(args, session_id):
    return asyncio.run(update_order(dict(args), session_id))


class UpdateOrderResultShapeTests(unittest.TestCase):
    def setUp(self):
        order_state_singleton.sessions = {}
        self._hh = patch("order_state.is_happy_hour", return_value=False)
        self._hh.start()
        self.addCleanup(self._hh.stop)
        self.sid = order_state_singleton.create_session()

    def test_extra_folded_into_the_drink_is_rejected_with_the_corrected_calls(self):
        result = _run(COMBINED, self.sid)

        self.assertEqual(result.destination, ToolResultDirection.TO_SERVER)
        payload = json.loads(result.model_output())
        self.assertEqual(payload["status"], "rejected")
        self.assertIs(payload["item_added"], False)
        self.assertEqual(payload["reason"], "extra_in_item_name")
        self.assertIn("Nothing was added", payload["instructions"])
        self.assertIn("all set", payload["instructions"].lower())
        self.assertEqual(payload["suggested_calls"], [
            {"action": "add", "item_name": "Caramel Craze Latte", "size": "Medium", "quantity": 2},
            {"action": "add", "item_name": "Extra Espresso Shot", "size": "Standard", "quantity": 2, "price": 1.0},
        ])
        self.assertEqual(order_state_singleton.get_order_summary(self.sid).items, [])

    def test_the_suggested_calls_are_accepted(self):
        payload = json.loads(_run(COMBINED, self.sid).model_output())
        base, extra = payload["suggested_calls"]
        self.assertEqual(json.loads(_run({**base, "price": 4.99}, self.sid).model_output())["status"], "ok")
        self.assertEqual(json.loads(_run(extra, self.sid).model_output())["status"], "ok")
        items = [(i.item, i.quantity) for i in order_state_singleton.get_order_summary(self.sid).items]
        self.assertEqual(items, [("Caramel Craze Latte", 2), ("Extra Espresso Shot", 2)])

    def test_other_combined_wordings_and_extras(self):
        cases = {
            "Iced Signature Latte + whipped cream": ("Iced Signature Latte", "Whipped Cream", 0.5),
            "Cold Brew and a flavor swirl": ("Cold Brew", "Flavor Swirl", 0.75),
            "Caramel Craze Latte with an extra shot": ("Caramel Craze Latte", "Extra Espresso Shot", 1.0),
        }
        for name, (base, extra, price) in cases.items():
            with self.subTest(name=name):
                payload = json.loads(_run({**COMBINED, "item_name": name, "quantity": 1}, self.sid).model_output())
                self.assertEqual(payload["status"], "rejected")
                calls = payload["suggested_calls"]
                self.assertEqual((calls[0]["item_name"], calls[1]["item_name"], calls[1]["price"]),
                                 (base, extra, price))

    def test_extra_on_food_is_rejected_without_suggesting_it(self):
        payload = json.loads(_run({**COMBINED, "item_name": "Glazed Donut with whipped cream"}, self.sid).model_output())
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["reason"], "extra_without_drink")
        self.assertIn("can't add them", payload["message"])
        self.assertNotIn("suggested_calls", payload)

    def test_lone_extra_without_a_drink_is_rejected(self):
        payload = json.loads(_run({"action": "add", "item_name": "Extra Espresso Shot", "size": "Standard",
                                   "quantity": 1, "price": 1.0}, self.sid).model_output())
        self.assertEqual((payload["status"], payload["reason"]), ("rejected", "extra_without_drink"))
        self.assertIs(payload["item_added"], False)
        self.assertNotIn("suggested_calls", payload)

    def test_quantity_limits_are_explicit_rejections(self):
        per_item = json.loads(_run({"action": "add", "item_name": "Glazed Donut", "size": "Standard",
                                    "quantity": tools.MAX_QUANTITY_PER_ITEM + 1, "price": 1.49},
                                   self.sid).model_output())
        self.assertEqual((per_item["status"], per_item["reason"]), ("rejected", "item_quantity_limit"))
        with patch.object(tools, "MAX_QUANTITY_PER_ITEM", 1000):
            total = json.loads(_run({"action": "add", "item_name": "Glazed Donut", "size": "Standard",
                                     "quantity": tools.MAX_TOTAL_ITEMS + 1, "price": 1.49}, self.sid).model_output())
        self.assertEqual((total["status"], total["reason"]), ("rejected", "order_item_limit"))
        self.assertEqual(order_state_singleton.get_order_summary(self.sid).items, [])

    def test_success_tells_the_model_and_the_browser_gets_the_summary(self):
        result = _run({"action": "add", "item_name": "Caramel Craze Latte", "size": "Medium",
                       "quantity": 1, "price": 4.99}, self.sid)
        self.assertEqual(result.destination, ToolResultDirection.TO_CLIENT)
        self.assertEqual(json.loads(result.to_text())["items"][0]["item"], "Caramel Craze Latte")
        ok = json.loads(result.model_output())
        self.assertEqual((ok["status"], ok["action"], ok["item_name"]), ("ok", "add", "Caramel Craze Latte"))
        self.assertEqual(ok["order_items"], ["Medium Caramel Craze Latte"])


class ModelOutputTests(unittest.TestCase):
    def test_model_output_by_direction(self):
        self.assertEqual(ToolResult("x", ToolResultDirection.TO_SERVER).model_output(), "x")
        self.assertEqual(ToolResult("x", ToolResultDirection.TO_CLIENT).model_output(), "")
        self.assertEqual(ToolResult("x", ToolResultDirection.TO_CLIENT, server_text="y").model_output(), "y")

    def test_middle_tier_sends_the_model_output_and_the_browser_the_summary(self):
        async def run():
            rtmt = RTMiddleTier("https://example.invalid", "gpt-realtime-2.1", MagicMock())
            rtmt.tools["update_order"] = Tool(
                target=AsyncMock(return_value=ToolResult("SUMMARY", ToolResultDirection.TO_CLIENT, server_text='{"status":"ok"}')),
                schema=update_order_tool_schema)
            server_ws, client_ws = MagicMock(), MagicMock()
            server_ws.send_json, client_ws.send_json = AsyncMock(), AsyncMock()
            msg = MagicMock()
            msg.data = json.dumps({"type": "response.output_item.done", "item": {
                "type": "function_call", "call_id": "c1", "name": "update_order", "arguments": "{}"}})
            await rtmt._process_message_to_client(msg, client_ws, server_ws, {"c1": RTToolCall("c1", "p")})
            return server_ws.send_json.await_args.args[0], client_ws.send_json.await_args.args[0]

        to_model, to_browser = asyncio.run(run())
        self.assertEqual(to_model["item"]["output"], '{"status":"ok"}')
        self.assertEqual(to_browser["tool_result"], "SUMMARY")

    def test_edge_pipeline_sends_the_model_output_too(self):
        from rtmt_local import RTLocalPipeline, _ConnectionState

        async def run():
            pipeline = RTLocalPipeline(voice_choice="en_US-amy-medium")
            pipeline.tools["update_order"] = Tool(
                target=AsyncMock(return_value=ToolResult("SUMMARY", ToolResultDirection.TO_CLIENT, server_text='{"status":"ok"}')),
                schema=update_order_tool_schema)
            pipeline._llm_chat = AsyncMock(return_value={"choices": [{"message": {"content": "Added."}}]})
            client_ws = MagicMock()
            client_ws.send_json = AsyncMock()
            state = _ConnectionState("s1")
            await pipeline._execute_tool_calls(MagicMock(), client_ws, state, [
                {"id": "t1", "function": {"name": "update_order", "arguments": "{}"}}])
            return state.conversation, client_ws.send_json.await_args.args[0]

        conversation, to_browser = asyncio.run(run())
        tool_msg = next(m for m in conversation if m.get("role") == "tool")
        self.assertEqual(tool_msg["content"], '{"status":"ok"}')
        self.assertEqual(to_browser["tool_result"], "SUMMARY")


class PromptAndSchemaTests(unittest.TestCase):
    def test_prompt_says_a_rejection_added_nothing(self):
        self.assertIn("status 'rejected', nothing was added", DUNKIN_SYSTEM_PROMPT)
        self.assertIn("never say 'all set'", DUNKIN_SYSTEM_PROMPT)
        self.assertIn("suggested_calls", DUNKIN_SYSTEM_PROMPT)

    def test_schema_says_extras_are_separate_items(self):
        description = update_order_tool_schema["parameters"]["properties"]["item_name"]["description"]
        self.assertIn("separate items", description)
        self.assertIn("'rejected'", description)


if __name__ == "__main__":
    unittest.main()
