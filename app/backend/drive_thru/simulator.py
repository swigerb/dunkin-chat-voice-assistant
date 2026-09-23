"""Drive-thru lane simulator that feeds the employee dashboard."""

from __future__ import annotations

import asyncio
import random
import uuid
from collections import deque
from datetime import UTC, datetime, timedelta
from typing import Any

from crm import CustomerProfile

from .models import DriveThruCar, DriveThruEvent, DriveThruMetrics, DriveThruStatus
from .store import InMemorySimulatorStateStore, SimulatorStateStore


class DriveThruSimulator:
    def __init__(
        self,
        *,
        max_cars: int = 4,
        state_store: SimulatorStateStore | None = None,
    ):
        self._max_cars = max(1, max_cars)
        self._state_store = state_store or InMemorySimulatorStateStore()
        self._cars: list[DriveThruCar] = []
        self._cars_by_session: dict[str, DriveThruCar] = {}
        self._listeners: set[asyncio.Queue[DriveThruEvent]] = set()
        self._lock = asyncio.Lock()
        self._metrics = DriveThruMetrics(0, 0.0, 0.0, 0.0)
        self._order_timestamps: deque[datetime] = deque(maxlen=200)
        self._persistence_task: asyncio.Task | None = None
        self._demo_progress_events = {
            DriveThruStatus.ARRIVED: DriveThruStatus.ORDERING,
            DriveThruStatus.ORDERING: DriveThruStatus.PAYING,
            DriveThruStatus.PAYING: DriveThruStatus.PICKUP,
            DriveThruStatus.PICKUP: DriveThruStatus.COMPLETE,
        }

    async def start(self) -> None:
        await self._load_state()
        if not self._cars:
            async with self._lock:
                self._spawn_placeholder_cars()
        if self._persistence_task is None:
            self._persistence_task = asyncio.create_task(self._auto_persist())

    async def stop(self) -> None:
        if self._persistence_task is not None:
            self._persistence_task.cancel()
            try:
                await self._persistence_task
            except asyncio.CancelledError:
                pass
            self._persistence_task = None

    @property
    def max_cars(self) -> int:
        return self._max_cars

    async def _auto_persist(self) -> None:
        while True:
            await asyncio.sleep(5)
            await self._persist_state()

    async def _load_state(self) -> None:
        data = await self._state_store.load()
        cars: list[DriveThruCar] = []
        for entry in data:
            car = DriveThruCar(
                car_id=entry.get("carId", _next_car_id()),
                status=DriveThruStatus(entry.get("status", DriveThruStatus.ARRIVED.value)),
                mac_address=entry.get("macAddress"),
                session_id=entry.get("sessionId"),
                crm_customer_id=entry.get("crmCustomerId"),
                crm_summary=entry.get("crmSummary"),
                order_total=entry.get("orderTotal"),
                created_at=datetime.fromisoformat(entry["createdAt"]) if entry.get("createdAt") else datetime.now(UTC),
                updated_at=datetime.fromisoformat(entry["updatedAt"]) if entry.get("updatedAt") else datetime.now(UTC),
            )
            cars.append(car)
            if car.session_id:
                self._cars_by_session[car.session_id] = car
        self._cars = cars
        self._recompute_metrics()

    async def _persist_state(self) -> None:
        await self._state_store.persist([car.as_dict() for car in self._cars])

    def _spawn_placeholder_cars(self) -> None:
        """Populate the lane with starter cars. Caller MUST hold ``self._lock``."""
        for _ in range(min(self._max_cars, 3)):
            self._spawn_car_locked()

    def _spawn_car_locked(self, mac_address: str | None = None, profile: CustomerProfile | None = None) -> DriveThruCar:
        """Create and register a new car. Caller MUST already hold ``self._lock``."""
        car = DriveThruCar(car_id=_next_car_id(), mac_address=mac_address)
        if profile:
            car.crm_customer_id = profile.id
            car.crm_summary = _profile_snapshot(profile)
        self._cars.append(car)
        self._trim_cars()
        return car

    async def spawn_car(self, mac_address: str | None = None, profile: CustomerProfile | None = None) -> DriveThruCar:
        async with self._lock:
            car = self._spawn_car_locked(mac_address, profile)
            await self._broadcast_snapshot("car.arrived")
            return car

    async def assign_session(self, session_id: str) -> DriveThruCar:
        async with self._lock:
            car = next((c for c in self._cars if c.session_id is None and c.status != DriveThruStatus.COMPLETE), None)
            if car is None:
                car = self._spawn_car_locked()
            car.session_id = session_id
            self._cars_by_session[session_id] = car
            car.status = DriveThruStatus.ORDERING
            car.updated_at = datetime.now(UTC)
            await self._broadcast_snapshot("session.assigned")
            return car

    async def attach_crm_profile(self, session_id: str, profile: CustomerProfile, mac_address: str | None = None) -> None:
        async with self._lock:
            car = self._cars_by_session.get(session_id)
            if car is None:
                return
            car.crm_customer_id = profile.id
            car.crm_summary = _profile_snapshot(profile)
            car.mac_address = mac_address or car.mac_address
            car.updated_at = datetime.now(UTC)
            await self._broadcast_snapshot("car.crm_updated")

    async def record_order_update(self, session_id: str, order_summary: dict[str, Any]) -> None:
        async with self._lock:
            car = self._cars_by_session.get(session_id)
            if car is None:
                return
            car.order_total = order_summary.get("finalTotal")
            car.updated_at = datetime.now(UTC)
            await self._broadcast(
                DriveThruEvent(
                    "dashboard.order_update",
                    {"sessionId": session_id, "carId": car.car_id, "orderSummary": order_summary},
                )
            )
            await self._broadcast_snapshot("lane.snapshot")

    async def complete_session(self, session_id: str) -> None:
        async with self._lock:
            car = self._cars_by_session.pop(session_id, None)
            if car is None:
                return
            car.status = DriveThruStatus.COMPLETE
            car.updated_at = datetime.now(UTC)
            self._order_timestamps.append(datetime.now(UTC))
            await self._broadcast_snapshot("car.complete")

    async def release_session(self, session_id: str) -> None:
        """A voice session ended without the crew completing it (guest ended it,
        idle close, or its resume hold expired): take its car off the lane. Not a
        completed order, so it doesn't count toward orders per hour."""
        async with self._lock:
            car = self._cars_by_session.pop(session_id, None)
            if car is None:
                return
            if car in self._cars:
                self._cars.remove(car)
            await self._broadcast(DriveThruEvent(
                "session.ended", {"sessionId": session_id, "carId": car.car_id, **self._snapshot_payload()}))

    async def complete_car(self, car_id: str) -> None:
        """Mark a car as complete by car_id (used by the crew dashboard)."""
        async with self._lock:
            car = next((c for c in self._cars if c.car_id == car_id), None)
            if car is None:
                return
            car.status = DriveThruStatus.COMPLETE
            car.updated_at = datetime.now(UTC)
            self._order_timestamps.append(datetime.now(UTC))
            if car.session_id:
                self._cars_by_session.pop(car.session_id, None)
            await self._broadcast_snapshot("car.complete")

    async def advance_random_car(self) -> None:
        """Advance a random car to the next status, but never auto-complete.

        Cars stop at PICKUP and stay there until a crew member clicks Done.
        """
        async with self._lock:
            candidates = [
                car for car in self._cars
                if car.status not in (DriveThruStatus.COMPLETE, DriveThruStatus.PICKUP)
            ]
            if not candidates:
                return
            car = random.choice(candidates)
            next_status = self._demo_progress_events.get(car.status, DriveThruStatus.PICKUP)
            car.status = next_status
            car.updated_at = datetime.now(UTC)
            if next_status == DriveThruStatus.PAYING and car.order_total is None:
                car.order_total = round(random.uniform(5.0, 18.0), 2)
            await self._broadcast_snapshot("car.demo_progressed")

    async def reset(self) -> None:
        async with self._lock:
            self._cars.clear()
            self._cars_by_session.clear()
            self._order_timestamps.clear()
            self._spawn_placeholder_cars()
            await self._broadcast_snapshot("lane.reset")

    def subscribe(self) -> asyncio.Queue[DriveThruEvent]:
        queue: asyncio.Queue[DriveThruEvent] = asyncio.Queue(maxsize=64)
        self._listeners.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[DriveThruEvent]) -> None:
        self._listeners.discard(queue)

    async def snapshot(self) -> dict[str, Any]:
        async with self._lock:
            return {
                "cars": [car.as_dict() for car in self._cars if car.status != DriveThruStatus.COMPLETE],
                "metrics": self._metrics.as_dict(),
            }

    async def _broadcast_snapshot(self, event_type: str) -> None:
        await self._broadcast(DriveThruEvent(event_type, self._snapshot_payload()))

    def _snapshot_payload(self) -> dict[str, Any]:
        self._recompute_metrics()
        return {
            "cars": [car.as_dict() for car in self._cars if car.status != DriveThruStatus.COMPLETE],
            "metrics": self._metrics.as_dict(),
        }

    async def _broadcast(self, event: DriveThruEvent) -> None:
        for queue in list(self._listeners):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                _drain_old_events(queue)
                queue.put_nowait(event)

    def _trim_cars(self) -> None:
        if len(self._cars) <= self._max_cars:
            return
        self._cars.sort(key=lambda car: car.created_at)
        while len(self._cars) > self._max_cars:
            removed = self._cars.pop(0)
            if removed.session_id and removed.session_id in self._cars_by_session:
                del self._cars_by_session[removed.session_id]

    def _recompute_metrics(self) -> None:
        active_cars = [car for car in self._cars if car.status != DriveThruStatus.COMPLETE]
        avg_wait = 0.0
        if active_cars:
            avg_wait = sum(car.wait_seconds for car in active_cars) / len(active_cars)
        cutoff = datetime.now(UTC) - timedelta(hours=1)
        while self._order_timestamps and self._order_timestamps[0] < cutoff:
            self._order_timestamps.popleft()
        orders_per_hour = len(self._order_timestamps)
        recognized = sum(1 for car in active_cars if car.crm_customer_id)
        recognized_percent = 0.0
        if active_cars:
            recognized_percent = (recognized / len(active_cars)) * 100
        self._metrics = DriveThruMetrics(
            cars_in_queue=len(active_cars),
            avg_wait_seconds=round(avg_wait, 1),
            orders_per_hour=orders_per_hour,
            recognized_percent=round(recognized_percent, 1),
        )


def _profile_snapshot(profile: CustomerProfile) -> dict[str, Any]:
    return {
        "id": profile.id,
        "name": profile.name,
        "rewardsStatus": profile.rewards_status,
        "loyaltyScore": profile.loyalty_score,
        "loyaltyGoal": profile.loyalty_goal,
        "curbsidePreferred": profile.curbside_preferred,
        "favoriteItems": [item.model_dump() for item in profile.favorite_items],
        "usualOrder": [item.model_dump() for item in profile.usual_order],
        "suggestedSales": profile.suggested_sales,
        "lastVisit": profile.last_visit_iso,
    }


def _next_car_id() -> str:
    suffix = random.randint(100, 999)
    return f"CAR-{suffix}-{uuid.uuid4().hex[:4].upper()}"


def _drain_old_events(queue: asyncio.Queue[DriveThruEvent]) -> None:
    try:
        queue.get_nowait()
    except asyncio.QueueEmpty:
        return
