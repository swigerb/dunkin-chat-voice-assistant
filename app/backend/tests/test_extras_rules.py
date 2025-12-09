import asyncio
import math
import sys
import unittest
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

from order_state import order_state_singleton
from rtmt import ToolResultDirection
from tools import update_order


class ExtrasRuleTests(unittest.TestCase):
    def setUp(self):
        order_state_singleton.sessions = {}

    def _add_item(self, session_id: str, name: str, size: str, qty: int, price: float):
        order_state_singleton.handle_order_update(session_id, "add", name, size, qty, price)

    def test_block_extra_when_only_donut(self):
        session_id = order_state_singleton.create_session()
        self._add_item(session_id, "Glazed Donut", "standard", 1, 1.49)

        result = asyncio.run(
            update_order(
                {
                    "action": "add",
                    "item_name": "Extra Espresso Shot",
                    "size": "standard",
                    "quantity": 1,
                    "price": 1.0,
                },
                session_id,
            )
        )

        self.assertEqual(result.destination, ToolResultDirection.TO_SERVER)
        self.assertIn("extras", result.text.lower())

        summary = order_state_singleton.get_order_summary(session_id)
        self.assertEqual(len(summary.items), 1)
        self.assertEqual(summary.items[0].item, "Glazed Donut")
        self.assertTrue(math.isclose(summary.total, 1.49, rel_tol=1e-9))

    def test_allow_extra_when_latte_present(self):
        session_id = order_state_singleton.create_session()
        self._add_item(session_id, "Caramel Craze Latte", "medium", 1, 4.99)

        result = asyncio.run(
            update_order(
                {
                    "action": "add",
                    "item_name": "Extra Espresso Shot",
                    "size": "standard",
                    "quantity": 1,
                    "price": 1.0,
                },
                session_id,
            )
        )

        self.assertEqual(result.destination, ToolResultDirection.TO_CLIENT)

        summary = order_state_singleton.get_order_summary(session_id)
        self.assertEqual(len(summary.items), 2)
        extras_item = summary.items[1]
        self.assertEqual(extras_item.item, "Extra Espresso Shot")
        self.assertEqual(extras_item.quantity, 1)
        expected_total = (1 * 4.99) + 1.0
        self.assertTrue(math.isclose(summary.total, expected_total, rel_tol=1e-9))

    def test_block_extra_when_only_breakfast_sandwich(self):
        session_id = order_state_singleton.create_session()
        self._add_item(session_id, "Bacon Egg & Cheese on Croissant", "standard", 1, 4.99)

        result = asyncio.run(
            update_order(
                {
                    "action": "add",
                    "item_name": "Whipped Cream",
                    "size": "standard",
                    "quantity": 1,
                    "price": 0.5,
                },
                session_id,
            )
        )

        self.assertEqual(result.destination, ToolResultDirection.TO_SERVER)
        self.assertIn("extras", result.text.lower())

        summary = order_state_singleton.get_order_summary(session_id)
        self.assertEqual(len(summary.items), 1)
        self.assertEqual(summary.items[0].item, "Bacon Egg & Cheese on Croissant")
        self.assertTrue(math.isclose(summary.total, 4.99, rel_tol=1e-9))


if __name__ == "__main__":
    unittest.main()
