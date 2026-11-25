import re
from datetime import timedelta

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from solya.app import build_container, create_app
from solya.db import session_scope
from solya.models import ConnectRequest, Invite, Owner, Payment, Site
from solya.pricing import ESTIMATE_LABEL, VAT_NOTE
from solya.services import onboarding, payments
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

    async def sleep(seconds):  # SolarEdge request spacing advances the fixed clock
        clock.advance(timedelta(seconds=seconds))

    return build_container(settings, clock=clock, channel=FakeChannel(), budget_sleep=sleep)


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
    ("דנה", "dana@example.com", "٠٥٠١٢٣٤٥٦٧"),       # Arabic-Indic digits do not count
    ("ד" * 101, "dana@example.com", "0501234567"),  # name over 100
    ("דנה", "d@" + "e" * 253, "0501234567"),         # email over 254
    ("דנה", "dana@example.com", "050-123-4567" + "-" * 21),  # phone over 32
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


def test_signup_field_caps_inclusive(tc, container):
    r = _signup(tc, _invite(container), name="ד" * 100, email="d@" + "e" * 252,
                phone="050-123-4567" + "-" * 20)
    assert r.status_code == 303


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


def _signed_up(tc, container) -> tuple[str, int]:
    code = _invite(container)
    first = _signup(tc, code)
    owner_id = container.sessions.read(first.cookies[SESSION_COOKIE])
    tc.cookies.clear()
    return code, owner_id


def test_join_get_confirms_without_signing_in(tc, container):
    code, _ = _signed_up(tc, container)
    r = tc.get(f"/join/{code}")
    assert r.status_code == 200
    assert "להמשיך כ־דנה כהן?" in r.text
    assert "זה לא אני" in r.text and 'href="/"' in r.text
    assert f'action="/join/{code}" method="post"' in r.text
    assert SESSION_COOKIE not in r.cookies
    assert SESSION_COOKIE not in r.headers.get("set-cookie", "")


def test_join_post_requires_csrf(tc, container):
    code, _ = _signed_up(tc, container)
    tc.get(f"/join/{code}")
    for data in ({}, {"csrf": "forged"}):
        r = tc.post(f"/join/{code}", data=data)
        assert r.status_code == 403
        assert SESSION_COOKIE not in r.cookies


def test_join_resume_before_link(tc, container):
    code, owner_id = _signed_up(tc, container)
    csrf = _csrf(tc.get(f"/join/{code}").text)
    r = tc.post(f"/join/{code}", data={"csrf": csrf})
    assert r.status_code == 303
    assert r.headers["location"] == "/connect-site"
    assert container.sessions.read(r.cookies[SESSION_COOKIE]) == owner_id
    assert len(_owners(container)) == 1


def test_join_post_rate_limited(tc, container):
    code, _ = _signed_up(tc, container)  # one sign-up already counted
    csrf = _csrf(tc.get(f"/join/{code}").text)
    for _ in range(9):
        assert tc.post(f"/join/{code}", data={"csrf": csrf}).status_code == 303
    assert tc.post(f"/join/{code}", data={"csrf": csrf}).status_code == 429


def test_join_post_after_link_issues_no_session(tc, container):
    code, owner_id = _signed_up(tc, container)
    csrf = _csrf(tc.get(f"/join/{code}").text)
    with session_scope(container.session_factory) as s:
        s.get(Owner, owner_id).telegram_chat_id = 777
    r = tc.post(f"/join/{code}", data={"csrf": csrf})
    assert r.status_code == 200 and ALREADY in r.text
    assert SESSION_COOKIE not in r.cookies


def test_join_inactive_owner_not_resumable(tc, container):
    code, owner_id = _signed_up(tc, container)
    with session_scope(container.session_factory) as s:
        s.get(Owner, owner_id).active = False
    r = tc.get(f"/join/{code}")
    assert BAD_INVITE in r.text and "להמשיך" not in r.text


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


# --- connect, site picker, price, payment stub, ready (Task 6) -----------------------------

FARM_ID = 3033498  # portal peakPower 65.52, no override: 49.97 + 15.52 = 65.49 estimate
CODE = "SECRET-AUTH-CODE-123"
CONNECTED = "המערכת חוברה בהצלחה. אפשר לחזור לטלגרם."
PICK_ONE = "יש לבחור לפחות מערכת אחת"
VIA_BOT = "השינויים נעשים עכשיו דרך סול בטלגרם"
STUB = "טרם שולם. ספק הסליקה טרם נבחר, ולא בוצע חיוב ולא הופעל מנוי."


