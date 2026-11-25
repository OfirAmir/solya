from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
import respx
from sqlalchemy import select

from solya.db import session_scope
from solya.models import ApiLedger, Owner, RawResponse, Site
from solya.solaredge.budget import CreditBudget
from solya.solaredge.client import SolarEdgeClient
from solya.solaredge.errors import AuthExpired, BudgetExceeded, PermissionDenied, RateLimited

SITE = 3033498
BASE = "https://monitoringapi.solaredge.com/v2"


class FakeTokens:
    def __init__(self):
        self.refreshed = 0
        self.token = "tok"

    async def get_access_token(self, site_id):
        return self.token

    async def force_refresh(self, site_id):
        self.refreshed += 1
        self.token = "tok2"


@pytest.fixture
async def make_client(db, settings, clock):
    with session_scope(db) as s:
        s.add(Owner(id=1, display_name="o", telegram_chat_id=1, created_at=clock.now()))
        s.add(Site(se_site_id=SITE, owner_id=1, portal_name="p", tz="Asia/Jerusalem", created_at=clock.now()))
    tokens = FakeTokens()
    sleeps = []

    async def sleep(seconds):  # R22 spacing: advance the fixed clock instead of waiting
        sleeps.append(seconds)
        clock.advance(timedelta(seconds=seconds))

    budget = CreditBudget(db, settings, clock, sleep=sleep)
    async with httpx.AsyncClient() as http:
        client = SolarEdgeClient(http, settings, tokens, budget, db, clock)
        client.tokens = tokens
        client.budget = budget
        client.sleeps = sleeps
        yield client


def raws(db):
    with session_scope(db) as s:
        return [SimpleNamespace(status=r.status, body=r.body, params=r.params) for r in s.scalars(select(RawResponse))]


async def test_bearer_and_raw_stored(make_client, db, load_fixture):
    with respx.mock:
        route = respx.get(f"{BASE}/sites/{SITE}").mock(
            return_value=httpx.Response(200, json=load_fixture("solaredge/site_farm.json"))
        )
        info = await make_client.get_site(SITE)
    assert route.calls[0].request.headers["authorization"] == "Bearer tok"
    assert info.site_id == SITE
    rows = raws(db)
    assert len(rows) == 1 and rows[0].status == 200 and rows[0].body["siteId"] == SITE
    assert "tok" not in str(rows[0].params)


async def test_overview_window_params(make_client, load_fixture):
    with respx.mock:
        route = respx.get(f"{BASE}/sites/{SITE}/overview").mock(
            return_value=httpx.Response(200, json=load_fixture("solaredge/overview_farm_20261124.json"))
        )
        ov = await make_client.get_overview(SITE, date(2026, 11, 24), "Asia/Jerusalem")
    q = route.calls[0].request.url.params
    assert q["from"] == "2026-11-23T22:00:00Z"
    assert q["to"] == "2026-11-24T22:00:00Z"
    assert ov.production_wh is not None


async def test_devices_repeated_types_param(make_client, load_fixture):
    with respx.mock:
        route = respx.get(f"{BASE}/sites/{SITE}/devices").mock(
            return_value=httpx.Response(200, json=load_fixture("solaredge/devices_farm.json"))
        )
        devices = await make_client.get_devices(SITE)
    assert "types=INVERTER&types=METER" in str(route.calls[0].request.url)
    assert len(devices) == 2


async def test_inverter_window_clamped_to_24h(make_client, load_fixture):
    with respx.mock:
        route = respx.get(f"{BASE}/sites/{SITE}/inverters/status").mock(
            return_value=httpx.Response(200, json=load_fixture("solaredge/inverters_status_ok.json"))
        )
        await make_client.get_inverter_status(
            SITE, "Asia/Jerusalem",
            datetime(2026, 11, 20, tzinfo=timezone.utc), datetime(2026, 11, 24, 12, tzinfo=timezone.utc),
        )
    q = route.calls[0].request.url.params
    assert q["from"] == "2026-11-23T12:00:00Z" and q["to"] == "2026-11-24T12:00:00Z"
    assert q["resolution"] == "HOUR"


async def test_401_refreshes_once_then_ok(make_client, db, load_fixture):
    with respx.mock:
        route = respx.get(f"{BASE}/sites/{SITE}").mock(
            side_effect=[httpx.Response(401), httpx.Response(200, json=load_fixture("solaredge/site_farm.json"))]
        )
        await make_client.get_site(SITE)
    assert make_client.tokens.refreshed == 1
    assert route.calls[1].request.headers["authorization"] == "Bearer tok2"
    assert [r.status for r in raws(db)] == [401, 200]
    with session_scope(db) as s:
        assert [r.credited for r in s.scalars(select(ApiLedger).order_by(ApiLedger.id))] == [False, True]


