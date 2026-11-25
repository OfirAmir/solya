"""Communication freshness rule and the sun times it is anchored to."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from astral import Observer
from astral.sun import sun

BEIT_YITZHAK = (32.33, 34.89)


def sun_times(lat: float | None, lon: float | None, tz: str, local_date: date) -> dict[str, datetime]:
    """Astral sun times for a local date; an unknown location falls back to Beit Yitzhak."""
    if lat is None or lon is None:
        lat, lon = BEIT_YITZHAK
    return sun(Observer(lat, lon), date=local_date, tzinfo=ZoneInfo(tz))


def last_daylight(now: datetime, lat: float | None, lon: float | None, tz: str) -> datetime:
    """The latest daylight moment at or before ``now`` (R23): ``now`` itself in daylight,
    today's sunset after dark, the previous day's sunset before sunrise."""
    local_date = now.astimezone(ZoneInfo(tz)).date()
    today = sun_times(lat, lon, tz, local_date)
    if now < today["sunrise"]:
        return sun_times(lat, lon, tz, local_date - timedelta(days=1))["sunset"]
    return min(now, today["sunset"])


def is_stale(
    last_update_time: datetime | None,
    now: datetime,
    stale_after_minutes: int,
    *,
    lat: float | None,
    lon: float | None,
    tz: str,
) -> bool:
    """True when the site had not reported within ``stale_after_minutes`` of the last
    daylight moment (spec §5.5: the threshold applies in daylight; inverters stop
    reporting after dark). Unknown counts as stale."""
    if last_update_time is None:
        return True
    reference = last_daylight(now, lat, lon, tz)
    return reference - last_update_time > timedelta(minutes=stale_after_minutes)
