"""Tests for happy-hour pricing logic.

Critical: patches is_happy_hour WHERE IT IS USED (order_state module) so tests
are deterministic and never depend on wall-clock time.
"""

import math
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.append(str(Path(__file__).resolve().parents[1]))

from order_state import order_state_singleton


class HappyHourPricingTests(unittest.TestCase):
    def setUp(self):
        order_state_singleton.sessions = {}

    # ── Discounted branch ──

    @patch("order_state.is_happy_hour", return_value=True)
    def test_iced_drink_discounted_during_happy_hour(self, _mock_hh):
        """Cold beverages get 25% off during happy hour."""
        session_id = order_state_singleton.create_session()
        order_state_singleton.handle_order_update(session_id, "add", "Iced Caramel Latte", "medium", 1, 4.99)

        summary = order_state_singleton.get_order_summary(session_id)
        # 4.99 * 0.75 = 3.7425
        expected_total = 4.99 * 0.75
        self.assertTrue(math.isclose(summary.total, expected_total, rel_tol=1e-9))

    @patch("order_state.is_happy_hour", return_value=True)
    def test_cold_brew_discounted_during_happy_hour(self, _mock_hh):
        """Cold brew falls in cold beverages category."""
        session_id = order_state_singleton.create_session()
        order_state_singleton.handle_order_update(session_id, "add", "Original Cold Brew", "large", 2, 3.99)

        summary = order_state_singleton.get_order_summary(session_id)
        expected_total = 2 * 3.99 * 0.75
        self.assertTrue(math.isclose(summary.total, expected_total, rel_tol=1e-9))

    @patch("order_state.is_happy_hour", return_value=True)
    def test_signature_latte_discounted_during_happy_hour(self, _mock_hh):
        """Signature lattes are eligible for happy hour."""
        session_id = order_state_singleton.create_session()
        order_state_singleton.handle_order_update(session_id, "add", "Caramel Craze Latte", "medium", 1, 4.99)

        summary = order_state_singleton.get_order_summary(session_id)
        expected_total = 4.99 * 0.75
        self.assertTrue(math.isclose(summary.total, expected_total, rel_tol=1e-9))

    # ── Non-discounted branch ──

    @patch("order_state.is_happy_hour", return_value=False)
    def test_iced_drink_full_price_outside_happy_hour(self, _mock_hh):
        """Cold beverages are full price outside happy hour."""
        session_id = order_state_singleton.create_session()
        order_state_singleton.handle_order_update(session_id, "add", "Iced Caramel Latte", "medium", 1, 4.99)

        summary = order_state_singleton.get_order_summary(session_id)
        expected_total = 4.99
        self.assertTrue(math.isclose(summary.total, expected_total, rel_tol=1e-9))

    @patch("order_state.is_happy_hour", return_value=True)
    def test_donut_not_discounted_during_happy_hour(self, _mock_hh):
        """Non-eligible categories (donuts) never get happy hour pricing."""
        session_id = order_state_singleton.create_session()
        order_state_singleton.handle_order_update(session_id, "add", "Glazed Donut", "standard", 1, 1.49)

        summary = order_state_singleton.get_order_summary(session_id)
        expected_total = 1.49
        self.assertTrue(math.isclose(summary.total, expected_total, rel_tol=1e-9))

    @patch("order_state.is_happy_hour", return_value=True)
    def test_breakfast_sandwich_not_discounted(self, _mock_hh):
        """Breakfast sandwiches are not eligible."""
        session_id = order_state_singleton.create_session()
        order_state_singleton.handle_order_update(session_id, "add", "Bacon Egg & Cheese Croissant", "standard", 1, 5.49)

        summary = order_state_singleton.get_order_summary(session_id)
        expected_total = 5.49
        self.assertTrue(math.isclose(summary.total, expected_total, rel_tol=1e-9))

    # ── Mixed order ──

    @patch("order_state.is_happy_hour", return_value=True)
    def test_mixed_order_only_eligible_items_discounted(self, _mock_hh):
        """Only eligible items get discounted; others stay full price."""
        session_id = order_state_singleton.create_session()
        order_state_singleton.handle_order_update(session_id, "add", "Iced Caramel Latte", "medium", 1, 4.99)
        order_state_singleton.handle_order_update(session_id, "add", "Glazed Donut", "standard", 2, 1.49)

        summary = order_state_singleton.get_order_summary(session_id)
        expected_total = (4.99 * 0.75) + (2 * 1.49)
        self.assertTrue(math.isclose(summary.total, expected_total, rel_tol=1e-9))

    # ── Tax verification ──

    @patch("order_state.is_happy_hour", return_value=True)
    def test_tax_applies_after_discount(self, _mock_hh):
        """8% tax is applied on the discounted total, not the original."""
        session_id = order_state_singleton.create_session()
        order_state_singleton.handle_order_update(session_id, "add", "Original Cold Brew", "large", 1, 3.99)

        summary = order_state_singleton.get_order_summary(session_id)
        expected_total = 3.99 * 0.75
        expected_tax = expected_total * 0.08
        expected_final = expected_total + expected_tax
        self.assertTrue(math.isclose(summary.tax, expected_tax, rel_tol=1e-9))
        self.assertTrue(math.isclose(summary.finalTotal, expected_final, rel_tol=1e-9))


if __name__ == "__main__":
    unittest.main()
