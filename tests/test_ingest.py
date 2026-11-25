from datetime import date, timedelta

import httpx
import pytest
import respx

from solya.db import session_scope
from solya.ingest import pull_site
from solya.models import ApiLedger, Owner, Site
from solya.solaredge.budget import CreditBudget
from solya.solaredge.client import SolarEdgeClient
from solya.solaredge.errors import AuthExpired

SITE = 3033498
BASE = "https://monitoringapi.solaredge.com/v2"
DAY = date(2026, 11, 24)


class FakeTokens:
    async def get_access_token(self, site_id):
        return "tok"

    async def force_refresh(self, site_id):
        pass


def _advance(clock):
    async def sleep(seconds):
        clock.advance(timedelta(seconds=seconds))

    return sleep


@pytest.fixture
async def client(db, settings, clock):
    with session_scope(db) as s:
        s.add(Owner(id=1, display_name="o", telegram_chat_id=1, created_at=clock.now()))
        s.add(Site(se_site_id=SITE, owner_id=1, portal_name="p", tz="Asia/Jerusalem", created_at=clock.now()))
    async with httpx.AsyncClient() as http:
        yield SolarEdgeClient(http, settings, FakeTokens(), CreditBudget(db, settings, clock, sleep=_advance(clock)), db, clock)


def mock_all(load_fixture, overview=None):
    routes = {
        "site": respx.get(f"{BASE}/sites/{SITE}").mock(
            return_value=httpx.Response(200, json=load_fixture("solaredge/site_farm.json"))),
        "overview": respx.get(f"{BASE}/sites/{SITE}/overview").mock(
            return_value=overview or httpx.Response(200, json=load_fixture("solaredge/overview_farm_20261124.json"))),
        "alerts": respx.get(f"{BASE}/sites/{SITE}/alerts").mock(
            return_value=httpx.Response(200, json=load_fixture("solaredge/alerts_fan.json"))),
        "inv": respx.get(f"{BASE}/sites/{SITE}/inverters/status").mock(
            return_value=httpx.Response(200, json=load_fixture("solaredge/inverters_status_ok.json"))),
        "life": respx.get(f"{BASE}/sites/{SITE}/lifetime-energy").mock(
            return_value=httpx.Response(200, json=load_fixture("solaredge/lifetime_farm.json"))),
        "dev": respx.get(f"{BASE}/sites/{SITE}/devices").mock(
            return_value=httpx.Response(200, json=load_fixture("solaredge/devices_farm.json"))),
    }
    return routes


async def test_evening_pull_full_bundle(client, db, clock, load_fixture):
    with respx.mock:
        mock_all(load_fixture)
        b = await pull_site(client, db, clock, SITE, "evening", DAY)
    assert b.errors == {}
    assert b.site and b.overview and b.alerts and b.inverters and b.lifetime
    assert b.pulled_at == clock.now()
    with session_scope(db) as s:
        row = s.get(Site, SITE)
        assert row.expected_inverters == ["7E04E44D-B3"]
        assert row.devices_fetched_at == clock.now()
        assert row.last_update_time is not None and row.lat is not None


async def test_status_window_is_midnight_to_now(client, db, clock, load_fixture):
    with respx.mock:
        r = mock_all(load_fixture)
        await pull_site(client, db, clock, SITE, "evening", DAY)
    q = r["inv"].calls[0].request.url.params
    assert q["from"] == "2026-11-23T22:00:00Z" and q["to"] == "2026-11-24T15:00:00Z"


async def test_midday_skips_lifetime_uses_optional(client, db, clock, load_fixture):
    with respx.mock:
        r = mock_all(load_fixture)
        b = await pull_site(client, db, clock, SITE, "midday", DAY)
    assert b.lifetime is None and not r["life"].called
    seen = []
    orig = client._budget.acquire

    async def spy(site_id, endpoint, priority):
        seen.append(priority)
        await orig(site_id, endpoint, priority)

    client._budget.acquire = spy
    with respx.mock:
        mock_all(load_fixture)
        await pull_site(client, db, clock, SITE, "midday", DAY)
    assert seen and set(seen) == {"optional"}


async def test_devices_not_refetched_when_fresh(client, db, clock, load_fixture):
    with respx.mock:
        mock_all(load_fixture)
        await pull_site(client, db, clock, SITE, "evening", DAY)
    with respx.mock:
        r = mock_all(load_fixture)
        await pull_site(client, db, clock, SITE, "evening", DAY)
    assert not r["dev"].called


async def test_overview_500_recorded_not_fatal(client, db, clock, load_fixture):
    with respx.mock:
        mock_all(load_fixture, overview=httpx.Response(500))
        b = await pull_site(client, db, clock, SITE, "evening", DAY)
    assert "overview" in b.errors and b.overview is None
    assert b.alerts is not None and b.inverters is not None


async def test_auth_expired_aborts(client, db, clock, load_fixture):
    with respx.mock:
        r = mock_all(load_fixture, overview=httpx.Response(401))
        with pytest.raises(AuthExpired):
            await pull_site(client, db, clock, SITE, "evening", DAY)
    assert not r["alerts"].called


async def test_malformed_overview_recorded_not_fatal(client, db, clock, load_fixture):
    bad = load_fixture("solaredge/overview_farm_20261124.json")
    bad["siteId"] = 999
    with respx.mock:
        mock_all(load_fixture, overview=httpx.Response(200, json=bad))
        b = await pull_site(client, db, clock, SITE, "evening", DAY)
    assert "overview" in b.errors and b.overview is None
    assert b.alerts is not None
