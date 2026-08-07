"""Background fleet to keep the drive-thru simulator lively for demos."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Sequence

from crm import CRMRepository, CustomerProfile

from .simulator import DriveThruSimulator


class DriveThruDemoFleet:
    """Simulates arriving guests so the dashboard always has motion."""

    def __init__(
        self,
        simulator: DriveThruSimulator,
        crm_repo: CRMRepository | None = None,
        *,
        tick_interval_seconds: tuple[float, float] = (3.0, 12.0),
    ) -> None:
        self._simulator = simulator
        self._crm_repo = crm_repo
        self._tick_interval_seconds = tick_interval_seconds
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        async with self._lock:
            if self.is_running:
                return
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        async with self._lock:
            if self._task is None:
                return
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        customers: Sequence[CustomerProfile] = self._crm_repo.list_customers() if self._crm_repo else []
        try:
            while True:
                sleep_for = random.uniform(*self._tick_interval_seconds)
                await asyncio.sleep(sleep_for)
                await self._tick(customers)
        except asyncio.CancelledError:  # pragma: no cover - cooperative cancellation
            raise

    async def _tick(self, customers: Sequence[CustomerProfile]) -> None:
        snapshot = await self._simulator.snapshot()
        cars = snapshot.get("cars", [])
        active_count = len(cars)
        pickup_count = sum(1 for c in cars if c.get("status") == "pickup")

        # Spawn new cars if there's room (but not if the lane is full of pickup-waiting cars)
        if active_count < self._simulator.max_cars and pickup_count < self._simulator.max_cars:
            profile = random.choice(customers) if customers and random.random() < 0.65 else None
            mac_address = None
            if profile and profile.bluetooth_devices:
                mac_address = random.choice(profile.bluetooth_devices)
            await self._simulator.spawn_car(mac_address=mac_address, profile=profile)
        elif active_count > 0:
            # Advance a car that isn't at pickup yet
            await self._simulator.advance_random_car()
