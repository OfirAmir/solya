"""RayV1: reads daily_metrics and weather_daily, stores every component in ray_daily.

The 25.11 farm example (Perf 99.8, Health 89.8, Env 75.2 -> RAY 90.15) is rebuilt from
synthetic rows, all for local_date 2026-11-25:

* Env 75.2: cloud 50% (Cloud 20), Tmax 22 C (Temp 0), PM10 48 (PM 15*48/150 = 4.8).
* Health 89.8: the 30 days before (26.10-24.11) alternate 13060 / 6940 Wh, so mean = 10000
  and population std = 3060, CV = 0.306, StabDeduction = 0.306/0.6*20 = 10.2.
* Perf 99.8: every day of Nov 2025 and Nov 2024 produced E = 11250 Wh, so
  expected = 11250 ("history") and both same-days refs are 11250. Actual A = 11075 Wh:
  ratio = 98.44, /0.85 cloud factor -> PerfRatio capped at 100. HistSub against
  last30 10000 (100, capped) and year refs 11250 (98.444) with weights 2/3, 1/3, 1/6
  (effective 4/7, 2/7, 1/7) = 57.1429 + 42.1905 = 99.3333. Perf = 70 + 29.8 = 99.8.
"""
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from solya.db import session_scope
from solya.models import DailyMetric, Owner, RayDaily, Site, WeatherDaily
from solya.rules.ray import load_ray
from solya.rules.ray_v1 import RayV1

D = date(2026, 11, 25)
T0 = datetime(2026, 11, 25, 15, 0, tzinfo=timezone.utc)
SID = 1001
E = 11250.0
A = 11075.0


def site_row(s, kwp_override=None, kwp_reported=100.0):
    s.add(Owner(id=1, display_name="o", created_at=T0))
    site = Site(
        se_site_id=SID, owner_id=1, portal_name="farm", tz="Asia/Jerusalem",
        peak_power_kwp_reported=kwp_reported, billing_kwp_override=kwp_override, created_at=T0,
    )
    s.add(site)
    s.flush()
    return site


def metric(s, d, wh, state="ok"):
    s.merge(DailyMetric(site_id=SID, local_date=d, production_wh=wh, comm_state=state,
                        as_of=T0, computed_at=T0))


def weather(s, d=D, cloud=50.0, tmax=22.0, pm10=48.0):
    s.add(WeatherDaily(site_id=SID, local_date=d, cloud_pct=cloud, tmax_c=tmax, pm10=pm10,
                       source="open-meteo", fetched_at=T0))


def last30(s):
    for i in range(1, 31):
        metric(s, D - timedelta(days=i), 13060.0 if i % 2 else 6940.0)


def month(s, year, wh=E, state="backfill", days=range(1, 31)):
    for day in days:
        metric(s, date(year, 11, day), wh, state)


def farm(s):
    site = site_row(s)
    last30(s)
    month(s, 2025)
    month(s, 2024)
    weather(s)
    s.flush()
    return site


def stored(s):
    return s.get(RayDaily, (SID, D))


def test_farm_example_90_2(db, settings):
    with session_scope(db) as s:
        site = farm(s)
        ray = RayV1(settings).compute(site, D, s, actual_wh=A)
        assert ray == pytest.approx(90.15, abs=1e-6)
        row = stored(s)
        assert row.ray == pytest.approx(90.15, abs=1e-6)
        assert row.perf == pytest.approx(99.8, abs=1e-6)
        assert row.perf_ratio == pytest.approx(100)
        assert row.hist_sub == pytest.approx(99.3333333, abs=1e-6)
        assert row.health == pytest.approx(89.8, abs=1e-6)
        assert row.env == pytest.approx(75.2, abs=1e-6)
        assert row.cv == pytest.approx(0.306, abs=1e-9)
        assert row.expected_wh == pytest.approx(E)
        assert row.expected_source == "history"
        assert row.confidence == "normal"
        assert (row.cloud_pct, row.tmax_c, row.pm10) == (50.0, 22.0, 48.0)
        assert row.computed_at is not None


def test_recompute_upserts(db, settings):
    with session_scope(db) as s:
        site = farm(s)
        RayV1(settings).compute(site, D, s, actual_wh=A)
        RayV1(settings).compute(site, D, s, actual_wh=5000.0)
        rows = s.scalars(select(RayDaily)).all()
        assert len(rows) == 1 and rows[0].ray < 90


def test_uses_actual_wh_not_todays_row(db, settings):
    with session_scope(db) as s:
        site = farm(s)
        metric(s, D, 1.0)  # a today row with very different production
        s.flush()
        assert RayV1(settings).compute(site, D, s, actual_wh=A) == pytest.approx(90.15, abs=1e-6)


def test_missing_actual_or_session_is_none(db, settings):
    with session_scope(db) as s:
        site = farm(s)
        assert RayV1(settings).compute(site, D, s, actual_wh=None) is None
        assert RayV1(settings).compute(site, D, s) is None
        assert RayV1(settings).compute(site, D, None, actual_wh=A) is None
        assert stored(s) is None


