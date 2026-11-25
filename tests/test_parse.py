from datetime import datetime, timezone

import pytest

from solya.solaredge import parse

TZ = "Asia/Jerusalem"


def test_parse_site_allowlist(load_fixture):
    s = parse.parse_site(load_fixture("solaredge/site_farm.json"))
    assert not hasattr(s, "owner") and not hasattr(s, "address")
    assert s.site_id == 3033498
    assert s.name == "אורן סופר"
    assert s.peak_power_kwp == 65.52
    assert s.timezone == "Asia/Jerusalem"
    assert (s.lat, s.lon) == (32.34, 34.89)
    assert s.active is True
    assert s.last_update_time == datetime(2026, 11, 24, 14, 45, tzinfo=timezone.utc)


def test_parse_site_home(load_fixture):
    s = parse.parse_site(load_fixture("solaredge/site_home.json"))
    assert (s.site_id, s.peak_power_kwp) == (4552265, 26.55)


def test_parse_overview_home(load_fixture):
    o = parse.parse_overview(load_fixture("solaredge/overview_home_20261124.json"), 4552265)
    assert o.production_wh == 85039
    assert o.to_grid_wh == 79225
    assert o.to_self_consumption_wh == 5814


def test_parse_overview_farm(load_fixture):
    o = parse.parse_overview(load_fixture("solaredge/overview_farm_20261124.json"), 3033498)
    assert o.production_wh == 316990 and o.to_grid_wh == 316990
    assert o.to_self_consumption_wh is None and o.consumption_wh is None


def test_parse_overview_null_is_none(load_fixture):
    o = parse.parse_overview(load_fixture("solaredge/overview_null.json"), 3033498)
    assert o.production_wh is None


def test_parse_overview_site_mismatch_raises(load_fixture):
    with pytest.raises(ValueError):
        parse.parse_overview(load_fixture("solaredge/overview_home_20261124.json"), 1)


def test_parse_overview_uses_block_unit():
    j = {"siteId": 1, "production": {"total": 2.5, "unit": "KWH", "toGrid": 1, "toSelfConsumption": None},
         "consumption": {"total": 1, "unit": "MWH"}}
    o = parse.parse_overview(j, 1)
    assert o.production_wh == 2500 and o.to_grid_wh == 1000
    assert o.to_self_consumption_wh is None
    assert o.consumption_wh == 1_000_000


def test_parse_alerts_fan(load_fixture):
    (a,) = parse.parse_alerts(load_fixture("solaredge/alerts_fan.json"), TZ)
    assert a.alert_id == 101 and a.status == "OPEN" and a.impact == 3
    assert a.serial == "7E04E44D-B3"
    assert a.first_trigger == datetime(2026, 11, 18, 4, 50, tzinfo=timezone.utc)
    assert a.error_codes == ["FAN_ALERT_1"]
    assert a.close_date is None


def test_parse_alerts_empty(load_fixture):
    assert parse.parse_alerts(load_fixture("solaredge/alerts_empty.json"), TZ) == []


def test_parse_alerts_code_falls_back_to_type():
    (a,) = parse.parse_alerts([{"alertId": 1, "type": "GRID_FAULT", "status": "OPEN"}], TZ)
    assert a.error_codes == ["GRID_FAULT"]


def test_parse_inverter_status_last_mode(load_fixture):
    st = parse.parse_inverter_status(load_fixture("solaredge/inverters_status_ok.json"), TZ)
    assert st["7E04E44D-B3"].last_mode == "MPPT"
    assert st["7E04E44D-B3"].last_mode_at == datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
    st = parse.parse_inverter_status(load_fixture("solaredge/inverters_status_fault.json"), TZ)
    assert st["7E04E44D-B3"].last_mode == "LOCKED_GRID"


def test_parse_inverter_status_no_values():
    st = parse.parse_inverter_status({"inverters": {"X": {"inverterMode": {"values": []}}}}, TZ)
    assert st["X"].last_mode is None and st["X"].last_mode_at is None


def test_parse_devices_active_filter_fields(load_fixture):
    devs = parse.parse_devices(load_fixture("solaredge/devices_farm.json"))
    assert [d.type for d in devs] == ["INVERTER", "METER"]
    inv = devs[0]
    assert inv.serial == "7E04E44D-B3" and inv.active is True and inv.model == "SE27.6K"


def test_parse_lifetime(load_fixture):
    lt = parse.parse_lifetime(load_fixture("solaredge/lifetime_farm.json"), TZ)
    assert lt.energy_wh == 671421060
    assert lt.last_update_time == datetime(2026, 11, 24, 14, 45, tzinfo=timezone.utc)


def test_parse_site_missing_timezone_uses_default(load_fixture):
    j = load_fixture("solaredge/site_farm.json")
    j["location"].pop("timezone")
    assert parse.parse_site(j).timezone == "Asia/Jerusalem"
    assert parse.parse_site(j, default_tz="Europe/Berlin").timezone == "Europe/Berlin"
