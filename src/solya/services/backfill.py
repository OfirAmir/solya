"""Month-to-date backfill: daily rows for the days of this month a site has none for.

Runs after a site connects and once at startup. It writes only days strictly before
today (site-local) that have no ``DailyMetric`` row: the evening pull owns today's row
and every row already there. It costs at most two optional calls (energy and meter
export) per site per calendar month; a stored ``/energy`` response this month means
the month was already tried.
"""
from __future__ import annotations

import logging
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from solya.db import SessionFactory, session_scope
from solya.models import DailyMetric, RawResponse, Site
from solya.rules.daily import income
from solya.solaredge.client import SolarEdgeClient

log = logging.getLogger(__name__)

COMM_STATE = "backfill"


async def backfill_month(
    client: SolarEdgeClient, session_factory: SessionFactory, site_id: int, now: datetime
) -> int:
    """Fill missing days from the 1st of the site-local month to yesterday. Returns the
    number of rows written."""
    with session_scope(session_factory) as s:
        site = s.get(Site, site_id)
        if site is None or site.status != "active" or not site.monitored:
            return 0
        tz = site.tz
        zone = ZoneInfo(tz)
        today = now.astimezone(zone).date()
        first = today.replace(day=1)
        last = today - timedelta(days=1)
        if last < first:
            return 0
        month_start = datetime.combine(first, time.min, tzinfo=zone).astimezone(timezone.utc)
        path = f"/sites/{site_id}/energy"
        tried = s.scalar(
            select(RawResponse.id)
            .where(
                RawResponse.site_id == site_id,
                RawResponse.endpoint == path,
                RawResponse.fetched_at >= month_start,
            )
            .limit(1)
        )
        if tried is not None:
            return 0
        have = set(
            s.scalars(
                select(DailyMetric.local_date).where(
                    DailyMetric.site_id == site_id,
                    DailyMetric.local_date >= first,
                    DailyMetric.local_date <= last,
                )
            )
        )
        if len(have) == (last - first).days + 1:
            return 0

    production = await client.get_energy_daily(site_id, first, last, tz, priority="optional")
    try:
        export = await client.get_meter_export_daily(site_id, first, last, tz, priority="optional")
    except Exception as e:  # production is still worth keeping without export
        log.warning("backfill export for site %s failed: %s", site_id, type(e).__name__)
        export = None

    written = 0
    with session_scope(session_factory) as s:
        site = s.get(Site, site_id)
        if site is None:
            return 0
        for day, wh in sorted(production.items()):
            if not first <= day <= last:
                continue  # never today, never outside this month
            if s.get(DailyMetric, (site_id, day)) is not None:
                continue  # never touch an existing row
            export_wh = export.get(day) if export is not None else None
            s.add(
                DailyMetric(
                    site_id=site_id,
                    local_date=day,
                    production_wh=wh,
                    export_wh=export_wh,
                    self_consumption_wh=None,
                    income_ils=income(wh, export_wh, site.tariff_ils_per_kwh, site.income_basis),
                    ray=None,
                    comm_state=COMM_STATE,
                    lifetime_wh=None,
                    as_of=now,
                    computed_at=now,
                )
            )
            written += 1
    log.info("backfill wrote %d days for site %s", written, site_id)
    return written
