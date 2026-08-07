"""Routes for the employee drive-thru dashboard."""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from drive_thru import DriveThruDemoFleet, DriveThruSimulator

logger = logging.getLogger(__name__)


async def dashboard_socket(request: web.Request) -> web.StreamResponse:
    simulator: DriveThruSimulator = request.app["drive_thru_simulator"]
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    listener = simulator.subscribe()
    try:
        snapshot = await simulator.snapshot()
        await ws.send_json({"type": "dashboard.snapshot", **snapshot})
        while True:
            event = await listener.get()
            await ws.send_json(event.as_dict())
    except asyncio.CancelledError:  # pragma: no cover - aiohttp lifecycle
        raise
    except Exception as exc:  # pragma: no cover - log unexpected errors
        logger.exception("Dashboard socket error: %s", exc)
    finally:
        simulator.unsubscribe(listener)
        await ws.close()
    return ws


async def spawn_car(request: web.Request) -> web.Response:
    simulator: DriveThruSimulator = request.app["drive_thru_simulator"]
    try:
        payload = await request.json()
    except Exception:  # pragma: no cover - malformed body
        payload = {}
    mac_address = payload.get("macAddress")
    await simulator.spawn_car(mac_address=mac_address)
    return web.json_response({"status": "ok"})


async def reset_lane(request: web.Request) -> web.Response:
    simulator: DriveThruSimulator = request.app["drive_thru_simulator"]
    await simulator.reset()
    return web.json_response({"status": "reset"})


async def demo_status(request: web.Request) -> web.Response:
    demo: DriveThruDemoFleet | None = request.app.get("drive_thru_demo")
    running = bool(demo and demo.is_running)
    return web.json_response({"running": running})


async def start_demo_mode(request: web.Request) -> web.Response:
    demo: DriveThruDemoFleet | None = request.app.get("drive_thru_demo")
    if demo is None:
        raise web.HTTPServiceUnavailable(reason="Demo fleet unavailable")
    await demo.start()
    return web.json_response({"status": "running"})


async def stop_demo_mode(request: web.Request) -> web.Response:
    demo: DriveThruDemoFleet | None = request.app.get("drive_thru_demo")
    if demo is None:
        raise web.HTTPServiceUnavailable(reason="Demo fleet unavailable")
    await demo.stop()
    return web.json_response({"status": "stopped"})


async def complete_car(request: web.Request) -> web.Response:
    """Mark a car as complete and remove it from the queue."""
    simulator: DriveThruSimulator = request.app["drive_thru_simulator"]
    try:
        payload = await request.json()
    except Exception:
        raise web.HTTPBadRequest(reason="Invalid JSON body")
    car_id = payload.get("carId")
    if not car_id:
        raise web.HTTPBadRequest(reason="carId is required")
    await simulator.complete_car(car_id)
    return web.json_response({"status": "completed"})
