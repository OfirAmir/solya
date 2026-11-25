import asyncio
import json
import logging
from datetime import date, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
from sqlalchemy import func, select

from solya.crypto import TokenCipher
from solya.db import session_scope
from solya.models import ApiLedger, DailyMetric, Owner, Site, SiteToken
from solya.solaredge.budget import CreditBudget
from solya.solaredge.errors import AuthExpired, TransientAuthError
from solya.solaredge.oauth import (
    TokenManager,
    TokenPair,
    build_authorize_url,
    exchange_code,
)

SITE = 4242
TOKEN_URL = "https://monitoringapi.solaredge.com/v2/oauth2/token"


def ok(access="a2", refresh="r2", expires_in=3600):
    return httpx.Response(200, json={"access_token": access, "refresh_token": refresh, "expires_in": expires_in})


@pytest.fixture
def cipher(settings):
    return TokenCipher(settings.token_encryption_key.get_secret_value())


@pytest.fixture
async def http():
    async with httpx.AsyncClient() as c:
        yield c


@pytest.fixture
def reauth_calls():
    return []


@pytest.fixture
def mgr(db, settings, cipher, http, clock, reauth_calls):
    async def on_reauth(site_id):
        reauth_calls.append(site_id)

    async def sleep(seconds):  # R22 spacing: advance the fixed clock instead of waiting
        clock.advance(timedelta(seconds=seconds))

    budget = CreditBudget(db, settings, clock, sleep=sleep)
    return TokenManager(db, settings, cipher, http, budget, clock, on_reauth)


def seed(db, mgr, clock, expires_in):
    with session_scope(db) as s:
        s.add(Owner(id=1, display_name="o", telegram_chat_id=1, created_at=clock.now()))
        s.add(Site(se_site_id=SITE, owner_id=1, portal_name="p", tz="Asia/Jerusalem", created_at=clock.now()))
        s.add(DailyMetric(site_id=SITE, local_date=date(2026, 11, 23), production_wh=1.0,
                          comm_state="ok", as_of=clock.now(), computed_at=clock.now()))
    mgr.store_new_grant(SITE, TokenPair("a1", "r1", expires_in))


def row(db):
    with session_scope(db) as s:
        r = s.get(SiteToken, SITE)
        s.expunge(r)
        return r


def test_authorize_url_params(settings):
    url = build_authorize_url(settings, "n123")
    assert " " not in url and "scope=SITE_DATA%20DEVICE_DATA" in url
    q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    assert q == {
        "client_id": "test-client-id",
        "scope": "SITE_DATA DEVICE_DATA",
        "redirect_uri": "https://solya.example/oauth/callback",
        "state": "n123",
        "external_id": "n123",
        "access_duration": "24",
    }


@respx.mock
async def test_exchange_code_json_body(http, settings):
    route = respx.post(TOKEN_URL).mock(return_value=ok("a", "r"))
    pair = await exchange_code(http, settings, "c1")
    req = route.calls.last.request
    assert req.headers["content-type"].startswith("application/json")
    assert json.loads(req.content) == {
        "grant_type": "authorization_code",
        "code": "c1",
        "client_id": "test-client-id",
        "client_secret": "test-client-secret",
    }
    assert (pair.access_token, pair.refresh_token, pair.expires_in) == ("a", "r", 3600)
    assert repr(pair) == "TokenPair(access_token=<redacted>, refresh_token=<redacted>, expires_in=3600)"


@respx.mock
async def test_get_access_token_cached(db, mgr, clock):
    seed(db, mgr, clock, 3600)
    route = respx.post(TOKEN_URL).mock(return_value=ok())
    assert await mgr.get_access_token(SITE) == "a1"
    assert route.call_count == 0


@respx.mock
async def test_refresh_rotates_and_persists(db, mgr, clock, cipher):
    seed(db, mgr, clock, 60)
    route = respx.post(TOKEN_URL).mock(return_value=ok("a2", "r2", 3600))
    assert await mgr.get_access_token(SITE) == "a2"
    assert json.loads(route.calls.last.request.content)["refresh_token"] == "r1"
    r = row(db)
    assert cipher.decrypt(r.refresh_enc) == "r2"
    assert cipher.decrypt(r.previous_refresh_enc) == "r1"
    assert cipher.decrypt(r.access_enc) == "a2"
    assert r.access_expires_at == clock.now() + timedelta(seconds=3600)
    with session_scope(db) as s:
        rows = list(s.execute(select(ApiLedger.credited, ApiLedger.endpoint)))
    assert rows == [(True, "oauth2/token")]


@respx.mock
async def test_refresh_transient_keeps_pair(db, mgr, clock, cipher):
    seed(db, mgr, clock, 60)
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(503))
    with pytest.raises(TransientAuthError):
        await mgr.get_access_token(SITE)
    assert cipher.decrypt(row(db).refresh_enc) == "r1"
    with session_scope(db) as s:
        assert s.get(Site, SITE).status == "active"


@respx.mock
async def test_refresh_timeout_is_transient(db, mgr, clock):
    seed(db, mgr, clock, 60)
    respx.post(TOKEN_URL).mock(side_effect=httpx.ReadTimeout("t"))
    with pytest.raises(TransientAuthError):
        await mgr.get_access_token(SITE)


