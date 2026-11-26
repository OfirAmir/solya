import logging
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

from solya.app import build_container, create_app
from solya.db import session_scope
from solya.models import ConnectRequest, Owner, Site, SiteToken
from solya.services import onboarding
from solya.text import he
from solya.text.rtl import rtl_text

from conftest import FakeChannel

SITE_ID = 4552265
CODE = "SECRET-AUTH-CODE-123"
ACCESS = "ACCESS-TOKEN-ABC"
REFRESH = "REFRESH-TOKEN-XYZ"


@pytest.fixture
def env(settings, clock, tmp_path):
    settings.database_url = f"sqlite:///{tmp_path}/sub/solya.db"
    channel = FakeChannel()

    async def sleep(seconds):
        clock.advance(timedelta(seconds=seconds))

    container = build_container(settings, clock=clock, channel=channel, budget_sleep=sleep)
    container.reporter.backfill_soon = lambda site_id: None  # no real backfill from callbacks
    with session_scope(container.session_factory) as s:
        owner = Owner(display_name="Dana", telegram_chat_id=555, active=True,
                      last_inbound_at=clock.now(), created_at=clock.now())
        s.add(owner)
        s.flush()
        url = onboarding.create_connect_request(s, owner.id, clock.now(), settings)
    token = url.rsplit("/", 1)[1]
    with TestClient(create_app(container, start_background=False), follow_redirects=False) as tc:
        yield tc, container, channel, token


def _nonce(container, token):
    with session_scope(container.session_factory) as s:
        return s.get(ConnectRequest, token).nonce


def test_healthz(env):
    tc, *_ = env
    assert tc.get("/healthz").json() == {"ok": True}


def test_connect_redirects_with_nonce(env):
    tc, container, _, token = env
    r = tc.get(f"/connect/{token}")
    assert r.status_code == 302
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["state"] == q["external_id"] == [_nonce(container, token)]
    assert "solya_connect=" + token in r.headers["set-cookie"]
    assert "httponly" in r.headers["set-cookie"].lower()


def test_connect_invalid_404(env):
    tc, *_ = env
    r = tc.get("/connect/nope")
    assert r.status_code == 404
    assert 'dir="rtl"' in r.text
    assert "הקישור אינו בתוקף. שלחו /connect בבוט כדי לקבל קישור חדש." in r.text


def _mock_se(site_json):
    respx.post("https://monitoringapi.solaredge.com/v2/oauth2/token").mock(
        return_value=httpx.Response(
            200, json={"access_token": ACCESS, "refresh_token": REFRESH, "expires_in": 3600}
        )
    )
    respx.get(f"https://monitoringapi.solaredge.com/v2/sites/{SITE_ID}").mock(
        return_value=httpx.Response(200, json=site_json)
    )


@respx.mock
def test_callback_happy_path(env, load_fixture):
    tc, container, channel, token = env
    _mock_se(load_fixture("solaredge/site_home.json"))
    nonce = _nonce(container, token)
    r = tc.get("/oauth/callback", params={"code": CODE, "site_id": SITE_ID,
                                          "external_id": nonce, "state": nonce})
    assert r.status_code == 200
    assert "המערכת חוברה בהצלחה. אפשר לחזור לטלגרם." in r.text
    with session_scope(container.session_factory) as s:
        site = s.get(Site, SITE_ID)
        assert "אורן סופר" in site.portal_name
        row = s.get(SiteToken, SITE_ID)
        assert ACCESS.encode() not in row.access_enc and REFRESH.encode() not in row.refresh_enc
        assert container.cipher.decrypt(row.access_enc) == ACCESS
        assert s.get(ConnectRequest, token).used_at is not None
    texts = [m.text for m in channel.sent]
    assert any("חוברה בהצלחה" in t for t in texts)
    assert any(rtl_text(he.WELCOME_1) in t for t in texts)


@respx.mock
def test_callback_schedules_todays_jobs(env, load_fixture):
    """I7/R26: a site connected in the afternoon gets tonight's pull and report."""
    tc, container, channel, token = env
    _mock_se(load_fixture("solaredge/site_home.json"))
    nonce = _nonce(container, token)
    r = tc.get("/oauth/callback", params={"code": CODE, "site_id": SITE_ID,
                                          "external_id": nonce, "state": nonce})
    assert r.status_code == 200
    with session_scope(container.session_factory) as s:
        owner_id = s.get(Site, SITE_ID).owner_id
    ids = {j.id for j in container.scheduler.scheduler.get_jobs()}
    assert {f"evening_pull:{SITE_ID}:2026-11-24", f"evening_report:{owner_id}:2026-11-24"} <= ids


def test_callback_error_param_400(env):
    tc, *_ = env
    r = tc.get("/oauth/callback", params={"error": "access_denied", "error_description": "no"})
    assert r.status_code == 400
    assert "החיבור בוטל או נכשל. אפשר לנסות שוב עם /connect." in r.text


