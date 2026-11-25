import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from solya.app import build_container, create_app
from solya.db import session_scope
from solya.models import ApiLedger, LlmUsage, Owner, RawResponse, Site, SiteToken

from conftest import FakeChannel
from web_helpers import proxied_client

PASSWORD = "correct horse battery"
HTTPS = "https://testserver"
CIPHER = b"gAAAAA-secret-cipher-bytes-xyz"
PLAIN = "plaintext-access-token-123"


def _build(settings, clock, tmp_path, password):
    settings.database_url = f"sqlite:///{tmp_path}/solya.db"
    settings.admin_password = SecretStr(password) if password else None

    async def sleep(seconds):
        clock.advance(timedelta(seconds=seconds))

    c = build_container(settings, clock=clock, channel=FakeChannel(), budget_sleep=sleep)
    return c, create_app(c, start_background=False)


@pytest.fixture
def container(settings, clock, tmp_path):
    return _build(settings, clock, tmp_path, PASSWORD)[0]


@pytest.fixture
def app(container):
    return create_app(container, start_background=False)


@pytest.fixture
def tc(app):
    with TestClient(app, base_url=HTTPS, follow_redirects=False) as client:
        yield client


def _csrf(html: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def _login(client, password=PASSWORD):
    csrf = _csrf(client.get("/admin/login").text)
    return client.post("/admin/login", data={"password": password, "csrf": csrf})


def _seed(container):
    now = container.clock.now()
    with session_scope(container.session_factory) as s:
        a = Owner(display_name="דנה כהן", email="dana@example.com", phone="0501234567",
                  signup_source="web", subscription_status="pending_payment",
                  telegram_chat_id=None, created_at=now)
        b = Owner(display_name="יוסי לוי", email="yossi@example.com", phone="0527654321",
                  signup_source="bot", subscription_status="active",
                  telegram_chat_id=555, created_at=now)
        s.add_all([a, b])
        s.flush()
        s.add_all([
            Site(se_site_id=3033498, owner_id=a.id, portal_name="גג דנה", card_name="הבית של דנה",
                 tz="Asia/Jerusalem", monitored=True, call_plan="low", comm_state="ok",
                 created_at=now),
            Site(se_site_id=4044, owner_id=a.id, portal_name="מחסן", tz="Asia/Jerusalem",
                 monitored=False, status="reauth_needed", comm_state="ok", created_at=now),
            Site(se_site_id=5055, owner_id=b.id, portal_name="חווה", tz="Asia/Jerusalem",
                 created_at=now),
        ])
        s.add(SiteToken(site_id=3033498, access_enc=CIPHER, refresh_enc=CIPHER,
                        access_expires_at=now, refreshed_at=now, authorized_at=now,
                        grant_expires_at=now))
        s.add(RawResponse(site_id=3033498, endpoint="/overview", params={}, status=200,
                          body={"token": PLAIN}, fetched_at=now - timedelta(hours=3)))
        s.add_all([ApiLedger(ts=now, site_id=3033498, endpoint="/overview", http_status=200,
                             credited=True) for _ in range(3)])
        s.add(ApiLedger(ts=now, site_id=5055, endpoint="/overview", http_status=200,
                        credited=True))
        s.add(LlmUsage(owner_id=b.id, ts=now, input_tokens=100, output_tokens=23))
        return a.id, b.id


def test_admin_404_without_password(settings, clock, tmp_path):
    _, app = _build(settings, clock, tmp_path, None)
    with TestClient(app, base_url=HTTPS, follow_redirects=False) as tc:
        assert tc.get("/admin").status_code == 404
        assert tc.get("/admin/login").status_code == 404
        assert tc.post("/admin/login", data={"password": "", "csrf": "x"}).status_code == 404
        assert tc.post("/admin/logout", data={"csrf": "x"}).status_code == 404


def test_admin_requires_login(tc):
    r = tc.get("/admin")
    assert r.status_code == 303 and r.headers["location"] == "/admin/login"
    tc.cookies.set("solya_admin", "forged")
    assert tc.get("/admin").status_code == 303


def test_admin_login_and_table(tc, container, settings):
    _seed(container)
    r = _login(tc)
    assert r.status_code == 303 and r.headers["location"] == "/admin"
    page = tc.get("/admin")
    assert page.status_code == 200
    body = page.text
    assert 'dir="rtl"' in body
    for text in ["דנה כהן", "dana@example.com", "0501234567", "yossi@example.com",
                 "ממתין לתשלום", "פעיל", "אתר", "בוט",
                 "הבית של דנה", "3033498", "מחסן", "לא במעקב", "low", "standard",
                 "דורש חיבור מחדש", "קרדיטים במחזור: <bdi>4/2000</bdi>"]:
        assert text in body, text
    assert "2026-11-24 14:00" in body  # last pull 3h before 15:00 UTC, Israel winter time
    assert re.search(r"<td>3</td>\s*<td>0</td>", body)  # owner a: 3 credits, 0 tokens
    assert re.search(r"<td>1</td>\s*<td>123</td>", body)  # owner b: 1 credit, 123 tokens
    # no token material, session secret or admin password anywhere in the response
    secrets_ = [CIPHER.decode(), PLAIN, PASSWORD, "gAAAAA",
                settings.effective_session_secret().hex(),
                settings.token_encryption_key.get_secret_value(),
                settings.telegram_bot_token.get_secret_value()]
    for secret in secrets_:
        assert secret not in body
    assert PASSWORD not in r.text and PASSWORD not in str(r.headers)


def test_admin_wrong_password_and_tampered_cookie(tc):
    r = _login(tc, "nope")
    assert r.status_code == 401 and "הסיסמה שגויה" in r.text
    assert "solya_admin" not in r.headers.get("set-cookie", "")
    assert tc.get("/admin").status_code == 303


def test_admin_lockout_per_client(app):
    bad = lambda cl: cl.post(  # noqa: E731
        "/admin/login", data={"password": "bad", "csrf": _csrf(cl.get("/admin/login").text)},
        headers={"X-Forwarded-For": cl.xff},
    )
    attacker = proxied_client(app, ["10.0.0.1"], "10.0.0.1")
    attacker.base_url = HTTPS
    attacker.follow_redirects = False
    attacker.xff = "1.1.1.1"
    for _ in range(5):
        assert bad(attacker).status_code == 401
    # the sixth try is locked out even with the right password
    csrf = _csrf(attacker.get("/admin/login", headers={"X-Forwarded-For": "1.1.1.1"}).text)
    r = attacker.post("/admin/login", data={"password": PASSWORD, "csrf": csrf},
                      headers={"X-Forwarded-For": "1.1.1.1"})
    assert r.status_code == 429
    # another client behind the same proxy is unaffected, and its success changes nothing
    other = proxied_client(app, ["10.0.0.1"], "10.0.0.1")
    other.base_url = HTTPS
    other.follow_redirects = False
    h = {"X-Forwarded-For": "2.2.2.2"}
    csrf = _csrf(other.get("/admin/login", headers=h).text)
    r = other.post("/admin/login", data={"password": PASSWORD, "csrf": csrf}, headers=h)
    assert r.status_code == 303
    csrf = _csrf(attacker.get("/admin/login", headers={"X-Forwarded-For": "1.1.1.1"}).text)
    r = attacker.post("/admin/login", data={"password": PASSWORD, "csrf": csrf},
                      headers={"X-Forwarded-For": "1.1.1.1"})
    assert r.status_code == 429


def test_admin_lockout_ignores_xff_from_untrusted_peer(app):
    c = proxied_client(app, ["10.0.0.1"], "9.9.9.9")  # untrusted peer
    c.base_url = HTTPS
    c.follow_redirects = False
    for i in range(5):
        csrf = _csrf(c.get("/admin/login").text)
        r = c.post("/admin/login", data={"password": "bad", "csrf": csrf},
                   headers={"X-Forwarded-For": f"7.7.7.{i}"})
        assert r.status_code == 401
    csrf = _csrf(c.get("/admin/login").text)
    r = c.post("/admin/login", data={"password": PASSWORD, "csrf": csrf},
               headers={"X-Forwarded-For": "7.7.7.99"})
    assert r.status_code == 429


def test_admin_csrf(tc):
    assert tc.post("/admin/login", data={"password": PASSWORD, "csrf": "bogus"}).status_code == 403
    assert tc.post("/admin/login", data={"password": PASSWORD}).status_code == 403
    assert _login(tc).status_code == 303
    assert tc.post("/admin/logout", data={"csrf": "bogus"}).status_code == 403
    assert tc.get("/admin").status_code == 200  # still signed in
    csrf = _csrf(tc.get("/admin").text)
    r = tc.post("/admin/logout", data={"csrf": csrf})
    assert r.status_code == 303 and r.headers["location"] == "/admin/login"
    assert tc.get("/admin").status_code == 303


def test_admin_logout_revokes_cookie_server_side(tc, container):
    assert _login(tc).status_code == 303
    old = tc.cookies.get("solya_admin")
    csrf = _csrf(tc.get("/admin").text)
    assert tc.post("/admin/logout", data={"csrf": csrf}).status_code == 303
    tc.cookies.clear()
    tc.cookies.set("solya_admin", old)  # an attacker replays the copied cookie
    r = tc.get("/admin")
    assert r.status_code == 303 and r.headers["location"] == "/admin/login"
    tc.cookies.clear()
    container.clock.advance(timedelta(seconds=2))
    assert _login(tc).status_code == 303  # a fresh login still works
    assert tc.get("/admin").status_code == 200


def test_admin_pages_no_store(tc):
    assert tc.get("/admin/login").headers["cache-control"] == "no-store"
    assert _login(tc).status_code == 303
    assert tc.get("/admin").headers["cache-control"] == "no-store"


def test_admin_logout_with_expired_cookie_redirects(tc, container):
    assert _login(tc).status_code == 303
    container.clock.advance(timedelta(hours=13))
    r = tc.post("/admin/logout", data={"csrf": "stale"})
    assert r.status_code == 303 and r.headers["location"] == "/admin/login"


LOW_PLAN_NOTE = "תוכנית חסכונית: ללא זיהוי תקלות ממיר"


def test_admin_states_low_plan_limits(tc, container):
    """Spec 5.5: the admin page says the low plan does not detect inverter faults."""
    _seed(container)
    assert _login(tc).status_code == 303
    body = tc.get("/admin").text
    assert body.count(LOW_PLAN_NOTE) == 1  # only site 3033498 is on the low plan
    start = body.index("הבית של דנה")
    site = body[start:body.index("</div>", start)]
    assert LOW_PLAN_NOTE in site


def test_admin_ai_tokens_use_the_budget_month(tc, container):
    """M8: the month starts at midnight Israel time, as the AI budget's does, not UTC."""
    _, b = _seed(container)
    now = datetime(2026, 12, 1, 10, 0, tzinfo=timezone.utc)
    container.clock.advance(now - container.clock.now())
    with session_scope(container.session_factory) as s:
        s.query(LlmUsage).delete()
        s.add(LlmUsage(owner_id=b, ts=datetime(2026, 11, 30, 23, 0, tzinfo=timezone.utc),
                       input_tokens=40, output_tokens=2))  # 01:00 on 1 December in Israel
        s.add(LlmUsage(owner_id=b, ts=datetime(2026, 11, 30, 21, 0, tzinfo=timezone.utc),
                       input_tokens=500, output_tokens=0))  # 23:00 on 30 November in Israel
    assert _login(tc).status_code == 303
    body = tc.get("/admin").text
    assert re.search(r"<td>\d+</td>\s*<td>42</td>", body)
