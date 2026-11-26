"""Backfill: daily rows for the days of a whole-month window a site has none for.

``backfill_month`` (PR 1) fills this month to date. It runs after a site connects and
once at startup. ``backfill_history`` (RAY) also fills the previous calendar month and
the same calendar month one and two years ago. Both use ``backfill_range``, which writes
only days strictly before today (site-local) that have no ``DailyMetric`` row: the
evening pull owns today's row and every row already there.

Credits: per requested range, at most one successful optional ``/energy`` call and, when
export is requested, one meter call, once per calendar month. That is at most two calls
for this month, two for the previous month (its meter only for an export-basis site) and
one for each history month: six per site per month. A range counts as tried this month
once ``/energy`` answered 200 for a request whose ``from`` starts in the range's month and,
for an export-basis site with export requested, the meter answered 200 or 404 (no meter)
for that range. A failed call is not credited and is retried at the next start; a retry
after a failed meter call reuses the stored ``/energy`` answer for that range instead of
paying for it again (it covers only the earlier try's days; the days after it are the
evening pulls', as after any first try). For an export-basis site a failed meter call
writes no rows. A month before the site was installed answers with empty values: no rows, still tried.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from solya.db import SessionFactory, session_scope
from solya.models import DailyMetric, RawResponse, Site
from solya.rules.daily import income
from solya.solaredge.client import SolarEdgeClient
from solya.solaredge.parse import parse_energy_daily

log = logging.getLogger(__name__)

COMM_STATE = "backfill"
HISTORY_YEARS = (1, 2)  # the same calendar month this many years ago
_NONE = object()  # no stored answer (a stored body can itself be None)


def _from_month(params, zone: ZoneInfo) -> tuple[int, int] | None:
    """The site-local (year, month) of a stored request's ``from`` param, else None."""
    raw = params.get("from") if isinstance(params, dict) else None
    if not isinstance(raw, str):
        return None
    try:
        at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if at.tzinfo is None:
        return None
    local = at.astimezone(zone)
    return local.year, local.month


def _stored_body(
    s,
    site_id: int,
    path: str,
    statuses: tuple[int, ...],
    since: datetime,
    month: tuple[int, int],
    zone: ZoneInfo,
):
    """The latest body stored since ``since`` for ``path`` with one of ``statuses`` whose
    request started in ``month`` (site-local), else ``_NONE``."""
    found = s.scalars(
        select(RawResponse)
        .where(
            RawResponse.site_id == site_id,
            RawResponse.endpoint == path,
            RawResponse.status.in_(statuses),
            RawResponse.fetched_at >= since,
        )
        .order_by(RawResponse.fetched_at.desc(), RawResponse.id.desc())
    )
    for row in found:
        if _from_month(row.params, zone) == month:
            return row.body
    return _NONE


def _month_end(first: date) -> date:
    nxt = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    return nxt - timedelta(days=1)


async def backfill_month(
    client: SolarEdgeClient, session_factory: SessionFactory, site_id: int, now: datetime
) -> int:
    """Fill missing days from the 1st of the site-local month to yesterday. Returns the
    number of rows written."""
    with session_scope(session_factory) as s:
        site = s.get(Site, site_id)
        if site is None:
            return 0
        today = now.astimezone(ZoneInfo(site.tz)).date()
    first = today.replace(day=1)
    return await backfill_range(
        client, session_factory, site_id, first, today - timedelta(days=1), now, with_export=True
    )


async def backfill_history(
    client: SolarEdgeClient, session_factory: SessionFactory, site_id: int, now: datetime
) -> int:
    """RAY: fill the previous calendar month (with export for an export-basis site, for
    income) and the same calendar month one and two years ago (production only). The
    caller checks that RAY is enabled. One failing month does not stop the others; it is
    logged by exception type only and retried at the next run."""
    with session_scope(session_factory) as s:
        site = s.get(Site, site_id)
        if site is None or site.status != "active" or not site.monitored:
            return 0
        today = now.astimezone(ZoneInfo(site.tz)).date()
        export_basis = site.income_basis == "export"
    prev = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
    ranges = [(prev, export_basis)] + [
        (today.replace(year=today.year - n, day=1), False) for n in HISTORY_YEARS
    ]
    written = 0
    for first, with_export in ranges:
        try:
            written += await backfill_range(
                client, session_factory, site_id, first, _month_end(first), now,
                with_export=with_export,
            )
        except Exception as e:
            log.warning(
                "history backfill %s for site %s failed: %s",
                first.strftime("%Y-%m"), site_id, type(e).__name__,
            )
    return written


async def backfill_range(
    client: SolarEdgeClient,
    session_factory: SessionFactory,
    site_id: int,
    start: date,
    end: date,
    now: datetime,
    *,
    with_export: bool,
) -> int:
    """Fill missing days from ``start`` to ``end`` (one calendar month at most, clamped to
    before site-local today), once per calendar month. ``with_export`` also asks the meter
    for the export. Returns the number of rows written."""
    with session_scope(session_factory) as s:
        site = s.get(Site, site_id)
        if site is None or site.status != "active" or not site.monitored:
            return 0
        tz = site.tz
        zone = ZoneInfo(tz)
        today = now.astimezone(zone).date()
        first = start
        last = min(end, today - timedelta(days=1))
        if last < first:
            return 0
        this_month = datetime.combine(
            today.replace(day=1), time.min, tzinfo=zone
        ).astimezone(timezone.utc)
        month = (first.year, first.month)
        needs_export = with_export and site.income_basis == "export"
        energy_body = _stored_body(
            s, site_id, f"/sites/{site_id}/energy", (200,), this_month, month, zone
        )
        meter_done = not with_export or _stored_body(
            s, site_id, f"/sites/{site_id}/meters/telemetry", (200, 404), this_month, month, zone
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
        # The stored answer covers only the earlier try's range (to its "yesterday"), so
        # a retry on a later day writes nothing after that. Not a hole: from the day of
        # the first try on, each day's row is the evening pull's (it runs at sunset every
        # day and owns that day's row), exactly as after a first try that succeeded. A
        # fresh /energy call here would only spend this month's credit a second time.
        production = parse_energy_daily(energy_body, tz)
    else:
        production = await client.get_energy_daily(site_id, first, last, tz, priority="optional")
    try:  # None when the site has no meter (404)
        export = (
            await client.get_meter_export_daily(site_id, first, last, tz, priority="optional")
            if with_export
            else None
        )
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
                continue  # never today, never outside the range
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
    log.info("backfill %s wrote %d days for site %s", first.strftime("%Y-%m"), written, site_id)
    return written
