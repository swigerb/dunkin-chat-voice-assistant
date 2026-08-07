import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from config_loader import get_config
from models import OrderItem, OrderSummary

__all__ = ["OrderState", "SessionIdentifiers", "order_state_singleton", "is_happy_hour"]

logger = logging.getLogger("order_state")

_config = get_config()
_biz_cfg = _config.get("business_rules", {})

# Store timezone — defaults to US Eastern (Dunkin HQ).
_STORE_TZ = ZoneInfo(os.environ.get("STORE_TIMEZONE", "America/New_York"))

# Happy-hour eligible categories (iced drinks, cold brew, espresso-based).
_HAPPY_HOUR_CATEGORIES: set[str] = set(_biz_cfg.get("happy_hour_categories", ["cold beverages", "signature lattes"]))


def is_happy_hour() -> bool:
    """Check if the current time is within the happy hour window (store-local time)."""
    now = datetime.now(_STORE_TZ)
    start = _biz_cfg.get("happy_hour_start", 14)
    end = _biz_cfg.get("happy_hour_end", 17)
    return start <= now.hour < end


def _infer_happy_hour_category(item_name: str) -> str:
    """Lightweight category inference for happy-hour eligibility."""
    normalized = item_name.lower()
    if "latte" in normalized:
        return "signature lattes"
    if any(kw in normalized for kw in ("cold brew", "refresher", "iced", "cold")):
        return "cold beverages"
    return ""


@dataclass
class SessionIdentifiers:
    session_token: str
    round_trip_index: int
    round_trip_token: str


class OrderState:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance.sessions = {}
        return cls._instance

    def _update_summary(self, session_id: str):
        session = self.sessions[session_id]
        order_items = session["order_state"]
        happy_hour = is_happy_hour()
        discount = _biz_cfg.get("happy_hour_discount", 0.75)
        total = 0.0
        for item in order_items:
            item_total = item.price * item.quantity
            if happy_hour and _infer_happy_hour_category(item.item) in _HAPPY_HOUR_CATEGORIES:
                item_total *= discount
            total += item_total
        tax = total * _biz_cfg.get("tax_rate", 0.08)
        finalTotal = total + tax
        session["order_summary"] = OrderSummary(items=order_items, total=total, tax=tax, finalTotal=finalTotal)
        logger.info(f"Order Summary Updated for session {session_id}: {session['order_summary']}")

    def create_session(self) -> str:
        session_id = str(uuid.uuid4())
        session_token = str(uuid.uuid4())
        self.sessions[session_id] = {
            "order_state": [],
            "order_summary": OrderSummary(items=[], total=0.0, tax=0.0, finalTotal=0.0),
            "session_token": session_token,
            "round_trip_index": 0,
            "round_trip_token": self._format_round_trip_token(session_token, 0)
        }
        self._update_summary(session_id)
        logger.info(f"Session created with ID {session_id}")
        return session_id

    def delete_session(self, session_id: str) -> None:
        if session_id in self.sessions:
            del self.sessions[session_id]
            logger.info("Session deleted with ID %s", session_id)

    def _format_round_trip_token(self, session_token: str, round_trip_index: int) -> str:
        return f"{session_token}-{round_trip_index:04d}"

    def handle_order_update(self, session_id: str, action: str, item_name: str, size: str, quantity: int, price: float):
        session = self.sessions[session_id]
        order_state = session["order_state"]

        normalized_size = (size or "").strip().lower()
        if normalized_size in {"", "standard", "n/a", "na", "none", "n.a."}:
            formatted_size = ""
        elif normalized_size == "kannchen":
            formatted_size = "Kannchen of "
        elif normalized_size == "pot":
            formatted_size = "Pot of "
        else:
            formatted_size = f"{normalized_size.capitalize()} "

        display = f"{formatted_size}{item_name}".strip()

        existing_item_index = next((index for index, order_item in enumerate(order_state) if order_item.item == item_name and order_item.size == size), -1)

        if action == "add":
            if existing_item_index != -1:
                order_state[existing_item_index].quantity += quantity
                logger.info(f"Updated quantity for {display} in session {session_id}")
            else:
                order_state.append(OrderItem(item=item_name, size=size, quantity=quantity, price=price, display=display))
                logger.info(f"Added {display} to session {session_id}")
        elif action == "remove":
            if existing_item_index != -1:
                if order_state[existing_item_index].quantity > quantity:
                    order_state[existing_item_index].quantity -= quantity
                    logger.info(f"Decreased quantity for {display} in session {session_id}")
                else:
                    order_state.pop(existing_item_index)
                    logger.info(f"Removed {display} from session {session_id}")

        self._update_summary(session_id)

    def get_order_summary(self, session_id: str) -> OrderSummary:
        order_summary = self.sessions[session_id]["order_summary"]
        logger.info(f"Order Summary Retrieved for session {session_id}: {order_summary}")
        return order_summary

    def get_session_identifiers(self, session_id: str) -> SessionIdentifiers:
        session = self.sessions[session_id]
        return SessionIdentifiers(
            session_token=session["session_token"],
            round_trip_index=session["round_trip_index"],
            round_trip_token=session["round_trip_token"],
        )

    def advance_round_trip(self, session_id: str) -> SessionIdentifiers:
        session = self.sessions[session_id]
        session["round_trip_index"] += 1
        session["round_trip_token"] = self._format_round_trip_token(
            session["session_token"], session["round_trip_index"]
        )
        logger.info(
            "Round trip %s recorded for session %s", session["round_trip_index"], session_id
        )
        return self.get_session_identifiers(session_id)

# Create a singleton instance of OrderState
order_state_singleton = OrderState()