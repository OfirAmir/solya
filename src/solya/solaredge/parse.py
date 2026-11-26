"""Pure parsers for SolarEdge V2 responses."""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from solya.solaredge.models import (
    Alert,
    Device,
    InverterState,
    LifetimeEnergy,
    Overview,
    SiteInfo,
)
from solya.solaredge.units import parse_se_time, to_wh


def _time(v: str | None, tz: str) -> datetime | None:
    return parse_se_time(v, tz) if v else None


def parse_site(j: dict, default_tz: str = "Asia/Jerusalem") -> SiteInfo:
    loc = j.get("location") or {}
    tz = loc.get("timezone") or default_tz
    return SiteInfo(
        site_id=int(j["siteId"]),
        name=j.get("name") or "",
        peak_power_kwp=j.get("peakPower"),
        timezone=tz,
        lat=loc.get("latitude"),
        lon=loc.get("longitude"),
        last_update_time=_time(j.get("lastUpdateTime"), tz),
        active=j.get("activationStatus") == "ACTIVE",
    )


def _block(j: dict, key: str, field: str) -> float | None:
    block = j.get(key) or {}
    return to_wh(block.get(field), block.get("unit"))


def parse_overview(j: dict, expected_site_id: int) -> Overview:
    site_id = j.get("siteId")
    if site_id != expected_site_id:
        raise ValueError(f"overview siteId {site_id!r} != expected {expected_site_id}")
    return Overview(
        site_id=expected_site_id,
        production_wh=_block(j, "production", "total"),
        to_grid_wh=_block(j, "production", "toGrid"),
        to_self_consumption_wh=_block(j, "production", "toSelfConsumption"),
        consumption_wh=_block(j, "consumption", "total"),
    )


def parse_alerts(j: list, tz: str) -> list[Alert]:
    out = []
    for a in j:
        comp = a.get("component") or {}
        codes = list(a.get("errorCodes") or [])
        if not codes and a.get("type"):
            codes = [a["type"]]
        out.append(
            Alert(
                alert_id=int(a["alertId"]),
                type=a.get("type"),
                impact=a.get("impact"),
                status=a.get("status") or "",
                first_trigger=_time(a.get("firstTrigger"), tz),
                close_date=_time(a.get("closeDate"), tz),
                component_name=comp.get("name"),
                serial=comp.get("serialNumber"),
                error_codes=codes,
            )
        )
    return out


def is_fault_mode(mode: str | None) -> bool:
    return bool(mode) and (mode == "FAULT" or mode.startswith("LOCKED"))


def parse_inverter_status(j: dict, tz: str) -> dict[str, InverterState]:
    out = {}
    for serial, data in (j.get("inverters") or {}).items():
        values = ((data or {}).get("inverterMode") or {}).get("values") or []
        last = values[-1] if values else None
        fault = next((v for v in values if is_fault_mode(v.get("value"))), None)
        out[serial] = InverterState(
            serial=serial,
            last_mode=last.get("value") if last else None,
            last_mode_at=_time(last.get("timestamp"), tz) if last else None,
            fault_mode=fault.get("value") if fault else None,
            fault_at=_time(fault.get("timestamp"), tz) if fault else None,
        )
    return out


def parse_devices(j: list) -> list[Device]:
    return [
        Device(
            type=d["type"],
            serial=d.get("serialNumber"),
            model=d.get("model"),
            active=bool(d.get("active")),
        )
        for d in j
    ]


def parse_lifetime(j: dict, tz: str) -> LifetimeEnergy:
    return LifetimeEnergy(
        energy_wh=to_wh(j.get("energy"), j.get("unit") or "WH"),
        last_update_time=_time(j.get("lastUpdateTime"), tz),
    )


def _daily_values(values: list | None, unit: str | None, tz: str) -> dict[date, float]:
    """DAY-resolution values keyed by site-local date (timestamps mark the day start)."""
    zone = ZoneInfo(tz)
    out: dict[date, float] = {}
    for v in values or []:
        wh = to_wh(v.get("value"), unit)
        ts = v.get("timestamp")
        if wh is None or not ts:
            continue
        day = parse_se_time(ts, tz).astimezone(zone).date()
        out[day] = out.get(day, 0.0) + wh
    return out


def parse_energy_daily(j: dict, tz: str) -> dict[date, float]:
    """``GET /sites/{id}/energy?resolution=DAY`` to Wh per site-local date; nulls skipped."""
    return _daily_values(j.get("values"), j.get("unit"), tz)


def parse_meter_export_daily(j: dict, tz: str) -> dict[date, float] | None:
    """``exportEnergy`` summed across meters per site-local date, or None when no meter
    reports ``exportEnergy``."""
    meters = j.get("meters") or {}
    out: dict[date, float] | None = None
    for meter in meters.values():
        block = (meter or {}).get("exportEnergy")
        if not isinstance(block, dict):
            continue
        if out is None:
            out = {}
        for day, wh in _daily_values(block.get("values"), block.get("unit"), tz).items():
            out[day] = out.get(day, 0.0) + wh
    return out