def _mock_se(site_json):
    respx.post("https://monitoringapi.solaredge.com/v2/oauth2/token").mock(
        return_value=httpx.Response(
            200, json={"access_token": "ACCESS-T", "refresh_token": "REFRESH-T", "expires_in": 3600}
        )
    )
    respx.get(f"https://monitoringapi.solaredge.com/v2/sites/{site_json['siteId']}").mock(
        return_value=httpx.Response(200, json=site_json)
    )


def _web_owner(tc, container) -> int:
    """Sign up on `tc` (which keeps the session cookie) and return the owner id."""
    r = _signup(tc, _invite(container))
    assert r.status_code == 303
    return container.sessions.read(r.cookies[SESSION_COOKIE])


def _add_site(container, owner_id, site_id, name="בית", kw=26.55, monitored=True) -> None:
    with session_scope(container.session_factory) as s:
        s.add(Site(se_site_id=site_id, owner_id=owner_id, portal_name=name, tz="Asia/Jerusalem",
                   peak_power_kwp_reported=kw, monitored=monitored,
                   created_at=container.clock.now()))


def _link(container, owner_id, chat=777) -> None:
    with session_scope(container.session_factory) as s:
        s.get(Owner, owner_id).telegram_chat_id = chat


def _owner(container, owner_id) -> Owner:
    with session_scope(container.session_factory) as s:
        owner = s.get(Owner, owner_id)
        s.expunge(owner)
        return owner


def _monitored(container) -> dict[int, bool]:
    with session_scope(container.session_factory) as s:
        return {site.se_site_id: site.monitored for site in s.scalars(select(Site))}


@respx.mock
def test_full_journey_to_ready(tc, container, load_fixture):
    owner_id = _web_owner(tc, container)
    r = tc.get("/connect-site")
    assert r.status_code == 200
    assert '<html dir="rtl" lang="he">' in r.text
    assert "חיבור ל-SolarEdge" in r.text
    r = tc.post("/connect-site", data={"csrf": _csrf(r.text)})
    assert r.status_code == 303
    location = r.headers["location"]
    assert re.fullmatch(r"/connect/[\w-]+", location)
    token = location.rsplit("/", 1)[1]
    r = tc.get(location)
    assert r.status_code == 302 and "solaredge" in r.headers["location"]
    with session_scope(container.session_factory) as s:
        req = s.get(ConnectRequest, token)
        assert req.owner_id == owner_id
        nonce = req.nonce

    _mock_se(load_fixture("solaredge/site_farm.json"))
    r = tc.get("/oauth/callback", params={"code": CODE, "site_id": FARM_ID, "state": nonce})
    assert r.status_code == 303
    assert r.headers["location"] == "/sites"
    assert _owner(container, owner_id).subscription_status == "connected"

    r = tc.get("/sites")
    assert r.status_code == 200
    assert str(FARM_ID) in r.text and "אורן סופר" in r.text
    assert "65.49 ₪" in r.text
    assert 'href="/connect-site"' in r.text
    r = tc.post("/sites", data={"site": str(FARM_ID), "csrf": _csrf(r.text)})
    assert r.status_code == 303 and r.headers["location"] == "/price"

    r = tc.get("/price")
    assert r.status_code == 200
    assert "65.49 ₪" in r.text and ESTIMATE_LABEL in r.text and VAT_NOTE in r.text
    r = tc.post("/pay", data={"csrf": _csrf(r.text)})
    assert r.status_code == 303
    pay_id = r.headers["location"].removeprefix("/pay/")
    with session_scope(container.session_factory) as s:
        payment = s.get(Payment, pay_id)
        assert payment.owner_id == owner_id
        assert payment.amount_agorot == 6549 and payment.amount_is_estimate is True
        assert payment.provider == "stub" and payment.status == "stub_pending"
    assert _owner(container, owner_id).subscription_status == "pending_payment"

    r = tc.get(f"/pay/{pay_id}")
    assert r.status_code == 200
    assert "65.49 ₪" in r.text and STUB in r.text and ESTIMATE_LABEL in r.text
    assert 'href="/ready"' in r.text

    r = tc.get("/ready")
    assert r.status_code == 200
    assert "המערכות נשמרו." in r.text
    link_token = _owner(container, owner_id).link_token
    assert f'href="https://t.me/solya_test_bot?start={link_token}"' in r.text
    assert "ACCESS-T" not in r.text and "REFRESH-T" not in r.text


def test_pay_other_owner_404(tc, app, container):
    _web_owner(tc, container)
    with TestClient(app, base_url=HTTPS, follow_redirects=False) as other:
        other_id = _web_owner(other, container)
        with session_scope(container.session_factory) as s:
            theirs = payments.create_stub_checkout(s, other_id, 4997, False, "x",
                                                   container.clock.now()).id
        assert other.get(f"/pay/{theirs}").status_code == 200
    r = tc.get(f"/pay/{theirs}")
    assert r.status_code == 404
    assert "49.97" not in r.text
    assert tc.get("/pay/no-such-payment").status_code == 404


