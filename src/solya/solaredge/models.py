from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class SiteInfo:
    site_id: int
    name: str
    peak_power_kwp: float | None
    timezone: str
    lat: float | None
    lon: float | None
    last_update_time: datetime | None
    active: bool


@dataclass(frozen=True)
class Overview:
    site_id: int
    production_wh: float | None
    to_grid_wh: float | None
    to_self_consumption_wh: float | None
    consumption_wh: float | None


@dataclass(frozen=True)
class Alert:
    alert_id: int
    type: str | None
    impact: int | None
    status: str
    first_trigger: datetime | None
    close_date: datetime | None
    component_name: str | None
    serial: str | None
    error_codes: list[str]


@dataclass(frozen=True)
class InverterState:
    serial: str
    last_mode: str | None
    last_mode_at: datetime | None


@dataclass(frozen=True)
class Device:
    type: str
    serial: str | None
    model: str | None
    active: bool


@dataclass(frozen=True)
class LifetimeEnergy:
    energy_wh: float | None
    last_update_time: datetime | None
