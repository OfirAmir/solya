import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest
import respx
from sqlalchemy import select

from solya.clock import FixedClock
from solya.db import session_scope
from solya.models import ApiLedger, DailyMetric, Owner, RawResponse, Site
from solya.services import answers, backfill
from solya.solaredge.budget import CreditBudget
from solya.solaredge.client import SolarEdgeClient

SITE = 3033498
BASE = "https://monitoringapi.solaredge.com/v2"
NOW = datetime(2026, 11, 26, 15, 0, tzinfo=timezone.utc)
TZ = "Asia/Jerusalem"
EARLIER = datetime(2026, 11, 10, 18, 0, tzinfo=timezone.utc)


class FakeTokens:
    async def get_access_token(self, site_id):
        return "tok"

    async def force_refresh(self, site_id):
        pass


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(NOW)


def seed(db, basis="production", tariff="0.45", monitored=True, status="active"):
    with session_scope(db) as s:
        s.add(Owner(id=1, display_name="אורן", telegram_chat_id=1, created_at=NOW))
        s.add(Site(se_site_id=SITE, owner_id=1, portal_name="p", label_he="משק", tz=TZ,
                   tariff_ils_per_kwh=Decimal(tariff) if tariff else None,
                   income_basis=basis, monitored=monitored, status=status, created_at=NOW))


@pytest.fixture
async def client(db, settings, clock):
    async def sleep(seconds):
        clock.advance(timedelta(seconds=seconds))

    budget = CreditBudget(db, settings, clock, sleep=sleep)
    async with httpx.AsyncClient() as http:
        yield SolarEdgeClient(http, settings, FakeTokens(), budget, db, clock)


def energy_body(days, kwh=10.0):
    return {
        "period": {}, "resolution": "DAY", "unit": "KWH",
        "values": [{"timestamp": f"{d.isoformat()}T00:00:00+02:00", "value": kwh + d.day}
                   for d in days],
    }


def meter_body(days, kwh=4.0):
    return {"meters": {"M1": {"exportEnergy": {"unit": "KWH", "values": [
        {"timestamp": f"{d.isoformat()}T00:00:00+02:00", "value": kwh} for d in days]}}},
            "resolution": "DAY"}


NOV = [date(2026, 11, d) for d in range(1, 27)]  # includes today, which must never be written


def mock_api(energy=None, meter=None):
    e = respx.get(f"{BASE}/sites/{SITE}/energy").mock(
        return_value=energy if isinstance(energy, httpx.Response)
        else httpx.Response(200, json=energy or energy_body(NOV)))
    m = respx.get(f"{BASE}/sites/{SITE}/meters/telemetry").mock(
        return_value=meter if isinstance(meter, httpx.Response)
        else httpx.Response(200, json=meter or meter_body(NOV)))
    return e, m


def rows(db):
    with session_scope(db) as s:
        out = {r.local_date: r for r in s.scalars(select(DailyMetric))}
        s.expunge_all()
        return out


def existing(db, day, wh=777.0):
    with session_scope(db) as s:
        s.add(DailyMetric(site_id=SITE, local_date=day, production_wh=wh, export_wh=None,
                          income_ils=Decimal("1.11"), comm_state="ok", as_of=EARLIER,
                          computed_at=EARLIER))


async def test_backfill_fills_only_missing_past_days(db, client):
    seed(db)
    existing(db, date(2026, 11, 10))
    existing(db, date(2026, 11, 26), wh=555.0)
    with respx.mock:
        e, m = mock_api()
        n = await backfill.backfill_month(client, db, SITE, NOW)
    q = e.calls[0].request.url.params
    assert (q["from"], q["to"], q["resolution"]) == (
        "2026-10-31T22:00:00Z", "2026-11-25T22:00:00Z", "DAY")
    assert m.calls[0].request.url.params["resolution"] == "DAY"
    assert n == 24
    got = rows(db)
    assert set(got) == set(NOV)
    for d in NOV:
        if d in (date(2026, 11, 10), date(2026, 11, 26)):
            assert got[d].comm_state == "ok" and got[d].income_ils == Decimal("1.11")
            assert got[d].as_of == EARLIER
            continue
        r = got[d]
        assert r.comm_state == "backfill"
        assert r.production_wh == (10.0 + d.day) * 1000
        assert r.export_wh == 4000.0
        assert r.income_ils == (Decimal(str(10.0 + d.day)) * Decimal("0.45")).quantize(Decimal("0.01"))
        assert r.as_of == NOW and r.computed_at == NOW
    assert got[date(2026, 11, 10)].production_wh == 777.0
    assert got[date(2026, 11, 26)].production_wh == 555.0
    with session_scope(db) as s:  # credits recorded: 2 calls, optional priority
        assert len(list(s.scalars(select(ApiLedger)))) == 2


