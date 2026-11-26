"""Open-Meteo daily weather for RAY: mean cloud cover and PM10 over the site's local
daylight window, plus the daily maximum temperature. Never raises for HTTP or parse
errors; a failed call leaves only its own fields None. Only exception type names are
logged, since request URLs carry the site's coordinates."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime

import httpx
from sqlalchemy.orm import Session

from solya.models import WeatherDaily
from solya.rules.ray_math import WINDOW_HOURS

log = logging.getLogger(__name__)

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
AIR_QUALITY_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
TIMEOUT_SECONDS = 10.0
MIN_VALID_HOURS = 6
SOURCE = "open-meteo"


@dataclass
class WeatherDay:
    cloud_pct: float | None = None
    tmax_c: float | None = None
    pm10: float | None = None


def _num(v: object) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v)


def _window_mean(hourly: dict, key: str, local_date: date) -> float | None:
    """Mean of ``key`` over local hours WINDOW_HOURS of ``local_date`` (times are in the
    requested site timezone); None with fewer than MIN_VALID_HOURS valid values."""
    valid: list[float] = []
    for t, v in zip(hourly["time"], hourly[key]):
        try:
            stamp = datetime.fromisoformat(t)
        except (TypeError, ValueError):
            continue
        value = _num(v)
        if value is not None and stamp.date() == local_date and stamp.hour in WINDOW_HOURS:
            valid.append(value)
    return sum(valid) / len(valid) if len(valid) >= MIN_VALID_HOURS else None


async def _get(http: httpx.AsyncClient, url: str, params: dict) -> dict | None:
    try:
        resp = await http.get(url, params=params, timeout=TIMEOUT_SECONDS)
        resp.raise_for_status()
        body = resp.json()
        return body if isinstance(body, dict) else None
    except Exception as exc:  # noqa: BLE001 - weather is best-effort
        log.warning("weather fetch failed: %s", type(exc).__name__)
        return None


async def fetch_day(
    http: httpx.AsyncClient,
    lat: float,
    lon: float,
    tz: str,
    local_date: date,
    *,
    forecast_base: str = FORECAST_URL,
) -> WeatherDay:
    base = {
        "latitude": lat,
        "longitude": lon,
        "timezone": tz,
        "start_date": local_date.isoformat(),
        "end_date": local_date.isoformat(),
    }
    out = WeatherDay()
    fc = await _get(
        http, forecast_base,
        {**base, "hourly": "cloud_cover", "daily": "temperature_2m_max"},
    )
    if fc is not None:
        try:
            out.cloud_pct = _window_mean(fc["hourly"], "cloud_cover", local_date)
        except Exception as exc:  # noqa: BLE001
            log.warning("weather parse failed: %s", type(exc).__name__)
        try:
            out.tmax_c = _num(fc["daily"]["temperature_2m_max"][0])
        except Exception as exc:  # noqa: BLE001
            log.warning("weather parse failed: %s", type(exc).__name__)
    aq = await _get(http, AIR_QUALITY_URL, {**base, "hourly": "pm10"})
    if aq is not None:
        try:
            out.pm10 = _window_mean(aq["hourly"], "pm10", local_date)
        except Exception as exc:  # noqa: BLE001
            log.warning("weather parse failed: %s", type(exc).__name__)
    return out


def store_day(
    session: Session, site_id: int, local_date: date, day: WeatherDay, now: datetime
) -> None:
    row = session.get(WeatherDaily, (site_id, local_date))
    if row is None:
        row = WeatherDaily(site_id=site_id, local_date=local_date)
        session.add(row)
    # A field that failed this time keeps any value already stored.
    for field in ("cloud_pct", "tmax_c", "pm10"):
        value = getattr(day, field)
        if value is not None or getattr(row, field) is None:
            setattr(row, field, value)
    row.source = SOURCE
    row.fetched_at = now