def test_callback_error_is_logged_without_code(env, caplog):
    """I8: a failed callback says why in the log: parameter names and the error, never the code."""
    tc, *_ = env
    with caplog.at_level(logging.INFO, logger="solya"):
        r = tc.get("/oauth/callback", params={"error": "access_denied", "error_description": "user said no",
                                              "code": CODE, "state": "s"})
    assert r.status_code == 400
    assert "access_denied" in caplog.text and "user said no" in caplog.text
    assert "code" in caplog.text and "error_description" in caplog.text  # the names
    assert CODE not in caplog.text


def test_callback_missing_site_id_logged(env, caplog):
    tc, *_ = env
    with caplog.at_level(logging.INFO, logger="solya"):
        r = tc.get("/oauth/callback", params={"code": CODE, "external_id": "x"})
    assert r.status_code == 400
    assert "החיבור בוטל או נכשל" in r.text
    assert "site_id" in caplog.text and "external_id" in caplog.text
    assert CODE not in caplog.text


def test_callback_non_numeric_site_id_hebrew_400(env, caplog):
    tc, *_ = env
    with caplog.at_level(logging.INFO, logger="solya"):
        r = tc.get("/oauth/callback", params={"code": CODE, "site_id": "abc"})
    assert r.status_code == 400
    assert 'dir="rtl"' in r.text and "החיבור בוטל או נכשל" in r.text
    assert CODE not in caplog.text


@respx.mock
def test_callback_accepts_siteId_alias(env, load_fixture):
    tc, container, channel, token = env
    _mock_se(load_fixture("solaredge/site_home.json"))
    nonce = _nonce(container, token)
    r = tc.get("/oauth/callback", params={"code": CODE, "siteId": SITE_ID, "state": nonce})
    assert r.status_code == 200
    with session_scope(container.session_factory) as s:
        assert s.get(Site, SITE_ID) is not None


def test_callback_unknown_correlation_400(env):
    tc, *_ = env
    r = tc.get("/oauth/callback", params={"code": CODE, "site_id": SITE_ID,
                                          "external_id": "bogus", "state": "bogus"})
    assert r.status_code == 400


@respx.mock
def test_callback_code_not_logged(env, caplog, load_fixture):
    tc, container, _, token = env
    _mock_se(load_fixture("solaredge/site_home.json"))
    respx.get(f"https://monitoringapi.solaredge.com/v2/sites/{SITE_ID}").mock(
        return_value=httpx.Response(500)
    )
    nonce = _nonce(container, token)
    with caplog.at_level(logging.DEBUG):
        r = tc.get("/oauth/callback", params={"code": CODE, "site_id": SITE_ID,
                                              "external_id": nonce, "state": nonce})
    assert r.status_code == 502
    app_records = [r for r in caplog.records if r.name.startswith("solya")]
    assert app_records
    blob = " ".join(r.getMessage() + str(r.args) for r in app_records)
    for secret in (CODE, ACCESS, REFRESH, nonce):
        assert secret not in blob


def test_configure_logging_silences_http_loggers():
    from solya.app import configure_logging

    names = ("httpx", "httpcore", "telegram", "apscheduler")
    saved = {n: logging.getLogger(n).level for n in names}
    try:
        configure_logging()
        for n in names:
            assert logging.getLogger(n).getEffectiveLevel() >= logging.WARNING
    finally:
        for n, lvl in saved.items():
            logging.getLogger(n).setLevel(lvl)


def test_rejected_bot_token_not_printed(settings, clock, tmp_path, monkeypatch):
    """M1: PTB's InvalidToken message carries the token; startup must fail without it."""
    import traceback

    from telegram.error import InvalidToken
    from telegram.ext import Application

    settings.database_url = f"sqlite:///{tmp_path}/solya.db"
    secret = settings.telegram_bot_token.get_secret_value()

    async def rejected(self):
        raise InvalidToken(f"The token `{secret}` was rejected by the server.")

    monkeypatch.setattr(Application, "initialize", rejected)
    container = build_container(settings, clock=clock, channel=FakeChannel())
    with pytest.raises(BaseException) as ei:
        with TestClient(create_app(container, start_background=True)):
            pass
    shown = "".join(traceback.format_exception(ei.value))
    assert secret not in shown and secret.split(":")[1] not in shown
    assert "TELEGRAM_BOT_TOKEN" in shown


@respx.mock
def test_callback_reassigning_site_notifies_admin(env, load_fixture):
    """M4: connecting a site already registered to another owner alerts the admin."""
    tc, container, channel, token = env
    container.settings.admin_chat_ids = [999]
    with session_scope(container.session_factory) as s:
        other = Owner(display_name="Other", telegram_chat_id=777, active=True,
                      created_at=container.clock.now())
        s.add(other)
        s.flush()
        s.add(Site(se_site_id=SITE_ID, owner_id=other.id, portal_name="old",
                   tz="Asia/Jerusalem", created_at=container.clock.now()))
        other_id = other.id
    _mock_se(load_fixture("solaredge/site_home.json"))
    nonce = _nonce(container, token)
    r = tc.get("/oauth/callback", params={"code": CODE, "site_id": SITE_ID, "state": nonce})
    assert r.status_code == 200
    admin = [m for m in channel.sent if m.recipient == "999"]
    assert len(admin) == 1
    assert str(SITE_ID) in admin[0].text and str(other_id) in admin[0].text
    assert admin[0].text.startswith("האתר")


