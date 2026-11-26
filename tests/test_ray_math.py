import pytest
from pydantic import ValidationError

from solya.config import Settings
from solya.rules import ray_math as m


def test_combine_examples():
    assert m.combine(99.8, 89.8, 75.2) == pytest.approx(90.15)
    assert int(m.combine(99.8, 89.8, 75.2) + 0.5) == 90
    assert m.combine(98.7, 92.0, 75.2) == pytest.approx(90.5, abs=0.05)


def test_combine_cap_binds():
    assert m.combine(40, 100, 100) == 65


def test_perf_ratio():
    assert m.perf_ratio(300_000, 300_000, 0) == 100
    assert m.perf_ratio(270_000, 300_000, 50) == 100
    assert m.perf_ratio(150_000, 300_000, 0) == pytest.approx(50)
    assert m.perf_ratio(0, 300_000, 0) == 0


def test_hist_sub():
    full = [(100.0, 2 / 3), (100.0, 1 / 3), (100.0, 1 / 6)]
    assert m.hist_sub(100.0, full) == pytest.approx(100)
    two = [(None, 2 / 3), (200.0, 1 / 3), (100.0, 1 / 6)]
    assert m.hist_sub(100.0, two) == pytest.approx((50 / 3 + 100 / 6) / (1 / 3 + 1 / 6))
    assert m.hist_sub(100.0, [(None, 2 / 3), (None, 1 / 3)]) is None
    assert m.hist_sub(100.0, []) is None


def test_health():
    assert m.health(0.306) == pytest.approx(89.8)
    assert m.health(1.0) == 80
    assert m.health(None) == 100


def test_env():
    assert m.env(0, 25, 0) == 100
    assert m.env(100, 80, 300) == 100 - 40 - 20 - 15


def test_perf():
    assert m.perf(100, None) == pytest.approx(100)
    assert m.perf(80, 100) == pytest.approx(86)


def test_national_table_default_valid(settings):
    t = settings.ray_national_kwh_per_kwp_day
    assert len(t) == 12 and all(v > 0 for v in t)
    assert settings.ray_visible is False


@pytest.mark.parametrize("bad", [[1.0] * 11, [1.0] * 13, [1.0] * 11 + [0.0],
                                 [1.0] * 11 + [float("inf")], [1.0] * 11 + [float("nan")]])
def test_settings_rejects_bad_table(settings, bad):
    data = settings.model_dump()
    data["ray_national_kwh_per_kwp_day"] = bad
    with pytest.raises(ValidationError):
        Settings.model_validate(data)


def _env_settings(monkeypatch, table: str) -> Settings:
    monkeypatch.setenv("RAY_NATIONAL_KWH_PER_KWP_DAY", table)
    return Settings(
        _env_file=None, solaredge_client_id="i", solaredge_client_secret="s",
        telegram_bot_token="1:t", telegram_bot_username="b",
        public_base_url="https://solya.example", token_encryption_key="k" * 44,
    )


def test_national_table_from_env_json(monkeypatch):
    s = _env_settings(monkeypatch, "[1,2,3,4,5,6,7,8,9,10,11,12.5]")
    assert s.ray_national_kwh_per_kwp_day == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12.5]


@pytest.mark.parametrize("bad", ["[1,2,3]", "[1,1,1,1,1,1,1,1,1,1,1,Infinity]",
                                 "[1,1,1,1,1,1,1,1,1,1,1,NaN]"])
def test_national_table_env_rejects_bad(monkeypatch, bad):
    with pytest.raises(ValidationError):
        _env_settings(monkeypatch, bad)
