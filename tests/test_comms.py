"""R23: staleness is judged against the last daylight moment, not the wall clock."""
from datetime import datetime, timedelta, timezone

from solya.rules.comms import is_stale, last_daylight

LAT, LON, TZ = 32.33, 34.89, "Asia/Jerusalem"
# 2026-11-24 at Beit Yitzhak: sunrise 06:17:47, sunset 16:36:07 local (UTC+2); 23 Nov sunset 16:36:25.
SUNSET = datetime(2026, 11, 24, 14, 36, 7, tzinfo=timezone.utc)
PREV_SUNSET = datetime(2026, 11, 23, 14, 36, 25, tzinfo=timezone.utc)


def stale(last, now):
    return is_stale(last, now, 60, lat=LAT, lon=LON, tz=TZ)


def test_last_daylight_is_now_during_the_day():
    noon = datetime(2026, 11, 24, 10, 0, tzinfo=timezone.utc)
    assert last_daylight(noon, LAT, LON, TZ) == noon


def test_last_daylight_is_sunset_after_dark():
    evening = datetime(2026, 11, 24, 19, 0, tzinfo=timezone.utc)
    assert abs(last_daylight(evening, LAT, LON, TZ) - SUNSET) < timedelta(seconds=1)


def test_last_daylight_before_sunrise_is_previous_sunset():
    early = datetime(2026, 11, 24, 2, 0, tzinfo=timezone.utc)  # 04:00 local
    assert abs(last_daylight(early, LAT, LON, TZ) - PREV_SUNSET) < timedelta(seconds=1)


def test_daylight_threshold_unchanged():
    noon = datetime(2026, 11, 24, 10, 0, tzinfo=timezone.utc)
    assert not stale(noon - timedelta(minutes=59), noon)
    assert stale(noon - timedelta(minutes=61), noon)


def test_late_retry_after_dusk_is_not_stale():
    """The data froze at dusk; a retry 70 min later must not see a comm loss."""
    last = SUNSET - timedelta(minutes=5)
    assert not stale(last, SUNSET + timedelta(minutes=70))
    assert not stale(last, datetime(2026, 11, 24, 22, 0, tzinfo=timezone.utc))


def test_lost_before_sunset_is_stale_at_night():
    assert stale(SUNSET - timedelta(minutes=90), SUNSET + timedelta(minutes=30))


def test_unknown_is_stale():
    assert stale(None, SUNSET)
