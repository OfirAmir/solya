import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from solya.app import build_container, create_app
from solya.db import session_scope
from solya.models import Invite, Owner
from solya.services import onboarding
from solya.web.journey import PRE_COOKIE
from solya.web.security import SESSION_COOKIE

from conftest import FakeChannel
from web_helpers import proxied_client

BAD_INVITE = "הקוד אינו בתוקף"
BAD_FIELDS = "נא למלא שם, אימייל וטלפון תקינים"
ALREADY = "כבר נרשמת, המשך בטלגרם"
# public_base_url is https, so cookies are Secure and only sent back over https
HTTPS = "https://testserver"


@pytest.fixture
def container(settings, clock, tmp_path):
    settings.database_url = f"sqlite:///{tmp_path}/solya.db"
    return build_container(settings, clock=clock, channel=FakeChannel())


@pytest.fixture
def app(container):
    return create_app(container, start_background=False)


@pytest.fixture
def tc(app):
    with TestClient(app, base_url=HTTPS, follow_redirects=False) as client:
        yield client


def _invite(container) -> str:
    with session_scope(container.session_factory) as s:
        return onboarding.create_invite(s, "admin", container.clock.now())


def _csrf(html: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def _form(tc, path="/") -> str:
    r = tc.get(path)
    assert r.status_code == 200
    return _csrf(r.text)


def _signup(tc, code, csrf=None, name="דנה כהן", email="dana@example.com", phone="0501234567"):
    if csrf is None:
        csrf = _form(tc)
    return tc.post("/signup", data={"name": name, "email": email, "phone": phone,
                                    "code": code, "csrf": csrf})


def _owners(container) -> list[Owner]:
    with session_scope(container.session_factory) as s:
        owners = list(s.scalars(select(Owner)))
        s.expunge_all()
        return owners


def test_landing_rtl_no_ray_when_disabled(tc, container):
    assert container.settings.ray_implementation is None
    r = tc.get("/")
    assert r.status_code == 200
    assert '<html dir="rtl" lang="he">' in r.text
    assert "RAY" not in r.text
    assert "סול שומרת על המערכת, כל ערב." in r.text
    assert "אופן הצגת המע״מ טרם נקבע." in r.text
    for field in ('name="name"', 'name="email"', 'name="phone"', 'name="code"', 'name="csrf"'):
        assert field in r.text
    assert PRE_COOKIE in r.cookies
    assert tc.get("/static/style.css").status_code == 200


def test_landing_mentions_ray_when_enabled(settings, clock, tmp_path):
    settings.database_url = f"sqlite:///{tmp_path}/solya.db"
    settings.ray_implementation = "solya.rules.ray:NullRay"
    container = build_container(settings, clock=clock, channel=FakeChannel())
    with TestClient(create_app(container, start_background=False), base_url=HTTPS) as client:
        assert "RAY FACTOR" in client.get("/").text


def test_signup_creates_web_owner_and_redirects(tc, container):
    code = _invite(container)
    r = _signup(tc, code, email=" dana@example.com ", name="  דנה כהן ")
    assert r.status_code == 303
    assert r.headers["location"] == "/connect-site"
    set_cookie = r.headers["set-cookie"]
    assert f"{SESSION_COOKIE}=" in set_cookie
    assert "httponly" in set_cookie.lower() and "samesite=lax" in set_cookie.lower()
    assert "secure" in set_cookie.lower()  # public_base_url is https
    [owner] = _owners(container)
    assert container.sessions.read(r.cookies[SESSION_COOKIE]) == owner.id
    assert owner.signup_source == "web"
    assert owner.subscription_status == "lead"
    assert owner.telegram_chat_id is None
    assert owner.link_token and len(owner.link_token) >= 24
    assert (owner.display_name, owner.email, owner.phone) == ("דנה כהן", "dana@example.com", "0501234567")
    with session_scope(container.session_factory) as s:
        inv = s.get(Invite, code)
        assert inv.used_at is not None and inv.owner_id == owner.id


def test_signup_invite_single_use(tc, container):
    code = _invite(container)
    assert _signup(tc, code).status_code == 303
    tc.cookies.clear()
    r = _signup(tc, code, email="other@example.com")
    assert r.status_code == 400
    assert BAD_INVITE in r.text
    assert SESSION_COOKIE not in r.cookies
    assert len(_owners(container)) == 1


def test_signup_unknown_invite(tc, container):
    r = _signup(tc, "no-such-code")
    assert r.status_code == 400
    assert BAD_INVITE in r.text
    assert _owners(container) == []


@pytest.mark.parametrize("name,email,phone", [
    ("ד", "dana@example.com", "0501234567"),        # name too short
    ("  ד  ", "dana@example.com", "0501234567"),    # short after strip
    ("דנה", "dana.example.com", "0501234567"),      # no @
    ("דנה", "dana@example.com", "05012345"),        # 8 digits
    ("דנה", "dana@example.com", "050-abc-de"),
])
def test_signup_bad_fields(tc, container, name, email, phone):
    code = _invite(container)
    r = _signup(tc, code, name=name, email=email, phone=phone)
    assert r.status_code == 400
    assert BAD_FIELDS in r.text
    assert _owners(container) == []
    with session_scope(container.session_factory) as s:
        assert s.get(Invite, code).used_at is None  # the invite was not burnt
    # the code is kept in the re-rendered form; email and phone are not echoed
    assert f'value="{code}"' in r.text
    assert email not in r.text


def test_signup_phone_formats(tc, container):
    for raw, stored in [("050-123 4567", "050-123 4567"), ("  0501234567  ", "0501234567"),
                        ("+972 50 123 4567", "+972 50 123 4567")]:
        tc.cookies.clear()
        r = _signup(tc, _invite(container), phone=raw)
        assert r.status_code == 303, raw
    assert [o.phone for o in _owners(container)] == ["050-123 4567", "0501234567", "+972 50 123 4567"]


def test_signup_csrf_required(tc, container):
    code = _invite(container)
    _form(tc)
    r = _signup(tc, code, csrf="forged")
    assert r.status_code == 403
    r = tc.post("/signup", data={"name": "דנה", "email": "d@e.com", "phone": "0501234567",
                                 "code": code})
    assert r.status_code == 403
    # a token from another browser's pre-session cookie does not work here
    foreign = _csrf(TestClient(tc.app, base_url=HTTPS).get("/").text)
    assert _signup(tc, code, csrf=foreign).status_code == 403
    assert _owners(container) == []
    with session_scope(container.session_factory) as s:
        assert s.get(Invite, code).used_at is None


def test_signup_rate_limited(app, container):
    client = proxied_client(app, ["127.0.0.1"], "127.0.0.1")
    other = proxied_client(app, ["127.0.0.1"], "127.0.0.1")
    client.base_url = other.base_url = HTTPS
    xff = {"X-Forwarded-For": "203.0.113.7"}
    csrf = _csrf(client.get("/").text)
    data = {"name": "ד", "email": "x", "phone": "1", "code": "c", "csrf": csrf}
    for _ in range(10):
        assert client.post("/signup", data=data, headers=xff).status_code == 400
    r = client.post("/signup", data=data, headers=xff)
    assert r.status_code == 429
    assert '<html dir="rtl" lang="he">' in r.text
    assert "יותר מדי ניסיונות" in r.text
    # another client behind the same proxy is not limited
    csrf2 = _csrf(other.get("/").text)
    r = other.post("/signup", data={**data, "csrf": csrf2},
                   headers={"X-Forwarded-For": "198.51.100.9"})
    assert r.status_code == 400


def test_join_prefills_code(tc, container):
    code = _invite(container)
    r = tc.get(f"/join/{code}")
    assert r.status_code == 200
    assert f'name="code" value="{code}"' in r.text
    assert 'name="csrf"' in r.text


def test_join_unknown_invite_shows_bad_code(tc):
    r = tc.get("/join/nope")
    assert r.status_code == 200
    assert BAD_INVITE in r.text


def test_join_resume_before_link(tc, container):
    code = _invite(container)
    first = _signup(tc, code)
    owner_id = container.sessions.read(first.cookies[SESSION_COOKIE])
    tc.cookies.clear()
    r = tc.get(f"/join/{code}")
    assert r.status_code == 303
    assert r.headers["location"] == "/connect-site"
    assert container.sessions.read(r.cookies[SESSION_COOKIE]) == owner_id
    assert len(_owners(container)) == 1


def test_join_after_link_shows_continue_in_telegram(tc, container):
    code = _invite(container)
    first = _signup(tc, code)
    owner_id = container.sessions.read(first.cookies[SESSION_COOKIE])
    with session_scope(container.session_factory) as s:
        owner = s.get(Owner, owner_id)
        link_token = owner.link_token
        owner.telegram_chat_id = 777
        owner.link_token = None
    tc.cookies.clear()
    r = tc.get(f"/join/{code}")
    assert r.status_code == 200
    assert ALREADY in r.text
    assert SESSION_COOKIE not in r.cookies
    assert link_token not in r.text and "start=" not in r.text


def test_join_bot_owner_invite_is_not_resumable(tc, container):
    code = _invite(container)
    with session_scope(container.session_factory) as s:
        onboarding.redeem_invite(s, code, 999, "דנה", container.clock.now())
    r = tc.get(f"/join/{code}")
    assert r.status_code == 200
    assert BAD_INVITE in r.text
    assert SESSION_COOKIE not in r.cookies


def test_autoescape(tc, container):
    r = _signup(tc, _invite(container), name="<b>x</b>", phone="1")
    assert "<b>x</b>" not in r.text and "&lt;b&gt;x&lt;/b&gt;" in r.text


# --- service-level -------------------------------------------------------------------------

def test_signup_web_service(db, clock):
    now = clock.now()
    with session_scope(db) as s:
        code = onboarding.create_invite(s, "admin", now)
    with session_scope(db) as s:
        with pytest.raises(ValueError, match="form"):
            onboarding.signup_web(s, code, "ד", "a@b", "0501234567", now)
    with session_scope(db) as s:
        assert onboarding.signup_web(s, "missing", "דנה", "a@b", "0501234567", now) is None
        owner = onboarding.signup_web(s, f" {code} ", "דנה", "a@b", "0501234567", now)
        assert owner is not None and owner.signup_source == "web"
    with session_scope(db) as s:
        assert onboarding.signup_web(s, code, "דנה", "a@b", "0501234567", now) is None
        assert s.scalar(select(func.count()).select_from(Owner)) == 1
