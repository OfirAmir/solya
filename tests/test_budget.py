from datetime import datetime, timedelta, timezone

import pytest

from solya.db import session_scope
from solya.models import ApiLedger
from solya.solaredge.budget import CreditBudget
from solya.solaredge.errors import BudgetExceeded


def seed(db, n, ts, credited=True, site_id=1):
    with session_scope(db) as s:
        s.add_all(
            ApiLedger(ts=ts, site_id=site_id, endpoint="sites", http_status=200, credited=credited)
            for _ in range(n)
        )


def make(db, settings, clock, sleep=None, **overrides):
    s = settings.model_copy(update=overrides)
    if sleep is None:
        async def sleep(_):  # noqa: E306
            pass
    return CreditBudget(db, s, clock, sleep=sleep)


async def test_optional_refused_at_90_percent(db, settings, clock):
    seed(db, 1800, clock.now() - timedelta(hours=2))
    b = make(db, settings, clock, budget_monthly_credits=2000, budget_per_minute=100000)
    with pytest.raises(BudgetExceeded):
        await b.acquire(1, "sites", "optional")
    await b.acquire(1, "sites", "essential")


async def test_essential_refused_at_cap(db, settings, clock):
    seed(db, 2000, clock.now() - timedelta(hours=2))
    b = make(db, settings, clock, budget_monthly_credits=2000, budget_per_minute=100000)
    with pytest.raises(BudgetExceeded):
        await b.acquire(1, "sites", "essential")


def test_cycle_starts_at_billing_day(db, settings, clock):
    clock.set(datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc))
    seed(db, 3, datetime(2026, 11, 16, 23, 59, tzinfo=timezone.utc))
    seed(db, 2, datetime(2026, 11, 17, 0, 0, tzinfo=timezone.utc))
    b = make(db, settings, clock, budget_billing_day=17)
    assert b.used_this_cycle() == 2


def test_cycle_billing_day_clamped_to_month_length(db, settings, clock):
    clock.set(datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc))
    seed(db, 1, datetime(2026, 2, 27, 23, 0, tzinfo=timezone.utc))
    seed(db, 2, datetime(2026, 2, 28, 0, 0, tzinfo=timezone.utc))
    b = make(db, settings, clock, budget_billing_day=31)
    assert b.used_this_cycle() == 2


def test_cycle_before_billing_day_uses_previous_month(db, settings, clock):
    clock.set(datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc))
    seed(db, 1, datetime(2025, 12, 14, 23, 0, tzinfo=timezone.utc))
    seed(db, 4, datetime(2025, 12, 15, 0, 0, tzinfo=timezone.utc))
    b = make(db, settings, clock, budget_billing_day=15)
    assert b.used_this_cycle() == 4


async def test_per_minute_waits(db, settings, clock):
    seed(db, 8, clock.now() - timedelta(seconds=10))
    calls = []

    async def fake_sleep(seconds):
        calls.append(seconds)
        clock.advance(timedelta(seconds=seconds))

    b = make(db, settings, clock, sleep=fake_sleep, budget_per_minute=8)
    await b.acquire(1, "sites", "essential")
    assert calls and calls[0] > 0
    assert calls[0] == pytest.approx(61 - 10)


def test_failed_calls_not_credited(db, settings, clock):
    b = make(db, settings, clock)
    b.record(1, "sites", 500, credited=False)
    assert b.used_this_cycle() == 0
    b.record(1, "sites", 200, credited=True)
    assert b.used_this_cycle() == 1


def test_used_by_site(db, settings, clock):
    seed(db, 2, clock.now(), site_id=1)
    seed(db, 3, clock.now(), site_id=2)
    b = make(db, settings, clock)
    assert b.used_by_site(2, clock.now() - timedelta(days=1)) == 3


async def test_calls_spaced_by_min_interval(db, settings, clock):
    """R22: a call right after another waits out the minimum spacing (burst limit)."""
    assert settings.budget_min_interval_seconds == 7.5
    seed(db, 1, clock.now() - timedelta(seconds=2))
    calls = []

    async def fake_sleep(seconds):
        calls.append(seconds)
        clock.advance(timedelta(seconds=seconds))

    b = make(db, settings, clock, sleep=fake_sleep)
    await b.acquire(1, "sites", "essential")
    assert calls == [pytest.approx(5.5)]


async def test_spacing_counts_uncredited_calls(db, settings, clock):
    seed(db, 1, clock.now() - timedelta(seconds=1), credited=False)
    calls = []

    async def fake_sleep(seconds):
        calls.append(seconds)
        clock.advance(timedelta(seconds=seconds))

    b = make(db, settings, clock, sleep=fake_sleep)
    await b.acquire(1, "sites", "essential")
    assert calls == [pytest.approx(6.5)]


async def test_no_wait_when_last_call_is_old_enough(db, settings, clock):
    seed(db, 1, clock.now() - timedelta(seconds=8))
    calls = []

    async def fake_sleep(seconds):
        calls.append(seconds)

    b = make(db, settings, clock, sleep=fake_sleep)
    await b.acquire(1, "sites", "essential")
    assert calls == []
