"""Seed the demo CRM SQLite database used by the drive-thru experience."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    rewards_status TEXT NOT NULL,
    loyalty_score INTEGER NOT NULL DEFAULT 0,
    loyalty_goal INTEGER NOT NULL DEFAULT 1000,
    curbside_preferred INTEGER NOT NULL DEFAULT 0,
    favorite_items_json TEXT,
    usual_order_json TEXT,
    suggested_sales_json TEXT,
    suggestions_json TEXT,
    last_visit_iso TEXT
);

CREATE TABLE IF NOT EXISTS devices (
    mac_address TEXT PRIMARY KEY,
    label TEXT,
    customer_id TEXT NOT NULL,
    FOREIGN KEY(customer_id) REFERENCES customers(id)
);
"""


def normalize_mac(mac: str) -> str:
    cleaned = mac.replace("-", "").replace(":", "").upper()
    if len(cleaned) != 12:
        return mac.upper()
    return ":".join(cleaned[i : i + 2] for i in range(0, len(cleaned), 2))


def load_seed(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    customers = payload.get("customers", [])
    if not isinstance(customers, list):
        raise ValueError("Seed file must contain a 'customers' array")
    return customers


def seed_database(db_path: Path, customers: list[dict[str, Any]], reset: bool = False) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        if reset:
            conn.execute("DROP TABLE IF EXISTS devices")
            conn.execute("DROP TABLE IF EXISTS customers")
        conn.executescript(SCHEMA)

        conn.execute("DELETE FROM devices")
        conn.execute("DELETE FROM customers")

        for customer in customers:
            conn.execute(
                """
                INSERT INTO customers (
                    id, name, rewards_status, loyalty_score, loyalty_goal,
                    curbside_preferred, favorite_items_json, usual_order_json,
                    suggested_sales_json, suggestions_json, last_visit_iso
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    customer["id"],
                    customer["name"],
                    customer.get("rewards_status", "Member"),
                    customer.get("loyalty_score", 0),
                    customer.get("loyalty_goal", 1000),
                    1 if customer.get("curbside_preferred") else 0,
                    json.dumps(customer.get("favorite_items", [])),
                    json.dumps(customer.get("usual_order", [])),
                    json.dumps(customer.get("suggested_sales", [])),
                    json.dumps(customer.get("suggestions", [])),
                    customer.get("last_visit_iso"),
                ),
            )

            for device in customer.get("bluetooth_devices", []):
                mac = normalize_mac(device.get("mac", ""))
                label = device.get("label")
                if not mac:
                    continue
                conn.execute(
                    """
                    INSERT OR REPLACE INTO devices (mac_address, label, customer_id)
                    VALUES (?, ?, ?)
                    """,
                    (mac, label, customer["id"]),
                )
        conn.commit()


def parse_args() -> argparse.Namespace:
    default_seed = Path("app/backend/data/crm_seed.json").resolve()
    default_db = Path("app/backend/data/crm.db").resolve()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=Path, default=default_seed, help="Path to the seed JSON file")
    parser.add_argument("--db", type=Path, default=default_db, help="Path to the SQLite database output")
    parser.add_argument("--reset", action="store_true", help="Drop and recreate tables before inserting rows")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    customers = load_seed(args.seed)
    seed_database(args.db, customers, reset=args.reset)
    print(f"Seeded {len(customers)} customers into {args.db}")


if __name__ == "__main__":
    main()