async def test_backfill_once_per_month(db, client, clock):
    seed(db)
    with respx.mock:
        e, m = mock_api()
        assert await backfill.backfill_month(client, db, SITE, NOW) == 25
    with session_scope(db) as s:  # even with the rows gone, this month was already tried
        for r in s.scalars(select(DailyMetric)):
            s.delete(r)
    later = NOW + timedelta(days=2)
    with respx.mock:
        e, m = mock_api()
        assert await backfill.backfill_month(client, db, SITE, later) == 0
        assert e.call_count == 0 and m.call_count == 0


async def test_backfill_retries_next_month(db, client, clock):
    seed(db)
    with session_scope(db) as s:  # an attempt last month does not count for this one
        s.add(RawResponse(site_id=SITE, endpoint=f"/sites/{SITE}/energy", params={}, status=200,
                          body={}, fetched_at=datetime(2026, 10, 31, 21, 0, tzinfo=timezone.utc)))
    with respx.mock:
        e, _ = mock_api()
        assert await backfill.backfill_month(client, db, SITE, NOW) == 25
        assert e.call_count == 1


async def test_backfill_first_of_month_noop(db, client):
    seed(db)
    first = datetime(2026, 11, 30, 22, 30, tzinfo=timezone.utc)  # 1 Dec 00:30 local
    with respx.mock:
        e, m = mock_api()
        assert await backfill.backfill_month(client, db, SITE, first) == 0
        assert e.call_count == 0 and m.call_count == 0
    assert rows(db) == {}


async def test_backfill_skips_inactive_or_unmonitored(db, client):
    seed(db, monitored=False)
    with respx.mock:
        e, m = mock_api()
        assert await backfill.backfill_month(client, db, SITE, NOW) == 0
        assert e.call_count == 0


async def test_backfill_export_income(db, client):
    seed(db, basis="export", tariff="0.48")
    with respx.mock:
        mock_api()
        assert await backfill.backfill_month(client, db, SITE, NOW) == 25
    r = rows(db)[date(2026, 11, 3)]
    assert r.export_wh == 4000.0 and r.income_ils == Decimal("1.92")


async def test_backfill_export_income_no_meter(db, client):
    seed(db, basis="export", tariff="0.48")
    with respx.mock:
        mock_api(meter=httpx.Response(404))
        assert await backfill.backfill_month(client, db, SITE, NOW) == 25
    r = rows(db)[date(2026, 11, 3)]
    assert r.production_wh == 13000.0
    assert r.export_wh is None and r.income_ils is None


async def test_backfill_no_tariff_no_income(db, client):
    seed(db, tariff=None)
    with respx.mock:
        mock_api()
        await backfill.backfill_month(client, db, SITE, NOW)
    assert rows(db)[date(2026, 11, 3)].income_ils is None


async def test_month_answer_counts_backfill_rows(db, client):
    seed(db)
    with respx.mock:
        mock_api()
        await backfill.backfill_month(client, db, SITE, NOW)
    with db() as s:
        owner = s.get(Owner, 1)
        out = answers.answer_month(s, owner, NOW)
    assert "ב־25 ימים" in out


