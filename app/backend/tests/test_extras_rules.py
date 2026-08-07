import asyncio
import math
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.append(str(Path(__file__).resolve().parents[1]))

from order_state import order_state_singleton
from rtmt import ToolResultDirection
from tools import update_order


class ExtrasRuleTests(unittest.TestCase):
    def setUp(self):
        order_state_singleton.sessions = {}
        # These assert exact totals, so pin happy hour off. Without this the
        # afternoon discount silently changes the arithmetic and the suite
        # passes or fails depending on the wall clock.
        self._hh = patch("order_state.is_happy_hour", return_value=False)
        self._hh.start()
        self.addCleanup(self._hh.stop)

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

    # ── Additional extras scenarios ──

    def test_allow_extra_with_cold_brew(self):
        """Extras should be allowed when a cold beverage is in the order."""
        session_id = order_state_singleton.create_session()
        self._add_item(session_id, "Original Cold Brew", "large", 1, 3.99)

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

    def test_allow_extra_when_mixed_order_has_latte(self):
        """Extras allowed when order has both a donut AND a latte."""
        session_id = order_state_singleton.create_session()
        self._add_item(session_id, "Glazed Donut", "standard", 1, 1.49)
        self._add_item(session_id, "Caramel Craze Latte", "medium", 1, 4.99)

        result = asyncio.run(
            update_order(
                {
                    "action": "add",
                    "item_name": "Flavor Swirl",
                    "size": "standard",
                    "quantity": 1,
                    "price": 0.75,
                },
                session_id,
            )
        )
        self.assertEqual(result.destination, ToolResultDirection.TO_CLIENT)
        summary = order_state_singleton.get_order_summary(session_id)
        self.assertEqual(len(summary.items), 3)

    def test_block_extra_with_only_multiple_donuts(self):
        """Even multiple donuts should not unlock extras."""
        session_id = order_state_singleton.create_session()
        self._add_item(session_id, "Glazed Donut", "standard", 3, 1.49)
        self._add_item(session_id, "Everything Bagel", "standard", 1, 2.49)

        result = asyncio.run(
            update_order(
                {
                    "action": "add",
                    "item_name": "Extra Shot",
                    "size": "standard",
                    "quantity": 1,
                    "price": 1.0,
                },
                session_id,
            )
        )
        self.assertEqual(result.destination, ToolResultDirection.TO_SERVER)
        self.assertIn("extras", result.text.lower())

    def test_allow_extra_with_multiple_beverages(self):
        """Multiple beverages in the order — extras should still be allowed."""
        session_id = order_state_singleton.create_session()
        self._add_item(session_id, "Caramel Craze Latte", "small", 1, 3.99)
        self._add_item(session_id, "Original Cold Brew", "large", 1, 3.99)

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
        self.assertEqual(result.destination, ToolResultDirection.TO_CLIENT)
        summary = order_state_singleton.get_order_summary(session_id)
        self.assertEqual(len(summary.items), 3)

    def test_block_extra_message_differs_with_blocked_base(self):
        """When order has only blocked-category items, the apology should mention them."""
        session_id = order_state_singleton.create_session()
        self._add_item(session_id, "Glazed Donut", "standard", 1, 1.49)

        result = asyncio.run(
            update_order(
                {
                    "action": "add",
                    "item_name": "Flavor Swirl",
                    "size": "standard",
                    "quantity": 1,
                    "price": 0.75,
                },
                session_id,
            )
        )
        self.assertEqual(result.destination, ToolResultDirection.TO_SERVER)
        self.assertIn("can't add them", result.text.lower())

    def test_non_extra_item_always_allowed(self):
        """Non-extra items should always be addable regardless of order contents."""
        session_id = order_state_singleton.create_session()
        self._add_item(session_id, "Glazed Donut", "standard", 1, 1.49)

        result = asyncio.run(
            update_order(
                {
                    "action": "add",
                    "item_name": "Caramel Craze Latte",
                    "size": "medium",
                    "quantity": 1,
                    "price": 4.99,
                },
                session_id,
            )
        )
        self.assertEqual(result.destination, ToolResultDirection.TO_CLIENT)
        summary = order_state_singleton.get_order_summary(session_id)
        self.assertEqual(len(summary.items), 2)

    def test_remove_action_bypasses_extra_check(self):
        """Removing an extra should work even without a qualifying beverage."""
        session_id = order_state_singleton.create_session()
        self._add_item(session_id, "Caramel Craze Latte", "medium", 1, 4.99)
        self._add_item(session_id, "Extra Espresso Shot", "standard", 1, 1.0)

        # Now remove the latte, leaving only the extra
        order_state_singleton.handle_order_update(session_id, "remove", "Caramel Craze Latte", "medium", 1, 4.99)

        # Removing the extra should still work (remove is not gated)
        result = asyncio.run(
            update_order(
                {
                    "action": "remove",
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
        self.assertEqual(len(summary.items), 0)


if __name__ == "__main__":
    unittest.main()
