"""Pure parsers for SolarEdge V2 responses."""
from __future__ import annotations

from datetime import datetime

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


def parse_site(j: dict) -> SiteInfo:
    loc = j.get("location") or {}
    tz = loc.get("timezone") or "UTC"
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


def parse_inverter_status(j: dict, tz: str) -> dict[str, InverterState]:
    out = {}
    for serial, data in (j.get("inverters") or {}).items():
        values = ((data or {}).get("inverterMode") or {}).get("values") or []
        last = values[-1] if values else None
        out[serial] = InverterState(
            serial=serial,
            last_mode=last.get("value") if last else None,
            last_mode_at=_time(last.get("timestamp"), tz) if last else None,
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
