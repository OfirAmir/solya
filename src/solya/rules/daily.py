"""Daily per-site figures and state."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal
from zoneinfo import ZoneInfo

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from solya.config import Settings
from solya.ingest import PullBundle
from solya.models import DailyMetric, Site
from solya.rules.comms import is_stale
from solya.rules.ray import RayCalculator

log = logging.getLogger(__name__)

SiteState = Literal["ok", "comm_loss", "no_data", "reauth_needed"]

SINGLE_SITE_LABEL = "המערכת"
_CENT = Decimal("0.01")


@dataclass(frozen=True)
class FaultView:
    code: str
    serial: str | None
    first_seen: datetime
    source: str
    detail: str | None = None


@dataclass
class SiteDay:
    site_id: int
    label: str
    card_name: str
    state: SiteState
    comm_since: datetime | None = None
    production_wh: float | None = None
    export_wh: float | None = None
    self_consumption_wh: float | None = None
    income_ils: Decimal | None = None
    open_faults: list[FaultView] = field(default_factory=list)
    ray: float | None = None
    kwp: float | None = None
    income_basis: str = "production"


def income(
    production_wh: float | None,
    export_wh: float | None,
    tariff: Decimal | None,
    basis: str,
) -> Decimal | None:
    wh = export_wh if basis == "export" else production_wh
    if wh is None or tariff is None:
        return None
    kwh = Decimal(str(wh)) / Decimal(1000)
    return (kwh * Decimal(tariff)).quantize(_CENT, rounding=ROUND_HALF_UP)


def effective_label(site: Site, owner_site_count: int) -> str:
    if site.label_he:
        return site.label_he
    if owner_site_count <= 1:
        return SINGLE_SITE_LABEL
    return site.card_name or site.portal_name


def compute_site_day(
    site: Site,
    owner_site_count: int,
    bundle: PullBundle | None,
    open_faults: list[FaultView],
    now: datetime,
    settings: Settings,
    ray: RayCalculator,
    *,
    local_date: date | None = None,
    session: Session | None = None,
) -> SiteDay:
    day = SiteDay(
        site_id=site.se_site_id,
        label=effective_label(site, owner_site_count),
        card_name=site.card_name or site.portal_name,
        state="ok",
        open_faults=list(open_faults),
        kwp=site.billing_kwp_override or site.peak_power_kwp_reported,
        income_basis=site.income_basis,
    )
    if site.status == "reauth_needed":
        day.state = "reauth_needed"
        return day

    if bundle is not None and bundle.site is None:
        # No fresh /sites answer: freshness is unknown, not stale. The stored
        # lastUpdateTime is from an earlier pull, so judging it would fake a comm loss.
        day.state = "no_data"
        return day
    last_update = site.last_update_time
    if bundle is not None:
        last_update = bundle.site.last_update_time
    if is_stale(
        last_update, now, settings.stale_after_minutes, lat=site.lat, lon=site.lon, tz=site.tz
    ):
        day.state = "comm_loss"
        day.comm_since = last_update
        return day

    overview = bundle.overview if bundle is not None else None
    if overview is None or overview.production_wh is None:
        day.state = "no_data"
        return day

    day.production_wh = overview.production_wh
    day.export_wh = overview.to_grid_wh
    day.self_consumption_wh = overview.to_self_consumption_wh
    day.income_ils = income(
        overview.production_wh, overview.to_grid_wh, site.tariff_ils_per_kwh, site.income_basis
    )
    if local_date is None:
        local_date = now.astimezone(ZoneInfo(site.tz)).date()
    # Today's row is not persisted yet: RAY gets today's production directly.
    # RAY is best-effort (spec interpretation 7): a failure never costs the day's figures.
    # Flush earlier pending writes (faults) first, so their errors surface outside the
    # guard. A database error inside RAY is not swallowed: it leaves the session needing
    # a rollback, so the day's row could not be saved anyway; it fails loudly instead.
    if session is not None:
        session.flush()
    try:
        day.ray = ray.compute(site, local_date, session, actual_wh=overview.production_wh)
    except SQLAlchemyError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("RAY failed for site %s: %s", site.se_site_id, type(exc).__name__)
        day.ray = None
    return day


def persist_site_day(
    session: Session,
    day: SiteDay,
    local_date: date,
    lifetime_wh: float | None,
    as_of: datetime,
    computed_at: datetime | None = None,
) -> None:
    """Upsert the daily row; non-ok states never overwrite an existing row."""
    row = session.get(DailyMetric, (day.site_id, local_date))
    if row is not None and day.state != "ok":
        return
    if row is None:
        row = DailyMetric(site_id=day.site_id, local_date=local_date)
        session.add(row)
    row.production_wh = day.production_wh
    row.export_wh = day.export_wh
    row.self_consumption_wh = day.self_consumption_wh
    row.income_ils = day.income_ils
    row.ray = day.ray
    row.comm_state = day.state
    row.lifetime_wh = lifetime_wh
    row.as_of = as_of
    row.computed_at = computed_at or as_of
    session.flush()
