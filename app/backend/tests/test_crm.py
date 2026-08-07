"""Tests for the CRM repository, seeding idempotency, and dashboard routes."""

import json
import sqlite3
import sys
from pathlib import Path

# Add backend source to path (same pattern as test_app.py)
sys.path.append(str(Path(__file__).resolve().parents[1]))

import pytest
from aiohttp import web

from crm import CRMRepository, CustomerFavoriteItem
from drive_thru import DriveThruSimulator

SCHEMA = """
CREATE TABLE customers (
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
CREATE TABLE devices (
    mac_address TEXT PRIMARY KEY,
    label TEXT,
    customer_id TEXT
);
"""


def seed_tmp_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "crm.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA)
        conn.execute(
            """
            INSERT INTO customers (
                id, name, rewards_status, loyalty_score, loyalty_goal,
                curbside_preferred, favorite_items_json, usual_order_json,
                suggested_sales_json, suggestions_json, last_visit_iso
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "cust-1",
                "Taylor Finch",
                "Gold",
                900,
                1200,
                1,
                json.dumps([{"item": "Iced Latte", "size": "Large", "quantity": 1}]),
                json.dumps([{"item": "Iced Latte", "size": "Large", "quantity": 1}]),
                json.dumps(["Add cold foam"]),
                json.dumps([]),
                "2026-03-09T12:00:00-05:00",
            ),
        )
        conn.execute(
            "INSERT INTO devices (mac_address, label, customer_id) VALUES (?, ?, ?)",
            ("AA:BB:CC:DD:EE:FF", "SUV", "cust-1"),
        )
    return db_path


# --- CRM Repository Tests ---


def test_repo_returns_customer_by_mac(tmp_path):
    db_path = seed_tmp_db(tmp_path)
    repo = CRMRepository(db_path)

    profile = repo.get_customer_by_mac("aa-bb-cc-dd-ee-ff")

    assert profile is not None
    assert profile.name == "Taylor Finch"
    assert profile.bluetooth_devices == ["AA:BB:CC:DD:EE:FF"]
    assert profile.favorite_items[0].item == "Iced Latte"


def test_repo_lists_customers(tmp_path):
    db_path = seed_tmp_db(tmp_path)
    repo = CRMRepository(db_path)

    profiles = repo.list_customers()

    assert len(profiles) == 1
    assert profiles[0].loyalty_score == 900


def test_repo_get_customer_by_id(tmp_path):
    db_path = seed_tmp_db(tmp_path)
    repo = CRMRepository(db_path)

    profile = repo.get_customer("cust-1")
    assert profile is not None
    assert profile.name == "Taylor Finch"
    assert profile.rewards_status == "Gold"


def test_repo_get_customer_not_found(tmp_path):
    db_path = seed_tmp_db(tmp_path)
    repo = CRMRepository(db_path)

    profile = repo.get_customer("nonexistent")
    assert profile is None


def test_repo_get_customer_by_mac_not_found(tmp_path):
    db_path = seed_tmp_db(tmp_path)
    repo = CRMRepository(db_path)

    profile = repo.get_customer_by_mac("00:00:00:00:00:00")
    assert profile is None


def test_repo_favorite_items_parsed_correctly(tmp_path):
    db_path = seed_tmp_db(tmp_path)
    repo = CRMRepository(db_path)

    profile = repo.get_customer("cust-1")
    assert profile is not None
    assert len(profile.favorite_items) == 1
    assert isinstance(profile.favorite_items[0], CustomerFavoriteItem)
    assert profile.favorite_items[0].size == "Large"


def test_repo_suggested_sales(tmp_path):
    db_path = seed_tmp_db(tmp_path)
    repo = CRMRepository(db_path)

    profile = repo.get_customer("cust-1")
    assert profile is not None
    assert profile.suggested_sales == ["Add cold foam"]


# --- Auto Seeding Tests ---


def test_auto_seeds_from_json(tmp_path):
    db_path = tmp_path / "data" / "crm.db"
    seed_file = tmp_path / "data" / "crm_seed.json"
    seed_file.parent.mkdir(parents=True, exist_ok=True)
    seed_file.write_text(json.dumps({
        "customers": [{
            "id": "cust-auto",
            "name": "Auto Seed",
            "rewards_status": "Silver",
            "loyalty_score": 500,
            "loyalty_goal": 1000,
            "curbside_preferred": False,
            "bluetooth_devices": [{"mac": "11-22-33-44-55-66", "label": "Truck"}],
            "favorite_items": [],
            "usual_order": [],
            "suggested_sales": [],
            "suggestions": [],
            "last_visit_iso": "2026-04-01T10:00:00-05:00",
        }]
    }))

    repo = CRMRepository(db_path, seed_path=seed_file)

    profile = repo.get_customer_by_mac("11:22:33:44:55:66")
    assert profile is not None
    assert profile.name == "Auto Seed"


def test_auto_seed_skipped_when_tables_exist(tmp_path):
    db_path = seed_tmp_db(tmp_path)
    seed_file = tmp_path / "crm_seed.json"
    seed_file.write_text(json.dumps({"customers": []}))

    repo = CRMRepository(db_path, seed_path=seed_file)

    profiles = repo.list_customers()
    assert len(profiles) == 1  # original data preserved, not overwritten


def test_seed_idempotent_no_duplicates(tmp_path):
    """Run the seeding process twice and confirm no duplicate records."""
    db_path = tmp_path / "idempotent" / "crm.db"
    seed_file = tmp_path / "idempotent" / "crm_seed.json"
    seed_file.parent.mkdir(parents=True, exist_ok=True)
    seed_file.write_text(json.dumps({
        "customers": [
            {
                "id": "cust-repeat",
                "name": "Repeat Customer",
                "rewards_status": "Gold",
                "loyalty_score": 700,
                "loyalty_goal": 1000,
                "curbside_preferred": True,
                "bluetooth_devices": [{"mac": "AA-11-BB-22-CC-33", "label": "Tesla"}],
                "favorite_items": [{"item": "Espresso", "size": "Small", "quantity": 1}],
                "usual_order": [],
                "suggested_sales": ["Try a muffin"],
                "suggestions": [],
                "last_visit_iso": "2026-05-01T08:00:00-05:00",
            }
        ]
    }))

    # First seed — creates schema + inserts data
    repo1 = CRMRepository(db_path, seed_path=seed_file)
    customers1 = repo1.list_customers()
    assert len(customers1) == 1

    # Second seed — should be a no-op because tables already exist
    repo2 = CRMRepository(db_path, seed_path=seed_file)
    customers2 = repo2.list_customers()
    assert len(customers2) == 1, (
        f"Expected 1 customer after idempotent re-seed, got {len(customers2)}"
    )


# --- Dashboard Route Tests ---


def _make_app_with_simulator() -> web.Application:
    """Build a minimal app with dashboard routes for testing."""
    from dashboard import (
        complete_car,
        dashboard_socket,
        demo_status,
        reset_lane,
        spawn_car,
        start_demo_mode,
        stop_demo_mode,
    )

    app = web.Application()
    simulator = DriveThruSimulator(max_cars=4)
    app["drive_thru_simulator"] = simulator
    app["drive_thru_demo"] = None  # No demo fleet in tests

    app.router.add_get("/dashboard", dashboard_socket)
    app.router.add_post("/simulator/spawn", spawn_car)
    app.router.add_post("/simulator/reset", reset_lane)
    app.router.add_post("/simulator/complete", complete_car)
    app.router.add_get("/simulator/demo", demo_status)
    app.router.add_post("/simulator/demo/start", start_demo_mode)
    app.router.add_post("/simulator/demo/stop", stop_demo_mode)
    return app


@pytest.fixture
def dashboard_app():
    app = _make_app_with_simulator()
    # Don't call simulator.start() - that creates background tasks.
    # Just pre-populate the lane for testing.
    return app


@pytest.mark.asyncio
async def test_spawn_car_returns_ok(dashboard_app):
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(dashboard_app)) as client:
        resp = await client.post("/simulator/spawn", json={})
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "ok"


@pytest.mark.asyncio
async def test_reset_lane_clears_queue(dashboard_app):
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(dashboard_app)) as client:
        await client.post("/simulator/spawn", json={})
        await client.post("/simulator/spawn", json={})
        resp = await client.post("/simulator/reset", json={})
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "reset"


@pytest.mark.asyncio
async def test_complete_car_requires_car_id(dashboard_app):
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(dashboard_app)) as client:
        resp = await client.post("/simulator/complete", json={})
        assert resp.status == 400


@pytest.mark.asyncio
async def test_complete_car_returns_completed(dashboard_app):
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(dashboard_app)) as client:
        simulator: DriveThruSimulator = dashboard_app["drive_thru_simulator"]
        # Spawn a car without starting the full simulator lifecycle
        await simulator.spawn_car()
        snapshot = await simulator.snapshot()
        cars = snapshot["cars"]
        assert len(cars) > 0
        car_id = cars[0]["carId"]
        resp = await client.post("/simulator/complete", json={"carId": car_id})
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "completed"


@pytest.mark.asyncio
async def test_demo_status_returns_not_running(dashboard_app):
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(dashboard_app)) as client:
        resp = await client.get("/simulator/demo")
        assert resp.status == 200
        data = await resp.json()
        assert data["running"] is False


@pytest.mark.asyncio
async def test_start_demo_without_fleet_returns_503(dashboard_app):
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(dashboard_app)) as client:
        resp = await client.post("/simulator/demo/start")
        assert resp.status == 503


@pytest.mark.asyncio
async def test_stop_demo_without_fleet_returns_503(dashboard_app):
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(dashboard_app)) as client:
        resp = await client.post("/simulator/demo/stop")
        assert resp.status == 503
