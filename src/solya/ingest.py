"""One site pull: calls the SolarEdge client endpoints and tolerates partial failure."""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Literal, TypeVar
from zoneinfo import ZoneInfo

from solya.clock import Clock
from solya.db import SessionFactory, session_scope
from solya.models import Site
from solya.solaredge.budget import Priority
from solya.solaredge.client import SolarEdgeClient
from solya.solaredge.errors import AuthExpired
from solya.solaredge.models import Alert, InverterState, LifetimeEnergy, Overview, SiteInfo

log = logging.getLogger(__name__)

DEVICES_MAX_AGE = timedelta(days=7)
T = TypeVar("T")


@dataclass
class PullBundle:
    site: SiteInfo | None
    overview: Overview | None
    alerts: list[Alert] | None
    inverters: dict[str, InverterState] | None
    lifetime: LifetimeEnergy | None
    errors: dict[str, str]
    pulled_at: datetime
    devices_refreshed: bool = field(default=False)


async def pull_site(
    client: SolarEdgeClient,
    session_factory: SessionFactory,
    clock: Clock,
    site_id: int,
    kind: Literal["evening", "midday"],
    local_date: date,
) -> PullBundle:
    pulled_at = clock.now()
    priority: Priority = "optional" if kind == "midday" else "essential"
    errors: dict[str, str] = {}

    with session_scope(session_factory) as s:
        row = s.get(Site, site_id)
        if row is None:
            raise LookupError(f"unknown site {site_id}")
        tz = row.tz
        devices_fetched_at = row.devices_fetched_at

    async def call(name: str, fn: Callable[[], Awaitable[T]]) -> T | None:
        try:
            return await fn()
        except AuthExpired:
            raise
        except Exception as exc:  # noqa: BLE001 - an odd shape must not lose the other calls
            errors[name] = f"{type(exc).__name__}: {exc}"
            log.warning("site %s %s call failed: %s", site_id, name, type(exc).__name__)
            return None

    site = await call("site", lambda: client.get_site(site_id, priority=priority))
    if site is not None:
        tz = site.timezone or tz
        with session_scope(session_factory) as s:
            row = s.get(Site, site_id)
            row.last_update_time = site.last_update_time
            row.tz = site.timezone
            row.lat = site.lat
            row.lon = site.lon

    overview = await call(
        "overview", lambda: client.get_overview(site_id, local_date, tz, priority=priority)
    )
    alerts = await call("alerts", lambda: client.get_open_alerts(site_id, tz, priority=priority))

    midnight = datetime.combine(local_date, time.min, tzinfo=ZoneInfo(tz))
    inverters = await call(
        "inverters",
        lambda: client.get_inverter_status(
            site_id, tz, midnight, pulled_at, priority=priority
        ),
    )

    lifetime = None
    if kind == "evening":
        lifetime = await call(
            "lifetime", lambda: client.get_lifetime(site_id, tz, priority=priority)
        )

    refreshed = False
    if devices_fetched_at is None or pulled_at - devices_fetched_at > DEVICES_MAX_AGE:
        devices = await call("devices", lambda: client.get_devices(site_id, priority=priority))
        if devices is not None:
            serials = [d.serial for d in devices if d.type == "INVERTER" and d.active and d.serial]
            with session_scope(session_factory) as s:
                row = s.get(Site, site_id)
                row.expected_inverters = serials
                row.devices_fetched_at = pulled_at
            refreshed = True

    return PullBundle(
        site=site,
        overview=overview,
        alerts=alerts,
        inverters=inverters,
        lifetime=lifetime,
        errors=errors,
        pulled_at=pulled_at,
        devices_refreshed=refreshed,
    )