def test_sites_lists_only_own(tc, app, container):
    mine = _web_owner(tc, container)
    with TestClient(app, base_url=HTTPS, follow_redirects=False) as other:
        theirs = _web_owner(other, container)
    _add_site(container, mine, 111111, name="הבית שלי")
    _add_site(container, theirs, 222222, name="של מישהו אחר")
    r = tc.get("/sites")
    assert r.status_code == 200
    assert "111111" in r.text and "הבית שלי" in r.text
    assert "222222" not in r.text and "של מישהו אחר" not in r.text
    # ticking another owner's site id does not count and does not touch it
    with session_scope(container.session_factory) as s:
        s.get(Site, 222222).monitored = False
    r = tc.post("/sites", data={"site": ["222222"], "csrf": _csrf(r.text)})
    assert r.status_code == 400 and PICK_ONE in r.text
    assert _monitored(container) == {111111: True, 222222: False}
    r = tc.post("/sites", data={"site": ["111111", "222222"], "csrf": _csrf(r.text)})
    assert r.status_code == 303
    assert _monitored(container) == {111111: True, 222222: False}


def test_sites_requires_one(tc, container):
    owner_id = _web_owner(tc, container)
    _add_site(container, owner_id, 111111)
    _add_site(container, owner_id, 111112)
    csrf = _csrf(tc.get("/sites").text)
    r = tc.post("/sites", data={"csrf": csrf})
    assert r.status_code == 400
    assert PICK_ONE in r.text and "111111" in r.text
    assert _monitored(container) == {111111: True, 111112: True}
    r = tc.post("/sites", data={"site": "111112", "csrf": csrf})
    assert r.status_code == 303 and r.headers["location"] == "/price"
    assert _monitored(container) == {111111: False, 111112: True}
    r = tc.get("/price")
    assert "49.97 ₪" in r.text  # only the monitored site is priced


def test_sites_after_link_409(tc, container):
    owner_id = _web_owner(tc, container)
    _add_site(container, owner_id, 111111)
    csrf = _csrf(tc.get("/connect-site").text)
    _link(container, owner_id)
    r = tc.post("/sites", data={"site": "111111", "csrf": csrf})
    assert r.status_code == 409
    assert VIA_BOT in r.text


def test_ready_after_link_shows_continue_in_telegram(tc, container):
    owner_id = _web_owner(tc, container)
    link_token = _owner(container, owner_id).link_token
    _link(container, owner_id)
    r = tc.get("/ready")
    assert r.status_code == 200
    assert ALREADY in r.text
    assert link_token not in r.text and "start=" not in r.text


def test_price_unknown_kw_still_allows_stub(tc, container):
    owner_id = _web_owner(tc, container)
    _add_site(container, owner_id, 111111, kw=None)
    r = tc.get("/price")
    assert "המחיר יחושב כשקילוואט מותקן שמור אצלנו." in r.text
    r = tc.post("/pay", data={"csrf": _csrf(r.text)})
    assert r.status_code == 303
    with session_scope(container.session_factory) as s:
        [payment] = s.scalars(select(Payment))
        assert payment.amount_agorot is None
    assert "המחיר יחושב" in tc.get(r.headers["location"]).text


def test_price_without_monitored_sites_goes_to_picker(tc, container):
    owner_id = _web_owner(tc, container)
    r = tc.get("/price")
    assert r.status_code == 303 and r.headers["location"] == "/sites"
    _add_site(container, owner_id, 111111, monitored=False)
    csrf = _csrf(tc.get("/sites").text)
    r = tc.post("/pay", data={"csrf": csrf})
    assert r.status_code == 303 and r.headers["location"] == "/sites"
    with session_scope(container.session_factory) as s:
        assert s.scalar(select(func.count()).select_from(Payment)) == 0


def test_journey_posts_require_csrf(tc, container):
    owner_id = _web_owner(tc, container)
    _add_site(container, owner_id, 111111)
    for path, data in [("/connect-site", {}), ("/sites", {"site": "111111"}), ("/pay", {})]:
        for extra in ({}, {"csrf": "forged"}):
            r = tc.post(path, data={**data, **extra})
            assert r.status_code == 403, path
    # the pre-session token from the landing page is not the session token
    tc.cookies.delete(PRE_COOKIE)
    pre_token = _csrf(tc.get("/").text)
    assert tc.post("/pay", data={"csrf": pre_token}).status_code == 403
    with session_scope(container.session_factory) as s:
        assert s.scalar(select(func.count()).select_from(Payment)) == 0
        assert s.scalar(select(func.count()).select_from(ConnectRequest)) == 0


