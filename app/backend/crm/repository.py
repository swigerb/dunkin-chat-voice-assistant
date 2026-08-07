"""Lightweight SQLite repository for CRM sample data."""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterable
from pathlib import Path

from .models import CustomerFavoriteItem, CustomerProfile, CustomerSuggestion

logger = logging.getLogger(__name__)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    id TEXT PRIMARY KEY,
    name TEXT,
    rewards_status TEXT,
    loyalty_score INTEGER,
    loyalty_goal INTEGER,
    curbside_preferred INTEGER,
    favorite_items_json TEXT,
    usual_order_json TEXT,
    suggested_sales_json TEXT,
    suggestions_json TEXT,
    last_visit_iso TEXT
);
CREATE TABLE IF NOT EXISTS devices (
    mac_address TEXT PRIMARY KEY,
    label TEXT,
    customer_id TEXT
);
"""


class CRMRepository:
    """Provides read-only access to CRM customer profiles."""

    def __init__(self, db_path: Path | str, *, seed_path: Path | str | None = None):
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema(seed_path)

    @classmethod
    def from_env(cls, db_path: str | None) -> CRMRepository:
        if db_path is None:
            db_path = Path(__file__).resolve().parent / ".." / "data" / "crm.db"
        resolved = Path(db_path).resolve()
        seed_path = resolved.parent / "crm_seed.json"
        return cls(resolved, seed_path=seed_path)

    def _ensure_schema(self, seed_path: Path | str | None = None) -> None:
        """Create tables and seed from JSON if the database is empty."""
        with self._connect() as conn:
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='customers'"
            ).fetchone()
            if tables is not None:
                return
            logger.info("Initializing CRM database schema at %s", self._db_path)
            conn.executescript(_SCHEMA)
            if seed_path is not None:
                seed_file = Path(seed_path)
                if seed_file.exists():
                    self._seed_from_json(conn, seed_file)

    @staticmethod
    def _seed_from_json(conn: sqlite3.Connection, seed_file: Path) -> None:
        """Populate tables from crm_seed.json."""
        data = json.loads(seed_file.read_text(encoding="utf-8"))
        customers = data.get("customers", [])
        for c in customers:
            conn.execute(
                """INSERT OR REPLACE INTO customers
                   (id, name, rewards_status, loyalty_score, loyalty_goal,
                    curbside_preferred, favorite_items_json, usual_order_json,
                    suggested_sales_json, suggestions_json, last_visit_iso)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    c["id"],
                    c["name"],
                    c["rewards_status"],
                    c["loyalty_score"],
                    c["loyalty_goal"],
                    int(c.get("curbside_preferred", False)),
                    json.dumps(c.get("favorite_items", [])),
                    json.dumps(c.get("usual_order", [])),
                    json.dumps(c.get("suggested_sales", [])),
                    json.dumps(c.get("suggestions", [])),
                    c.get("last_visit_iso", ""),
                ),
            )
            for dev in c.get("bluetooth_devices", []):
                mac = dev["mac"].replace("-", ":").upper()
                conn.execute(
                    "INSERT OR REPLACE INTO devices (mac_address, label, customer_id) VALUES (?,?,?)",
                    (mac, dev.get("label", ""), c["id"]),
                )
        conn.commit()
        logger.info("Seeded CRM database with %d customers", len(customers))

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def list_customers(self) -> list[CustomerProfile]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT c.*, COALESCE(d.devices, '[]') AS devices
                FROM customers c
                LEFT JOIN (
                    SELECT customer_id, json_group_array(mac_address) AS devices
                    FROM devices
                    GROUP BY customer_id
                ) d ON c.id = d.customer_id
                ORDER BY c.name
                """
            ).fetchall()
        return [self._row_to_profile(row) for row in rows]

    def get_customer(self, customer_id: str) -> CustomerProfile | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT c.*, COALESCE(d.devices, '[]') AS devices
                FROM customers c
                LEFT JOIN (
                    SELECT customer_id, json_group_array(mac_address) AS devices
                    FROM devices
                    GROUP BY customer_id
                ) d ON c.id = d.customer_id
                WHERE c.id = ?
                """,
                (customer_id,),
            ).fetchone()
        return self._row_to_profile(row) if row else None

    def get_customer_by_mac(self, mac_address: str) -> CustomerProfile | None:
        normalized = mac_address.replace("-", ":").upper()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT c.*, COALESCE(d.devices, '[]') AS devices
                FROM customers c
                INNER JOIN devices dv ON dv.customer_id = c.id
                LEFT JOIN (
                    SELECT customer_id, json_group_array(mac_address) AS devices
                    FROM devices
                    GROUP BY customer_id
                ) d ON c.id = d.customer_id
                WHERE dv.mac_address = ?
                """,
                (normalized,),
            ).fetchone()
        return self._row_to_profile(row) if row else None

    def _row_to_profile(self, row: sqlite3.Row) -> CustomerProfile:
        favorite_items = self._safe_load_list(row["favorite_items_json"])
        usual_order = self._safe_load_list(row["usual_order_json"])
        suggestions = self._safe_load_list(row["suggestions_json"], default=[])
        suggestion_models = [CustomerSuggestion(**payload) for payload in suggestions]
        return CustomerProfile(
            id=row["id"],
            name=row["name"],
            rewards_status=row["rewards_status"],
            loyalty_score=row["loyalty_score"],
            loyalty_goal=row["loyalty_goal"],
            curbside_preferred=bool(row["curbside_preferred"]),
            bluetooth_devices=self._safe_load_list(row["devices"], default=[]),
            favorite_items=[CustomerFavoriteItem(**payload) for payload in favorite_items],
            usual_order=[CustomerFavoriteItem(**payload) for payload in usual_order],
            suggested_sales=self._safe_load_list(row["suggested_sales_json"], default=[]),
            suggestions=suggestion_models,
            last_visit_iso=row["last_visit_iso"],
        )

    @staticmethod
    def _safe_load_list(raw: str | None, default: Iterable | None = None) -> list:
        if raw in (None, ""):
            return list(default or [])
        try:
            data = json.loads(raw)
            return data if isinstance(data, list) else list(default or [])
        except json.JSONDecodeError:
            logger.warning("Failed to parse CRM JSON payload: %s", raw)
            return list(default or [])
