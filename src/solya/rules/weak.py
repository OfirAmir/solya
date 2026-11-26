"""Weak production alert (smarter-alerts design, goal 2).

A day *counts* only with real production data, weather, a history-based expectation
and ``cloud_pct <= WEAK_MAX_CLOUD_PCT``; other days are skipped, never good or bad.
The fault opens when the last ``WEAK_DAYS`` counted days (within ``LOOKBACK_DAYS``
calendar days) all have ``perf_ratio < WEAK_THRESHOLD`` and closes when a counted day
reaches ``WEAK_RECOVER``. Evaluated once per evening, for the day just computed; a
skipped day neither opens nor closes it.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from solya.config import Settings
from solya.models import DailyMetric, RayDaily
from solya.rules.faults import DetectedFault, FaultEvent, sync_faults

LOOKBACK_DAYS = 10
WEAK_SOURCE = "weak"
WEAK_CODE = "WEAK_PRODUCTION"


@dataclass(frozen=True)
class WeakDay:
    local_date: date
    perf_ratio: float | None
    expected_source: str | None
    cloud_pct: float | None
    production_wh: float | None
    comm_state: str | None


@dataclass(frozen=True)
class WeakVerdict:
    action: Literal["open", "close", "none"]
    days: int = 0
    pct: int | None = None


def counts(d: WeakDay, max_cloud: float) -> bool:
    return (
        d.perf_ratio is not None
        and d.comm_state == "ok"
        and d.production_wh is not None
        and d.production_wh > 0  # a zero day has its own ZERO_PRODUCTION alert
        and d.expected_source == "history"
        and d.cloud_pct is not None
        and d.cloud_pct <= max_cloud
    )


def evaluate_weak(
    days: list[WeakDay],
    today: date,
    *,
    threshold: float,
    recover: float,
    n_days: int,
    max_cloud: float,
    lookback_days: int = LOOKBACK_DAYS,
) -> WeakVerdict:
    """What tonight's evaluation does: open, close, or leave the state as it is."""
    oldest = today - timedelta(days=lookback_days - 1)
    counted = sorted(
        (d for d in days if oldest <= d.local_date <= today and counts(d, max_cloud)),
        key=lambda d: d.local_date,
        reverse=True,
    )
    if not counted or counted[0].local_date != today:
        return WeakVerdict("none")  # tonight's day is skipped: no change
    if counted[0].perf_ratio >= recover:
        return WeakVerdict("close")
    last = counted[:n_days]
    if len(last) == n_days and all(d.perf_ratio < threshold for d in last):
        mean = sum(d.perf_ratio for d in last) / n_days
        return WeakVerdict("open", n_days, math.floor(mean + 0.5))
    return WeakVerdict("none")


def load_weak_days(
    session: Session, site_id: int, today: date, lookback_days: int = LOOKBACK_DAYS
) -> list[WeakDay]:
    oldest = today - timedelta(days=lookback_days - 1)
    rows = session.execute(
        select(RayDaily, DailyMetric)
        .outerjoin(
            DailyMetric,
            (DailyMetric.site_id == RayDaily.site_id)
            & (DailyMetric.local_date == RayDaily.local_date),
        )
        .where(
            RayDaily.site_id == site_id,
            RayDaily.local_date >= oldest,
            RayDaily.local_date <= today,
        )
        .order_by(RayDaily.local_date.desc())
    ).all()
    return [
        WeakDay(
            r.local_date, r.perf_ratio, r.expected_source, r.cloud_pct,
            m.production_wh if m is not None else None,
            m.comm_state if m is not None else None,
        )
        for r, m in rows
    ]


def weak_enabled(settings: Settings) -> bool:
    return bool(settings.weak_alerts_enabled and settings.ray_implementation)


def sync_weak(
    session: Session,
    site_id: int,
    today: date,
    now: datetime,
    settings: Settings,
    data_complete: bool,
) -> list[FaultEvent]:
    """Run tonight's weak-production evaluation through the fault lifecycle.

    ``data_complete`` False (a silent pull) never closes, like ``sync_faults``.
    """
    if not weak_enabled(settings):
        return []
    v = evaluate_weak(
        load_weak_days(session, site_id, today),
        today,
        threshold=settings.weak_threshold,
        recover=settings.weak_recover,
        n_days=settings.weak_days,
        max_cloud=settings.weak_max_cloud_pct,
    )
    if v.action == "open":
        detail = json.dumps({"days": v.days, "pct": v.pct})
        found = DetectedFault(f"weak:{site_id}", WEAK_SOURCE, WEAK_CODE, None, now, detail)
        return sync_faults(session, site_id, [found], now, False, source=WEAK_SOURCE)
    if v.action == "close":
        return sync_faults(session, site_id, [], now, data_complete, source=WEAK_SOURCE)
    return []