def _seed_victim_site(container):
    """Another owner's connected site, with its own stored tokens."""
    from solya.solaredge.oauth import TokenPair

    with session_scope(container.session_factory) as s:
        other = Owner(display_name="Victim", telegram_chat_id=777, active=True,
                      created_at=container.clock.now())
        s.add(other)
        s.flush()
        s.add(Site(se_site_id=SITE_ID, owner_id=other.id, portal_name="victim",
                   tz="Asia/Jerusalem", created_at=container.clock.now()))
        other_id = other.id
    container.tokens.store_new_grant(SITE_ID, TokenPair("VICTIM-ACCESS", "VICTIM-REFRESH", 3600))
    with session_scope(container.session_factory) as s:
        row = s.get(SiteToken, SITE_ID)
        before = (row.access_enc, row.refresh_enc, row.authorized_at)
    return other_id, before


def _assert_victim_untouched(container, other_id, before):
    with session_scope(container.session_factory) as s:
        row = s.get(SiteToken, SITE_ID)
        assert (row.access_enc, row.refresh_enc, row.authorized_at) == before
        assert container.cipher.decrypt(row.access_enc) == "VICTIM-ACCESS"
        assert container.cipher.decrypt(row.refresh_enc) == "VICTIM-REFRESH"
        site = s.get(Site, SITE_ID)
        assert site.owner_id == other_id and site.portal_name == "victim"


@respx.mock
def test_callback_token_without_site_access_stores_nothing(env, caplog):
    """M4: an edited site_id the new token cannot read (403) changes no stored token."""
    tc, container, channel, token = env
    other_id, before = _seed_victim_site(container)
    _mock_se({})
    route = respx.get(f"https://monitoringapi.solaredge.com/v2/sites/{SITE_ID}").mock(
        return_value=httpx.Response(403, json={"message": "forbidden"})
    )
    nonce = _nonce(container, token)
    with caplog.at_level(logging.DEBUG):
        r = tc.get("/oauth/callback", params={"code": CODE, "site_id": SITE_ID, "state": nonce})
    assert r.status_code == 502 and 'dir="rtl"' in r.text
    assert route.call_count == 1
    assert route.calls[0].request.headers["authorization"] == f"Bearer {ACCESS}"
    _assert_victim_untouched(container, other_id, before)
    with session_scope(container.session_factory) as s:
        assert s.get(ConnectRequest, token).used_at is None
    assert channel.sent == []
    blob = " ".join(rec.getMessage() for rec in caplog.records if rec.name.startswith("solya"))
    for secret in (CODE, ACCESS, REFRESH, nonce):
        assert secret not in blob


@respx.mock
def test_callback_site_id_mismatch_stores_nothing(env, load_fixture):
    """M4: SolarEdge answering for a different siteId is refused the same way."""
    tc, container, channel, token = env
    other_id, before = _seed_victim_site(container)
    body = load_fixture("solaredge/site_home.json")
    body["siteId"] = SITE_ID + 1
    _mock_se(body)
    nonce = _nonce(container, token)
    r = tc.get("/oauth/callback", params={"code": CODE, "site_id": SITE_ID, "state": nonce})
    assert r.status_code in (400, 502) and 'dir="rtl"' in r.text
    _assert_victim_untouched(container, other_id, before)
    with session_scope(container.session_factory) as s:
        assert s.get(SiteToken, SITE_ID + 1) is None
        assert s.get(Site, SITE_ID + 1) is None
    assert channel.sent == []


@respx.mock
def test_callback_new_site_403_creates_no_token_row(env):
    tc, container, channel, token = env
    _mock_se({})
    respx.get(f"https://monitoringapi.solaredge.com/v2/sites/{SITE_ID}").mock(
        return_value=httpx.Response(403)
    )
    nonce = _nonce(container, token)
    r = tc.get("/oauth/callback", params={"code": CODE, "site_id": SITE_ID, "state": nonce})
    assert r.status_code == 502
    with session_scope(container.session_factory) as s:
        assert s.get(SiteToken, SITE_ID) is None and s.get(Site, SITE_ID) is None


@respx.mock
def test_callback_happy_path_one_budgeted_site_call(env, load_fixture):
    """The access check is the only /sites call, and it goes through the budget ledger."""
    from solya.models import ApiLedger

    tc, container, channel, token = env
    _mock_se(load_fixture("solaredge/site_home.json"))
    nonce = _nonce(container, token)
    r = tc.get("/oauth/callback", params={"code": CODE, "site_id": SITE_ID, "state": nonce})
    assert r.status_code == 200
    site_calls = [c for c in respx.calls if c.request.url.path.startswith("/v2/sites")]
    assert len(site_calls) == 1
    with session_scope(container.session_factory) as s:
        rows = s.scalars(select(ApiLedger).where(ApiLedger.endpoint == f"/sites/{SITE_ID}")).all()
        assert len(rows) == 1 and rows[0].credited and rows[0].site_id == SITE_ID