def test_journey_pages_redirect_without_session(tc, container):
    gets = ["/connect-site", "/sites", "/price", "/pay/anything", "/ready"]
    posts = ["/connect-site", "/sites", "/pay"]
    for cookie in (None, "forged-session"):
        tc.cookies.clear()
        if cookie:
            tc.cookies.set(SESSION_COOKIE, cookie)
        for path in gets:
            r = tc.get(path)
            assert (r.status_code, r.headers.get("location")) == (303, "/"), path
        for path in posts:
            r = tc.post(path, data={"csrf": "x"})
            assert (r.status_code, r.headers.get("location")) == (303, "/"), path
    # a valid session for an owner who is no longer active also goes back to /
    owner_id = _web_owner(tc, container)
    with session_scope(container.session_factory) as s:
        s.get(Owner, owner_id).active = False
    assert tc.get("/sites").headers.get("location") == "/"


@respx.mock
def test_bot_owner_callback_unchanged(tc, container, load_fixture):
    _mock_se(load_fixture("solaredge/site_farm.json"))
    with session_scope(container.session_factory) as s:
        bot_owner = Owner(display_name="דנה", telegram_chat_id=555, created_at=container.clock.now())
        s.add(bot_owner)
        s.flush()
        web_linked = onboarding.signup_web(s, onboarding.create_invite(s, "admin", container.clock.now()),
                                           "רון", "r@e.com", "0501234567", container.clock.now())
        web_linked.telegram_chat_id = 556
        owners = [bot_owner.id, web_linked.id]
    for owner_id in owners:
        with session_scope(container.session_factory) as s:
            token = onboarding.create_connect_request(
                s, owner_id, container.clock.now(), container.settings).rsplit("/", 1)[1]
            nonce = s.get(ConnectRequest, token).nonce
        r = tc.get("/oauth/callback", params={"code": CODE, "site_id": FARM_ID, "state": nonce})
        assert r.status_code == 200, owner_id
        assert CONNECTED in r.text
    assert _owner(container, owners[0]).subscription_status == "active"
    assert _owner(container, owners[1]).subscription_status == "lead"


# --- OAuth failure pages for an unlinked web owner (final review F1) -------------------

RETRY = "אפשר לנסות שוב:"
RETRY_BUTTON = 'href="/connect-site"'


def _assert_web_retry(r, status):
    assert r.status_code == status
    assert RETRY in r.text and RETRY_BUTTON in r.text and "חזרה לחיבור" in r.text
    assert "/connect." not in r.text and "/connect בבוט" not in r.text
    assert '<link rel="stylesheet" href="/static/style.css">' in r.text  # the journey template


def test_web_owner_cancelled_callback_offers_connect_site(tc, container):
    _web_owner(tc, container)
    r = tc.get("/oauth/callback", params={"error": "access_denied", "error_description": "no"})
    _assert_web_retry(r, 400)
    assert "החיבור בוטל או נכשל." in r.text


def test_web_owner_expired_connect_link_offers_connect_site(tc, container):
    _web_owner(tc, container)
    r = tc.get("/connect/no-such-token")
    _assert_web_retry(r, 404)
    assert "הקישור אינו בתוקף." in r.text


@respx.mock
def test_web_owner_failed_exchange_offers_connect_site_without_session(tc, container):
    """No session cookie: the resolved connect request identifies the web owner."""
    owner_id = _web_owner(tc, container)
    with session_scope(container.session_factory) as s:
        token = onboarding.create_connect_request(
            s, owner_id, container.clock.now(), container.settings).rsplit("/", 1)[1]
        nonce = s.get(ConnectRequest, token).nonce
    respx.post("https://monitoringapi.solaredge.com/v2/oauth2/token").mock(
        return_value=httpx.Response(500))
    tc.cookies.clear()
    r = tc.get("/oauth/callback", params={"code": CODE, "site_id": FARM_ID, "state": nonce})
    _assert_web_retry(r, 502)
    assert "משהו השתבש בחיבור המערכת." in r.text


def test_linked_web_owner_and_no_session_keep_bot_copy(tc, container):
    r = tc.get("/oauth/callback", params={"error": "access_denied"})
    assert "אפשר לנסות שוב עם /connect." in r.text and RETRY_BUTTON not in r.text
    owner_id = _web_owner(tc, container)
    _link(container, owner_id)
    r = tc.get("/oauth/callback", params={"error": "access_denied"})
    assert "אפשר לנסות שוב עם /connect." in r.text and RETRY_BUTTON not in r.text
