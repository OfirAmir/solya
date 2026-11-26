import logging
from datetime import date, datetime, timezone

import httpx
import respx
from sqlalchemy import select

from solya import weather
from solya.db import session_scope
from solya.models import Site, WeatherDaily
from test_reporting import FARM, NOW, clock, h, mock_site  # noqa: F401 (fixtures)

FC = "https://api.open-meteo.com/v1/forecast"
AQ = "https://air-quality-api.open-meteo.com/v1/air-quality"
D = date(2026, 11, 26)


def hours(values, day):
    return [f"{day}T{h:02d}:00" for h in range(len(values))]


def forecast_body(cloud, tmax=21.5, day="2026-11-26"):
    return {"hourly": {"time": hours(cloud, day), "cloud_cover": cloud},
            "daily": {"time": [day], "temperature_2m_max": [tmax]}}


def aq_body(pm, day="2026-11-26"):
    return {"hourly": {"time": hours(pm, day), "pm10": pm}}


async def fetch(**kw):
    async with httpx.AsyncClient() as http:
        return await weather.fetch_day(http, 32.0, 34.8, "Asia/Jerusalem", D, **kw)


@respx.mock
async def test_mean_over_local_hours_7_to_16():
    cloud = [100.0] * 7 + [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0] + [0.0] * 7
    pm = [0.0] * 7 + [float(i) for i in range(10)] + [500.0] * 7
    f = respx.get(FC).mock(return_value=httpx.Response(200, json=forecast_body(cloud)))
    a = respx.get(AQ).mock(return_value=httpx.Response(200, json=aq_body(pm)))
    day = await fetch()
    assert day.cloud_pct == 55.0
    assert day.pm10 == 4.5
    assert day.tmax_c == 21.5
    for route in (f, a):
        q = route.calls.last.request.url.params
        assert q["timezone"] == "Asia/Jerusalem"
        assert q["start_date"] == q["end_date"] == "2026-11-26"
    assert f.calls.last.request.url.params["hourly"] == "cloud_cover"
    assert f.calls.last.request.url.params["daily"] == "temperature_2m_max"
    assert a.calls.last.request.url.params["hourly"] == "pm10"


@respx.mock
async def test_fewer_than_six_valid_hours_is_none():
    cloud = [None] * 24
    for h in (7, 8, 9, 10, 11):
        cloud[h] = 50.0
    respx.get(FC).mock(return_value=httpx.Response(200, json=forecast_body(cloud)))
    respx.get(AQ).mock(return_value=httpx.Response(200, json=aq_body([1.0] * 24)))
    day = await fetch()
    assert day.cloud_pct is None
    assert day.pm10 == 1.0
    # six valid hours is enough
    cloud[12] = 50.0
    respx.get(FC).mock(return_value=httpx.Response(200, json=forecast_body(cloud)))
    assert (await fetch()).cloud_pct == 50.0


@respx.mock
async def test_air_quality_500_keeps_other_fields():
    respx.get(FC).mock(return_value=httpx.Response(200, json=forecast_body([40.0] * 24)))
    respx.get(AQ).mock(return_value=httpx.Response(500))
    day = await fetch()
    assert (day.cloud_pct, day.tmax_c, day.pm10) == (40.0, 21.5, None)


@respx.mock
async def test_garbage_and_timeouts_never_raise():
    respx.get(FC).mock(side_effect=httpx.ConnectTimeout("x"))
    respx.get(AQ).mock(return_value=httpx.Response(200, text="not json"))
    day = await fetch()
    assert (day.cloud_pct, day.tmax_c, day.pm10) == (None, None, None)


@respx.mock
async def test_forecast_base_override():
    hist = "https://historical-forecast-api.open-meteo.com/v1/forecast"
    r = respx.get(hist).mock(return_value=httpx.Response(200, json=forecast_body([40.0] * 24)))
    respx.get(AQ).mock(return_value=httpx.Response(500))
    assert (await fetch(forecast_base=hist)).cloud_pct == 40.0
    assert r.called


def test_store_day_upserts(db):
    with session_scope(db) as s:
        weather.store_day(s, FARM, D, weather.WeatherDay(10.0, 20.0, 30.0), NOW)
    with session_scope(db) as s:
        weather.store_day(s, FARM, D, weather.WeatherDay(None, 25.0, None), NOW)
    with session_scope(db) as s:
        rows = list(s.scalars(select(WeatherDaily)))
        assert len(rows) == 1
        assert (rows[0].cloud_pct, rows[0].tmax_c, rows[0].pm10) == (None, 25.0, None)
        assert rows[0].source == "open-meteo"


def _locate(db):
    with session_scope(db) as s:
        site = s.get(Site, FARM)
        site.lat, site.lon = 31.77, 35.21


def _weather_mocks():
    f = respx.get(FC).mock(return_value=httpx.Response(200, json=forecast_body([40.0] * 24, day="2026-11-24")))
    a = respx.get(AQ).mock(return_value=httpx.Response(200, json=aq_body([3.0] * 24, day="2026-11-24")))
    return f, a


async def test_evening_pull_stores_weather_when_enabled(h, load_fixture, db):
    _locate(db)
    h.reporter._settings = h.reporter._settings.model_copy(update={"ray_implementation": "v1"})
    with respx.mock:
        mock_site(load_fixture, FARM)
        f, a = _weather_mocks()
        await h.reporter.evening_pull(FARM, date(2026, 11, 24))
    assert f.called and a.called
    with session_scope(db) as s:
        row = s.get(WeatherDaily, (FARM, date(2026, 11, 24)))
        assert (row.cloud_pct, row.tmax_c, row.pm10) == (40.0, 21.5, 3.0)


async def test_no_weather_call_when_disabled_or_midday(h, load_fixture, db):
    _locate(db)
    with respx.mock:  # RAY disabled
        mock_site(load_fixture, FARM)
        f, a = _weather_mocks()
        await h.reporter.evening_pull(FARM, date(2026, 11, 24))
        assert not f.called and not a.called
    h.reporter._settings = h.reporter._settings.model_copy(update={"ray_implementation": "v1"})
    with respx.mock:  # midday
        mock_site(load_fixture, FARM)
        f, a = _weather_mocks()
        await h.reporter.midday_check(FARM)
        assert not f.called and not a.called
    with session_scope(db) as s:
        assert s.scalars(select(WeatherDaily)).first() is None


async def test_no_url_in_info_logs(h, load_fixture, db, caplog):
    _locate(db)
    h.reporter._settings = h.reporter._settings.model_copy(update={"ray_implementation": "v1"})
    caplog.set_level(logging.INFO)
    with respx.mock:
        mock_site(load_fixture, FARM)
        respx.get(FC).mock(side_effect=httpx.ConnectTimeout("boom 31.77"))
        respx.get(AQ).mock(return_value=httpx.Response(500))
        await h.reporter.evening_pull(FARM, date(2026, 11, 24))
    text = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("solya"))
    assert "weather fetch failed: ConnectTimeout" in text
    assert "open-meteo" not in text and "31.77" not in text