async def test_backfill_soon_never_raises(db, settings, clock, client, caplog):
    from solya.messaging.outbox import Outbox
    from solya.rules.ray import NullRay
    from solya.services.reporting import Reporter

    from conftest import FakeChannel

    seed(db)
    reporter = Reporter(db, client, FakeTokens(), Outbox(db, clock), FakeChannel(), clock,
                        settings, NullRay(), lambda sid, at: None)
    with respx.mock:
        mock_api(energy=httpx.Response(500, json={"secret": "x"}))
        with caplog.at_level(logging.WARNING):
            reporter.backfill_soon(SITE)
            await asyncio.gather(*list(reporter._background))
    assert rows(db) == {}
    assert "ApiError" in caplog.text and "secret" not in caplog.text

    with respx.mock:  # final fix 3a: a failed /energy is not "tried": the next start retries
        e, _ = mock_api()
        reporter.backfill_soon(SITE)
        await asyncio.gather(*list(reporter._background))
        assert e.call_count == 1
    assert len(rows(db)) == 25


def credited(db):
    with session_scope(db) as s:
        return [(r.endpoint.rsplit("/", 1)[1], r.http_status)
                for r in s.scalars(select(ApiLedger).where(ApiLedger.credited.is_(True)))]


async def test_backfill_failed_energy_is_retried(db, client):
    """Final fix 3a: only a 200 /energy answer marks the month as tried."""
    seed(db)
    with respx.mock:
        mock_api(energy=httpx.Response(500))
        with pytest.raises(Exception):
            await backfill.backfill_month(client, db, SITE, NOW)
    assert rows(db) == {}
    with respx.mock:
        e, m = mock_api()
        assert await backfill.backfill_month(client, db, SITE, NOW + timedelta(hours=1)) == 25
        assert e.call_count == 1 and m.call_count == 1
    assert credited(db) == [("energy", 200), ("telemetry", 200)]


async def test_backfill_export_meter_failure_writes_nothing_and_retries(db, client):
    """Final fix 3b: for an export-basis site a failed meter call (other than 404) writes
    no rows and leaves the month untried. The retry reuses this month's stored /energy
    answer, so successful calls stay at two credits for the month."""
    seed(db, basis="export", tariff="0.48")
    with respx.mock:
        e, m = mock_api(meter=httpx.Response(503))
        assert await backfill.backfill_month(client, db, SITE, NOW) == 0
        assert e.call_count == 1 and m.call_count == 1
    assert rows(db) == {}
    later = NOW + timedelta(hours=1)
    with respx.mock:
        e, m = mock_api()
        assert await backfill.backfill_month(client, db, SITE, later) == 25
        assert e.call_count == 0 and m.call_count == 1
    r = rows(db)[date(2026, 11, 3)]
    assert r.production_wh == 13000.0 and r.export_wh == 4000.0
    assert r.income_ils == Decimal("1.92")
    with respx.mock:  # now tried: no more calls this month
        e, m = mock_api()
        assert await backfill.backfill_month(client, db, SITE, later + timedelta(days=1)) == 0
        assert e.call_count == 0 and m.call_count == 0
    assert credited(db) == [("energy", 200), ("telemetry", 200)]


async def test_backfill_export_no_meter_counts_as_tried(db, client):
    seed(db, basis="export", tariff="0.48")
    with respx.mock:
        mock_api(meter=httpx.Response(404))
        assert await backfill.backfill_month(client, db, SITE, NOW) == 25
    with respx.mock:
        e, m = mock_api()
        assert await backfill.backfill_month(client, db, SITE, NOW + timedelta(days=1)) == 0
        assert e.call_count == 0 and m.call_count == 0


async def test_backfill_production_meter_failure_keeps_production(db, client):
    """A production-basis site needs no export for income: keep the rows, month tried."""
    seed(db)
    with respx.mock:
        mock_api(meter=httpx.Response(503))
        assert await backfill.backfill_month(client, db, SITE, NOW) == 25
    assert rows(db)[date(2026, 11, 3)].export_wh is None
    with respx.mock:
        e, m = mock_api()
        assert await backfill.backfill_month(client, db, SITE, NOW + timedelta(days=1)) == 0
        assert e.call_count == 0 and m.call_count == 0