@pytest.mark.parametrize("field", [None, "cloud", "tmax", "pm10"])
def test_missing_weather_is_none_and_writes_nothing(db, settings, field):
    with session_scope(db) as s:
        site = site_row(s)
        last30(s)
        month(s, 2025)
        if field is not None:
            weather(s, **{field: None})
        s.flush()
        assert RayV1(settings).compute(site, D, s, actual_wh=A) is None
        assert stored(s) is None


def test_baseline_one_year_only(db, settings):
    with session_scope(db) as s:
        site = site_row(s)
        last30(s)
        month(s, 2024)  # no Nov 2025 at all
        weather(s)
        s.flush()
        ray = RayV1(settings).compute(site, D, s, actual_wh=A)
        row = stored(s)
        assert row.expected_source == "history"
        assert row.expected_wh == pytest.approx(E)
        # Without year1 the HistSub weights renormalise to 4/5, 1/5.
        assert row.hist_sub == pytest.approx(0.8 * 100 + 0.2 * A / E * 100)
        assert row.confidence == "normal"
        assert ray == pytest.approx(row.ray)


def test_baseline_table_when_fewer_than_10_days(db, settings):
    with session_scope(db) as s:
        site = site_row(s, kwp_override=50.0, kwp_reported=100.0)
        last30(s)
        month(s, 2025, days=range(1, 6))
        month(s, 2024, days=range(1, 5))  # 9 real days in total
        weather(s)
        s.flush()
        RayV1(settings).compute(site, D, s, actual_wh=A)
        row = stored(s)
        assert row.expected_source == "table"
        assert row.expected_wh == pytest.approx(settings.ray_national_kwh_per_kwp_day[10] * 50.0 * 1000)
        assert row.confidence == "low"


def test_low_confidence_with_fewer_than_30_real_days(db, settings):
    with session_scope(db) as s:
        site = site_row(s)
        for i in range(1, 11):
            metric(s, D - timedelta(days=i), 10000.0)
        month(s, 2025, days=range(1, 11))  # 20 real days in all, history baseline
        weather(s)
        s.flush()
        RayV1(settings).compute(site, D, s, actual_wh=A)
        row = stored(s)
        assert row.expected_source == "history"
        assert row.confidence == "low"


def test_stale_and_none_rows_excluded(db, settings):
    with session_scope(db) as s:
        site = farm(s)
        before = RayV1(settings).compute(site, D, s, actual_wh=A)
        # Real rows replaced by stale or empty ones, with values that would move every
        # reference if they were counted.
        for i in (1, 2, 3):
            metric(s, D - timedelta(days=i), 999999.0, state="comm_loss")
        metric(s, D - timedelta(days=4), None, state="ok")
        metric(s, date(2025, 11, 3), 1.0, state="comm_loss")
        metric(s, date(2024, 11, 3), None, state="backfill")
        metric(s, date(2025, 11, 4), 50.0, state="no_data")
        s.flush()
        after_ray = RayV1(settings).compute(site, D, s, actual_wh=A)
        row = stored(s)
        # The excluded rows are gone from every window; the remaining days still
        # average to the same references, so only the CV is recomputed (26 last-30 days now).
        assert row.expected_wh == pytest.approx(E)
        assert after_ray is not None and before is not None
        assert row.hist_sub == pytest.approx(99.3333333, abs=1e-6)
        assert row.perf == pytest.approx(99.8, abs=1e-6)
        remaining = [13060.0 if i % 2 else 6940.0 for i in range(5, 31)]
        mean = sum(remaining) / len(remaining)
        std = (sum((v - mean) ** 2 for v in remaining) / len(remaining)) ** 0.5
        assert row.cv == pytest.approx(std / mean)


def test_no_kwp_and_no_history_is_none(db, settings):
    with session_scope(db) as s:
        site = site_row(s, kwp_override=None, kwp_reported=None)
        last30(s)
        weather(s)
        s.flush()
        assert RayV1(settings).compute(site, D, s, actual_wh=A) is None
        assert stored(s) is None


def test_zero_history_and_refs_never_raise(db, settings):
    with session_scope(db) as s:
        site = site_row(s)
        for i in range(1, 31):
            metric(s, D - timedelta(days=i), 0.0)
        month(s, 2025, wh=0.0)
        weather(s)
        s.flush()
        ray = RayV1(settings).compute(site, D, s, actual_wh=A)
        row = stored(s)
        assert row.expected_source == "table"  # a zero history mean is no baseline
        assert row.cv is None and row.health == 100
        assert ray is not None


def test_load_ray_returns_working_instance(db, settings):
    calc = load_ray("solya.rules.ray_v1:RayV1", settings)
    assert isinstance(calc, RayV1)
    with session_scope(db) as s:
        site = farm(s)
        assert calc.compute(site, D, s, actual_wh=A) == pytest.approx(90.15, abs=1e-6)


def test_none_result_removes_stale_row(db, settings):
    with session_scope(db) as s:
        site = farm(s)
        assert RayV1(settings).compute(site, D, s, actual_wh=A) is not None
        assert stored(s) is not None
        assert RayV1(settings).compute(site, D, s, actual_wh=None) is None
        assert stored(s) is None
        assert RayV1(settings).compute(site, D, s, actual_wh=A) is not None
        s.get(WeatherDaily, (SID, D)).pm10 = None
        s.flush()
        assert RayV1(settings).compute(site, D, s, actual_wh=A) is None
        assert stored(s) is None
