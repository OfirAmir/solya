from datetime import datetime, timedelta, timezone


def test_settings_defaults(settings):
    assert settings.budget_monthly_credits == 2000
    assert settings.budget_per_minute == 8
    assert settings.se_scope == "SITE_DATA DEVICE_DATA"
    assert settings.fault_min_samples == 2


def test_fault_min_samples_must_be_positive(settings):
    import pytest
    from pydantic import ValidationError

    from solya.config import Settings

    base = {**settings.model_dump(), "_env_file": None}
    assert Settings(**{**base, "fault_min_samples": 1}).fault_min_samples == 1
    with pytest.raises(ValidationError):
        Settings(**{**base, "fault_min_samples": 0})


def test_secrets_not_in_repr(settings):
    assert "test-client-secret" not in repr(settings)
    assert "test-bot-token" not in repr(settings)


def test_fixed_clock_advance(clock):
    assert clock.now() == datetime(2026, 11, 24, 15, 0, tzinfo=timezone.utc)
    clock.advance(timedelta(minutes=90))
    assert clock.now() == datetime(2026, 11, 24, 16, 30, tzinfo=timezone.utc)
    clock.set(datetime(2026, 12, 1, tzinfo=timezone.utc))
    assert clock.now() == datetime(2026, 12, 1, tzinfo=timezone.utc)
