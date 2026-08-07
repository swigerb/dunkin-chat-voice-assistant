"""Tests for per-item and total order quantity limits.

Patches is_happy_hour to False so pricing assertions are deterministic.
"""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.append(str(Path(__file__).resolve().parents[1]))

from order_state import order_state_singleton
from rtmt import ToolResultDirection
from tools import MAX_QUANTITY_PER_ITEM, MAX_TOTAL_ITEMS, update_order


class QuantityLimitTests(unittest.TestCase):
    def setUp(self):
        order_state_singleton.sessions = {}

    def _add_item(self, session_id: str, name: str, size: str, qty: int, price: float):
        order_state_singleton.handle_order_update(session_id, "add", name, size, qty, price)

    def _run(self, coro):
        return asyncio.run(coro)

    # ── Per-item limit tests ──

    @patch("order_state.is_happy_hour", return_value=False)
    def test_order_exactly_max_quantity_succeeds(self, _mock_hh):
        """Ordering exactly MAX_QUANTITY_PER_ITEM (10) in one shot should succeed."""
        session_id = order_state_singleton.create_session()
        result = self._run(update_order({
            "action": "add",
            "item_name": "Glazed Donut",
            "size": "standard",
            "quantity": MAX_QUANTITY_PER_ITEM,
            "price": 1.49,
        }, session_id))

        self.assertEqual(result.destination, ToolResultDirection.TO_CLIENT)
        summary = order_state_singleton.get_order_summary(session_id)
        self.assertEqual(summary.items[0].quantity, MAX_QUANTITY_PER_ITEM)

    @patch("order_state.is_happy_hour", return_value=False)
    def test_order_one_over_max_quantity_rejected(self, _mock_hh):
        """Ordering MAX_QUANTITY_PER_ITEM + 1 (11) should be rejected."""
        session_id = order_state_singleton.create_session()
        result = self._run(update_order({
            "action": "add",
            "item_name": "Glazed Donut",
            "size": "standard",
            "quantity": MAX_QUANTITY_PER_ITEM + 1,
            "price": 1.49,
        }, session_id))

        self.assertEqual(result.destination, ToolResultDirection.TO_SERVER)
        self.assertIn("up to", result.text.lower())
        summary = order_state_singleton.get_order_summary(session_id)
        self.assertEqual(len(summary.items), 0)

    @patch("order_state.is_happy_hour", return_value=False)
    def test_incremental_add_up_to_max_succeeds(self, _mock_hh):
        """Adding items incrementally up to MAX should succeed at exactly MAX."""
        session_id = order_state_singleton.create_session()
        self._add_item(session_id, "Glazed Donut", "standard", 7, 1.49)

        result = self._run(update_order({
            "action": "add",
            "item_name": "Glazed Donut",
            "size": "standard",
            "quantity": 3,
            "price": 1.49,
        }, session_id))

        self.assertEqual(result.destination, ToolResultDirection.TO_CLIENT)
        summary = order_state_singleton.get_order_summary(session_id)
        self.assertEqual(summary.items[0].quantity, 10)

    @patch("order_state.is_happy_hour", return_value=False)
    def test_incremental_add_over_max_rejected(self, _mock_hh):
        """Adding one more than allowed to an existing item should be rejected."""
        session_id = order_state_singleton.create_session()
        self._add_item(session_id, "Glazed Donut", "standard", MAX_QUANTITY_PER_ITEM, 1.49)

        result = self._run(update_order({
            "action": "add",
            "item_name": "Glazed Donut",
            "size": "standard",
            "quantity": 1,
            "price": 1.49,
        }, session_id))

        self.assertEqual(result.destination, ToolResultDirection.TO_SERVER)
        self.assertIn("already have", result.text.lower())

    @patch("order_state.is_happy_hour", return_value=False)
    def test_different_sizes_have_separate_limits(self, _mock_hh):
        """Same item in different sizes should have independent limits."""
        session_id = order_state_singleton.create_session()
        self._add_item(session_id, "Cold Brew", "medium", MAX_QUANTITY_PER_ITEM, 3.49)

        result = self._run(update_order({
            "action": "add",
            "item_name": "Cold Brew",
            "size": "large",
            "quantity": MAX_QUANTITY_PER_ITEM,
            "price": 3.99,
        }, session_id))

        self.assertEqual(result.destination, ToolResultDirection.TO_CLIENT)
        summary = order_state_singleton.get_order_summary(session_id)
        self.assertEqual(len(summary.items), 2)

    # ── Total order limit tests ──

    @patch("order_state.is_happy_hour", return_value=False)
    def test_total_order_at_max_succeeds(self, _mock_hh):
        """Total items exactly at MAX_TOTAL_ITEMS should succeed."""
        session_id = order_state_singleton.create_session()
        self._add_item(session_id, "Glazed Donut", "standard", MAX_TOTAL_ITEMS - 1, 1.49)

        result = self._run(update_order({
            "action": "add",
            "item_name": "Cold Brew",
            "size": "large",
            "quantity": 1,
            "price": 3.49,
        }, session_id))

        self.assertEqual(result.destination, ToolResultDirection.TO_CLIENT)

    @patch("order_state.is_happy_hour", return_value=False)
    def test_total_order_over_max_rejected(self, _mock_hh):
        """Exceeding MAX_TOTAL_ITEMS should be rejected."""
        session_id = order_state_singleton.create_session()
        self._add_item(session_id, "Glazed Donut", "standard", MAX_TOTAL_ITEMS, 1.49)

        result = self._run(update_order({
            "action": "add",
            "item_name": "Cold Brew",
            "size": "large",
            "quantity": 1,
            "price": 3.49,
        }, session_id))

        self.assertEqual(result.destination, ToolResultDirection.TO_SERVER)
        self.assertIn("tops out", result.text.lower())

    # ── Response format tests ──

    @patch("order_state.is_happy_hour", return_value=False)
    def test_limit_response_ends_with_question(self, _mock_hh):
        """All limit-exceeded responses should end with a question to keep conversation alive."""
        session_id = order_state_singleton.create_session()
        self._add_item(session_id, "Glazed Donut", "standard", MAX_QUANTITY_PER_ITEM, 1.49)

        result = self._run(update_order({
            "action": "add",
            "item_name": "Glazed Donut",
            "size": "standard",
            "quantity": 1,
            "price": 1.49,
        }, session_id))

        self.assertIn("?", result.text)

    @patch("order_state.is_happy_hour", return_value=False)
    def test_remove_bypasses_limits(self, _mock_hh):
        """Remove actions should never be blocked by limits."""
        session_id = order_state_singleton.create_session()
        self._add_item(session_id, "Glazed Donut", "standard", MAX_QUANTITY_PER_ITEM, 1.49)

        result = self._run(update_order({
            "action": "remove",
            "item_name": "Glazed Donut",
            "size": "standard",
            "quantity": 3,
            "price": 1.49,
        }, session_id))

        self.assertEqual(result.destination, ToolResultDirection.TO_CLIENT)
        summary = order_state_singleton.get_order_summary(session_id)
        self.assertEqual(summary.items[0].quantity, 7)


if __name__ == "__main__":
    unittest.main()
