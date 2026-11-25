from datetime import date, datetime, timezone

import pytest

from solya.solaredge.units import day_window_utc, fmt_z, parse_se_time, to_w, to_wh

TZ = "Asia/Jerusalem"


def test_to_wh_units():
    assert to_wh(1.5, "KWH") == 1500
    assert to_wh(1, "kWh") == 1000
    assert to_wh(0.002, "MWH") == 2000
    assert to_wh(3, "wh") == 3
    assert to_wh(None, "KWH") is None


def test_to_wh_unknown_unit_raises():
    with pytest.raises(ValueError):
        to_wh(1, "BTU")


def test_to_w_units():
    assert to_w(2, "kW") == 2000
    assert to_w(5, "W") == 5
    assert to_w(None, "W") is None
    with pytest.raises(ValueError):
        to_w(1, "WH")


def test_parse_bare_time_is_local():
    assert parse_se_time("2026-11-18 06:50:00", TZ) == datetime(2026, 11, 18, 4, 50, tzinfo=timezone.utc)


def test_parse_iso_offset():
    assert parse_se_time("2026-11-24T16:45:00+02:00", TZ) == datetime(2026, 11, 24, 14, 45, tzinfo=timezone.utc)
    assert parse_se_time("2026-11-24T14:45:00Z", TZ) == datetime(2026, 11, 24, 14, 45, tzinfo=timezone.utc)


def test_fmt_z():
    assert fmt_z(datetime(2026, 11, 24, 14, 45, tzinfo=timezone.utc)) == "2026-11-24T14:45:00Z"


def test_day_window_normal():
    assert day_window_utc(date(2026, 11, 24), TZ) == ("2026-11-23T22:00:00Z", "2026-11-24T22:00:00Z")


def test_day_window_dst_end():
    assert day_window_utc(date(2026, 10, 25), TZ) == ("2026-10-24T21:00:00Z", "2026-10-25T22:00:00Z")
