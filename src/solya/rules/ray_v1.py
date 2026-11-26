"""RAY v1 calculator: reads daily_metrics and weather_daily, stores components in ray_daily.

All arithmetic is in ``ray_math``. Interpretations (spec 1-7): only real days count
(``production_wh`` not null and ``comm_state`` in ok/backfill), today's production comes
from the caller (today's row is not persisted yet), and a missing or non-positive mean is
treated as absent, never as zero.
"""
from __future__ import annotations

import calendar
import statistics
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from solya.models import DailyMetric, RayDaily, Site, WeatherDaily
from solya.rules import ray_math as m

REAL_STATES = ("ok", "backfill")
LAST_DAYS = 30
MIN_TOTAL_DAYS_NORMAL = 30


def _month_bounds(year: int, month: int, last_day: int | None = None) -> tuple[date, date]:
    end = calendar.monthrange(year, month)[1]
    if last_day is not None:
        end = min(end, last_day)
    return date(year, month, 1), date(year, month, end)


def _positive_mean(values: list[float], minimum: int) -> float | None:
    if len(values) < minimum:
        return None
    mean = sum(values) / len(values)
    return mean if mean > 0 else None


class RayV1:
    def __init__(self, settings=None) -> None:
        self._settings = settings

    def _real(self, session: Session, site_id: int, start: date, end: date) -> list[float]:
        """Real production values for ``start``..``end`` inclusive."""
        return list(
            session.scalars(
                select(DailyMetric.production_wh).where(
                    DailyMetric.site_id == site_id,
                    DailyMetric.local_date >= start,
                    DailyMetric.local_date <= end,
                    DailyMetric.production_wh.is_not(None),
                    DailyMetric.comm_state.in_(REAL_STATES),
                )
            )
        )

    def _expected(self, session: Session, site: Site, d: date) -> tuple[float | None, str | None]:
        history: list[float] = []
        for back in (1, 2):
            history += self._real(session, site.se_site_id, *_month_bounds(d.year - back, d.month))
        mean = _positive_mean(history, m.MIN_BASELINE_DAYS)
        if mean is not None:
            return mean, "history"
        kwp = site.billing_kwp_override or site.peak_power_kwp_reported
        table = getattr(self._settings, "ray_national_kwh_per_kwp_day", None)
        if not kwp or kwp <= 0 or not table:
            return None, None
        expected = table[d.month - 1] * kwp * 1000
        return (expected, "table") if expected > 0 else (None, None)

    def compute(
        self, site: Site, local_date: date, session: Session | None, actual_wh: float | None = None
    ) -> float | None:
        if session is None or actual_wh is None:
            return None
        weather = session.get(WeatherDaily, (site.se_site_id, local_date))
        if weather is None or None in (weather.cloud_pct, weather.tmax_c, weather.pm10):
            return None

        expected_wh, source = self._expected(session, site, local_date)
        if expected_wh is None:
            return None

        sid = site.se_site_id
        yesterday = local_date - timedelta(days=1)
        last = self._real(session, sid, local_date - timedelta(days=LAST_DAYS), yesterday)
        refs = [(_positive_mean(last, m.MIN_LAST30_DAYS), m.HIST_WEIGHTS["last30"])]
        for back, key in ((1, "year1"), (2, "year2")):
            same = self._real(
                session, sid, *_month_bounds(local_date.year - back, local_date.month, local_date.day)
            )
            refs.append((_positive_mean(same, m.MIN_SAME_DAYS), m.HIST_WEIGHTS[key]))

        cv = None
        cv_mean = _positive_mean(last, m.MIN_CV_DAYS)
        if cv_mean is not None:
            cv = statistics.pstdev(last) / cv_mean

        total_days = len(self._real(session, sid, date.min, yesterday))

        perf_ratio = m.perf_ratio(actual_wh, expected_wh, weather.cloud_pct)
        hist_sub = m.hist_sub(actual_wh, refs)
        perf = m.perf(perf_ratio, hist_sub)
        health = m.health(cv)
        env = m.env(weather.cloud_pct, weather.tmax_c, weather.pm10)
        ray = m.combine(perf, health, env)
        low = source == "table" or total_days < MIN_TOTAL_DAYS_NORMAL or cv is None

        row = session.get(RayDaily, (sid, local_date))
        if row is None:
            row = RayDaily(site_id=sid, local_date=local_date)
            session.add(row)
        row.ray = ray
        row.perf = perf
        row.perf_ratio = perf_ratio
        row.hist_sub = hist_sub
        row.health = health
        row.env = env
        row.expected_wh = expected_wh
        row.expected_source = source
        row.cv = cv
        row.cloud_pct = weather.cloud_pct
        row.tmax_c = weather.tmax_c
        row.pm10 = weather.pm10
        row.confidence = "low" if low else "normal"
        row.computed_at = datetime.now(timezone.utc)
        session.flush()
        return ray