async def test_401_twice_is_auth_expired(make_client):
    with respx.mock:
        respx.get(f"{BASE}/sites/{SITE}").mock(return_value=httpx.Response(401))
        with pytest.raises(AuthExpired):
            await make_client.get_site(SITE)
    assert make_client.tokens.refreshed == 1


async def test_429_maps_rate_limited(make_client):
    with respx.mock:
        respx.get(f"{BASE}/sites/{SITE}").mock(
            return_value=httpx.Response(429, headers={"retry-after": "30"})
        )
        with pytest.raises(RateLimited) as ei:
            await make_client.get_site(SITE)
    assert ei.value.retry_after == 30
    assert ei.value.month_exhausted is False


async def test_429_short_retry_after_retries_once(make_client, load_fixture):
    """R22: a burst 429 with a small Retry-After is retried once after waiting."""
    with respx.mock:
        route = respx.get(f"{BASE}/sites/{SITE}").mock(side_effect=[
            httpx.Response(429, headers={"retry-after": "5"}),
            httpx.Response(200, json=load_fixture("solaredge/site_farm.json")),
        ])
        info = await make_client.get_site(SITE)
    assert info.site_id == SITE
    assert route.call_count == 2
    assert 5 in make_client.sleeps


async def test_429_retried_only_once(make_client):
    with respx.mock:
        route = respx.get(f"{BASE}/sites/{SITE}").mock(
            return_value=httpx.Response(429, headers={"retry-after": "5"})
        )
        with pytest.raises(RateLimited):
            await make_client.get_site(SITE)
    assert route.call_count == 2


async def test_429_long_retry_after_not_retried(make_client):
    with respx.mock:
        route = respx.get(f"{BASE}/sites/{SITE}").mock(
            return_value=httpx.Response(429, headers={"retry-after": "86400"})
        )
        with pytest.raises(RateLimited):
            await make_client.get_site(SITE)
    assert route.call_count == 1


async def test_429_month_header_zero_is_exhausted_not_retried(make_client):
    with respx.mock:
        route = respx.get(f"{BASE}/sites/{SITE}").mock(return_value=httpx.Response(
            429, headers={"retry-after": "30", "x-ratelimit-remaining-month": "0"}))
        with pytest.raises(RateLimited) as ei:
            await make_client.get_site(SITE)
    assert ei.value.month_exhausted is True
    assert route.call_count == 1


async def test_429_month_exhausted(make_client):
    with respx.mock:
        respx.get(f"{BASE}/sites/{SITE}").mock(
            return_value=httpx.Response(429, headers={"x-ratelimit-remaining-minute": "5"})
        )
        with pytest.raises(RateLimited) as ei:
            await make_client.get_site(SITE)
    assert ei.value.retry_after is None and ei.value.month_exhausted is True


async def test_403_permission_denied(make_client):
    with respx.mock:
        respx.get(f"{BASE}/sites/{SITE}").mock(return_value=httpx.Response(403, json={"error": "x"}))
        with pytest.raises(PermissionDenied):
            await make_client.get_site(SITE)


async def test_budget_exceeded_no_http(make_client, db, clock):
    with session_scope(db) as s:
        for _ in range(2000):
            s.add(ApiLedger(ts=clock.now(), site_id=SITE, endpoint="/x", http_status=200, credited=True))
    with respx.mock:
        route = respx.get(f"{BASE}/sites/{SITE}").mock(return_value=httpx.Response(200, json={}))
        with pytest.raises(BudgetExceeded):
            await make_client.get_site(SITE)
    assert not route.called
    assert make_client.budget.call_lock.locked() is False


async def test_site_body_allowlisted_before_store(make_client, db, load_fixture):
    raw = load_fixture("solaredge/site_farm.json")
    with respx.mock:
        respx.get(f"{BASE}/sites/{SITE}").mock(return_value=httpx.Response(200, json=raw))
        info = await make_client.get_site(SITE)
    assert info.timezone
    body = raws(db)[0].body
    assert set(body) <= {"siteId", "name", "peakPower", "lastUpdateTime", "activationStatus", "location"}
    assert set(body.get("location", {})) <= {"timezone", "latitude", "longitude"}
    dumped = str(body)
    for key in ("email", "accountId", "address", "city"):
        assert key not in dumped
