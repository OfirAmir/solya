"""Month-to-date backfill: daily rows for the days of this month a site has none for.

Runs after a site connects and once at startup. It writes only days strictly before
today (site-local) that have no ``DailyMetric`` row: the evening pull owns today's row
and every row already there.

Credits: at most two successful optional calls (energy and meter export) per site per
calendar month. The month counts as tried once ``/energy`` answered 200 this month and,
for an export-basis site (whose income needs the export), the meter answered 200 or 404
(no meter). A failed call is not credited and is retried at the next start; a retry
after a failed meter call reuses this month's stored ``/energy`` answer instead of
paying for it again. For an export-basis site a failed meter call writes no rows.
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
from solya.solaredge.parse import parse_energy_daily

log = logging.getLogger(__name__)

COMM_STATE = "backfill"
_NONE = object()  # no stored answer (a stored body can itself be None)


def _stored_body(s, site_id: int, path: str, statuses: tuple[int, ...], since: datetime):
    """The latest body stored this month for ``path`` with one of ``statuses``, else
    ``_NONE``."""
    row = s.scalars(
        select(RawResponse)
        .where(
            RawResponse.site_id == site_id,
            RawResponse.endpoint == path,
            RawResponse.status.in_(statuses),
            RawResponse.fetched_at >= since,
        )
        .order_by(RawResponse.fetched_at.desc(), RawResponse.id.desc())
        .limit(1)
    ).first()
    return _NONE if row is None else row.body


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
        needs_export = site.income_basis == "export"
        energy_body = _stored_body(s, site_id, f"/sites/{site_id}/energy", (200,), month_start)
        meter_done = _stored_body(
            s, site_id, f"/sites/{site_id}/meters/telemetry", (200, 404), month_start
        ) is not _NONE
        if energy_body is not _NONE and (meter_done or not needs_export):
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

    if energy_body is not _NONE:  # an earlier try this month got it: no second credit
        production = parse_energy_daily(energy_body, tz)
    else:
        production = await client.get_energy_daily(site_id, first, last, tz, priority="optional")
    try:  # None when the site has no meter (404)
        export = await client.get_meter_export_daily(site_id, first, last, tz, priority="optional")
    except Exception as e:
        log.warning("backfill export for site %s failed: %s", site_id, type(e).__name__)
        if needs_export:  # income needs the export: write nothing, retry at next start
            return 0
        export = None  # production is still worth keeping without export

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
