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


def test_callback_error_param_400(env):
    tc, *_ = env
    r = tc.get("/oauth/callback", params={"error": "access_denied", "error_description": "no"})
    assert r.status_code == 400
    assert "החיבור בוטל או נכשל. אפשר לנסות שוב עם /connect." in r.text


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