@respx.mock
async def test_refresh_invalid_grant_marks_reauth(db, mgr, clock, reauth_calls, cipher):
    seed(db, mgr, clock, 60)
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(400, json={"error": "invalid_grant"}))
    with pytest.raises(AuthExpired):
        await mgr.get_access_token(SITE)
    with pytest.raises(AuthExpired):
        await mgr.get_access_token(SITE)
    with session_scope(db) as s:
        assert s.get(Site, SITE).status == "reauth_needed"
        assert s.get(DailyMetric, (SITE, date(2026, 11, 23))) is not None
    assert reauth_calls == [SITE]
    assert cipher.decrypt(row(db).refresh_enc) == "r1"


@respx.mock
async def test_refresh_falls_back_to_previous(db, mgr, clock, cipher):
    seed(db, mgr, clock, 60)
    respx.post(TOKEN_URL).mock(return_value=ok("a2", "r2"))
    await mgr.get_access_token(SITE)  # r1 -> r2, previous = r1
    clock.advance(timedelta(hours=2))
    respx.reset()
    route = respx.post(TOKEN_URL).mock(
        side_effect=[httpx.Response(400, json={"error": "invalid_grant"}), ok("a3", "r3")]
    )
    assert await mgr.get_access_token(SITE) == "a3"
    assert route.call_count == 2
    sent = [json.loads(c.request.content)["refresh_token"] for c in route.calls]
    assert sent == ["r2", "r1"]
    r = row(db)
    assert cipher.decrypt(r.refresh_enc) == "r3"
    assert cipher.decrypt(r.previous_refresh_enc) == "r1"  # the working token stays as fallback


@respx.mock
async def test_refresh_429_retried_once(db, mgr, clock, cipher):
    """R22: a burst 429 on the token endpoint is retried once; the rotation persists."""
    seed(db, mgr, clock, 60)
    route = respx.post(TOKEN_URL).mock(
        side_effect=[httpx.Response(429, headers={"retry-after": "4"}), ok("a2", "r2")]
    )
    assert await mgr.get_access_token(SITE) == "a2"
    assert route.call_count == 2
    sent = [json.loads(c.request.content)["refresh_token"] for c in route.calls]
    assert sent == ["r1", "r1"]
    assert cipher.decrypt(row(db).refresh_enc) == "r2"


@respx.mock
async def test_concurrent_refresh_single_call(db, mgr, clock):
    seed(db, mgr, clock, 60)
    route = respx.post(TOKEN_URL).mock(return_value=ok("a2", "r2"))
    a, b = await asyncio.gather(mgr.get_access_token(SITE), mgr.get_access_token(SITE))
    assert a == b == "a2"
    assert route.call_count == 1


@respx.mock
async def test_tokens_not_logged(db, mgr, clock, caplog):
    seed(db, mgr, clock, 60)
    respx.post(TOKEN_URL).mock(return_value=ok("secret-access-2", "secret-refresh-2"))
    with caplog.at_level(logging.DEBUG):
        await mgr.get_access_token(SITE)
    for t in ("secret-access-2", "secret-refresh-2", "a1", "r1", "test-client-secret"):
        assert t not in caplog.text


@respx.mock
async def test_reauth_callback_failure_still_raises_auth_expired(db, settings, cipher, http, clock):
    async def boom(_):
        raise RuntimeError("telegram down")

    m = TokenManager(db, settings, cipher, http, CreditBudget(db, settings, clock), clock, boom)
    seed(db, m, clock, 60)
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(400, json={"error": "invalid_grant"}))
    with pytest.raises(AuthExpired):
        await m.get_access_token(SITE)
    with session_scope(db) as s:
        assert s.get(Site, SITE).status == "reauth_needed"


@respx.mock
async def test_reauth_needed_short_circuits(db, mgr, clock):
    seed(db, mgr, clock, 60)
    with session_scope(db) as s:
        s.get(Site, SITE).status = "reauth_needed"
    route = respx.post(TOKEN_URL).mock(return_value=ok())
    with pytest.raises(AuthExpired):
        await mgr.get_access_token(SITE)
    with pytest.raises(AuthExpired):
        await mgr.force_refresh(SITE)
    assert route.call_count == 0
    with session_scope(db) as s:
        assert s.scalar(select(func.count()).select_from(ApiLedger)) == 0


@respx.mock
async def test_refresh_holds_budget_call_lock(db, mgr, clock):
    seed(db, mgr, clock, 60)
    held = []

    def handler(request):
        held.append(mgr._budget.call_lock.locked())
        return ok()

    respx.post(TOKEN_URL).mock(side_effect=handler)
    await mgr.get_access_token(SITE)
    assert held == [True]
    assert not mgr._budget.call_lock.locked()


@respx.mock
async def test_store_new_grant_reactivates_site(db, mgr, clock):
    seed(db, mgr, clock, 60)
    with session_scope(db) as s:
        s.get(Site, SITE).status = "reauth_needed"
    route = respx.post(TOKEN_URL).mock(return_value=ok())
    mgr.store_new_grant(SITE, TokenPair("a9", "r9", 3600))
    with session_scope(db) as s:
        assert s.get(Site, SITE).status == "active"
    assert await mgr.get_access_token(SITE) == "a9"
    assert route.call_count == 0
