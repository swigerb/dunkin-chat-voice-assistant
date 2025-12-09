import math
import sys
import unittest
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

from order_state import order_state_singleton, SessionIdentifiers


class OrderStateTests(unittest.TestCase):
    def setUp(self):
        order_state_singleton.sessions = {}

    def test_create_session_initializes_empty_summary(self):
        session_id = order_state_singleton.create_session()
        summary = order_state_singleton.get_order_summary(session_id)

        self.assertEqual(len(summary.items), 0)
        self.assertEqual(summary.total, 0)
        self.assertEqual(summary.tax, 0)
        self.assertEqual(summary.finalTotal, 0)

    def test_handle_order_update_adds_and_updates_totals(self):
        session_id = order_state_singleton.create_session()
        order_state_singleton.handle_order_update(session_id, "add", "Caramel Craze Latte", "medium", 2, 4.99)
        order_state_singleton.handle_order_update(session_id, "add", "Glazed Donut", "standard", 1, 1.49)

        summary = order_state_singleton.get_order_summary(session_id)

        self.assertEqual(len(summary.items), 2)
        self.assertEqual(summary.items[0].quantity, 2)

        expected_total = (2 * 4.99) + 1.49
        expected_tax = expected_total * 0.08
        expected_final = expected_total + expected_tax

        self.assertTrue(math.isclose(summary.total, expected_total, rel_tol=1e-9))
        self.assertTrue(math.isclose(summary.tax, expected_tax, rel_tol=1e-9))
        self.assertTrue(math.isclose(summary.finalTotal, expected_final, rel_tol=1e-9))

    def test_formatted_display_labels_handle_special_sizes(self):
        session_id = order_state_singleton.create_session()
        order_state_singleton.handle_order_update(session_id, "add", "Original Cold Brew", "pot", 1, 15.0)

        summary = order_state_singleton.get_order_summary(session_id)

        self.assertEqual(summary.items[0].display, "Pot of Original Cold Brew")

    def test_n_a_size_is_hidden_in_display(self):
        session_id = order_state_singleton.create_session()
        order_state_singleton.handle_order_update(session_id, "add", "Glazed Donut", "n/a", 1, 1.49)

        summary = order_state_singleton.get_order_summary(session_id)

        self.assertEqual(summary.items[0].display, "Glazed Donut")

    def test_session_identifiers_increment_with_round_trips(self):
        session_id = order_state_singleton.create_session()
        identifiers = order_state_singleton.get_session_identifiers(session_id)

        self.assertIsInstance(identifiers, SessionIdentifiers)
        self.assertEqual(identifiers.round_trip_index, 0)
        self.assertTrue(identifiers.round_trip_token.endswith("-0000"))

        first_round = order_state_singleton.advance_round_trip(session_id)
        self.assertEqual(first_round.round_trip_index, 1)
        self.assertTrue(first_round.round_trip_token.endswith("-0001"))
        self.assertEqual(first_round.session_token, identifiers.session_token)

    def test_session_tokens_are_unique_per_session(self):
        session_one = order_state_singleton.create_session()
        session_two = order_state_singleton.create_session()

        identifiers_one = order_state_singleton.get_session_identifiers(session_one)
        identifiers_two = order_state_singleton.get_session_identifiers(session_two)

        self.assertNotEqual(identifiers_one.session_token, identifiers_two.session_token)


if __name__ == "__main__":
    unittest.main()
