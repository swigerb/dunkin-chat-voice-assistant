"""Data models for the drive-thru lane simulator."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

__all__ = [
    "DriveThruStatus",
    "DriveThruCar",
    "DriveThruEvent",
    "DriveThruMetrics",
]


class DriveThruStatus(StrEnum):
    ARRIVED = "arrived"
    ORDERING = "ordering"
    PAYING = "paying"
    PICKUP = "pickup"
    COMPLETE = "complete"


@dataclass
class DriveThruCar:
    car_id: str
    status: DriveThruStatus = DriveThruStatus.ARRIVED
    mac_address: str | None = None
    session_id: str | None = None
    crm_customer_id: str | None = None
    crm_summary: dict[str, Any] | None = None
    order_total: float | None = None
    wait_target_seconds: int = 600
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def as_dict(self) -> dict[str, Any]:
        return {
            "carId": self.car_id,
            "status": self.status.value,
            "macAddress": self.mac_address,
            "sessionId": self.session_id,
            "crmCustomerId": self.crm_customer_id,
            "waitSeconds": self.wait_seconds,
            "waitColor": self.wait_color,
            "orderTotal": self.order_total,
            "createdAt": self.created_at.isoformat(),
            "updatedAt": self.updated_at.isoformat(),
            "crmSummary": self.crm_summary or {},
        }

    @property
    def wait_seconds(self) -> int:
        return max(0, int((datetime.now(UTC) - self.created_at).total_seconds()))

    @property
    def wait_color(self) -> str:
        if self.wait_seconds < 120:
            return "green"
        if self.wait_seconds < 300:
            return "yellow"
        return "red"


@dataclass
class DriveThruMetrics:
    cars_in_queue: int
    avg_wait_seconds: float
    orders_per_hour: float
    recognized_percent: float
    last_updated: datetime = field(default_factory=lambda: datetime.now(UTC))

    def as_dict(self) -> dict[str, Any]:
        return {
            "carsInQueue": self.cars_in_queue,
            "avgWaitSeconds": self.avg_wait_seconds,
            "ordersPerHour": self.orders_per_hour,
            "recognizedPercent": self.recognized_percent,
            "timestamp": self.last_updated.isoformat(),
        }


@dataclass
class DriveThruEvent:
    event_type: str
    payload: dict[str, Any]
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))

    def as_dict(self) -> dict[str, Any]:
        data = {"type": self.event_type, **self.payload}
        data["timestamp"] = self.timestamp.isoformat()
        return data
