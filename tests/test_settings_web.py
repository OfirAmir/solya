"""Task 6: the single-use settings link and the owner settings page."""
import re
from datetime import timedelta
from decimal import Decimal
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from solya.app import build_container, create_app
from solya.db import session_scope
from solya.models import Owner, SettingsLink, Site
from solya.services import onboarding
from solya.web.security import SESSION_COOKIE

from conftest import FakeChannel

HTTPS = "https://testserver"
EXPIRED = "הקישור פג. שלחו /settings לסול לקישור חדש."
OPEN = "פתיחת ההגדרות"
SAVED = "נשמר."
PICK_ONE = "יש לבחור לפחות מערכת אחת"
CONNECT_MORE = "חיבור מערכת נוספת"


@pytest.fixture
def channel():
    return FakeChannel()


@pytest.fixture
def container(settings, clock, tmp_path, channel):
    settings.database_url = f"sqlite:///{tmp_path}/solya.db"
    settings.admin_chat_ids = [999]
    return build_container(settings, clock=clock, channel=channel)


@pytest.fixture
def app(container):
    return create_app(container, start_background=False)


@pytest.fixture
def tc(app):
    with TestClient(app, base_url=HTTPS, follow_redirects=False) as client:
        yield client


def _csrf(html: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def _owner(container, chat=777) -> int:
    with session_scope(container.session_factory) as s:
        owner = Owner(display_name="אורן", telegram_chat_id=chat, active=True,
                      created_at=container.clock.now())
        s.add(owner)
        s.flush()
        return owner.id


def _add_site(container, owner_id, site_id, name="בית", kw=26.55, monitored=True):
    with session_scope(container.session_factory) as s:
        s.add(Site(se_site_id=site_id, owner_id=owner_id, portal_name=name, tz="Asia/Jerusalem",
                   peak_power_kwp_reported=kw, monitored=monitored,
                   created_at=container.clock.now()))


def _link(container, owner_id) -> str:
    with session_scope(container.session_factory) as s:
        url = onboarding.create_settings_link(s, owner_id, container.clock.now(),
                                              container.settings)
    assert url.startswith("https://solya.example/settings/")
    return urlsplit(url).path


def _sign_in(tc, container, owner_id):
    path = _link(container, owner_id)
    r = tc.post(path, data={"csrf": _csrf(tc.get(path).text)})
    assert (r.status_code, r.headers["location"]) == (303, "/settings")


def _site(container, site_id) -> Site:
    with session_scope(container.session_factory) as s:
        site = s.get(Site, site_id)
        s.expunge(site)
        return site


def test_settings_link_confirm_then_session(tc, container):
    owner_id = _owner(container)
    path = _link(container, owner_id)
    r = tc.get(path)
    assert r.status_code == 200
    assert OPEN in r.text
    assert SESSION_COOKIE not in r.cookies
    assert SESSION_COOKIE not in r.headers.get("set-cookie", "")
    r = tc.post(path, data={"csrf": _csrf(r.text)})
    assert (r.status_code, r.headers["location"]) == (303, "/settings")
    assert container.sessions.read(r.cookies[SESSION_COOKIE]) == owner_id
    assert "max-age=86400" in r.headers["set-cookie"].lower()
    assert tc.get("/settings").status_code == 200


def test_settings_link_post_requires_csrf(tc, container):
    path = _link(container, _owner(container))
    tc.get(path)
    r = tc.post(path, data={"csrf": "nope"})
    assert r.status_code == 403
    assert SESSION_COOKIE not in r.cookies
    # the link is still usable
    r = tc.post(path, data={"csrf": _csrf(tc.get(path).text)})
    assert r.status_code == 303


def test_settings_link_single_use(tc, container):
    path = _link(container, _owner(container))
    csrf = _csrf(tc.get(path).text)
    assert tc.post(path, data={"csrf": csrf}).status_code == 303
    tc.cookies.delete(SESSION_COOKIE)
    r = tc.post(path, data={"csrf": csrf})
    assert r.status_code == 410
    assert EXPIRED in r.text
    assert SESSION_COOKIE not in r.cookies
    r = tc.get(path)
    assert r.status_code == 410 and EXPIRED in r.text


def test_settings_link_expires_after_15_minutes(tc, container):
    path = _link(container, _owner(container))
    csrf = _csrf(tc.get(path).text)
    container.clock.advance(timedelta(minutes=16))
    r = tc.get(path)
    assert r.status_code == 410 and EXPIRED in r.text
    r = tc.post(path, data={"csrf": csrf})
    assert r.status_code == 410 and SESSION_COOKIE not in r.cookies


def test_settings_unknown_token_is_expired_page(tc):
    r = tc.get("/settings/not-a-token")
    assert r.status_code == 410 and EXPIRED in r.text


def test_settings_link_inactive_owner(tc, container):
    owner_id = _owner(container)
    path = _link(container, owner_id)
    csrf = _csrf(tc.get(path).text)
    with session_scope(container.session_factory) as s:
        s.get(Owner, owner_id).active = False
    r = tc.post(path, data={"csrf": csrf})
    assert r.status_code == 410 and SESSION_COOKIE not in r.cookies


def test_settings_session_lasts_24h(tc, container):
    owner_id = _owner(container)
    _sign_in(tc, container, owner_id)
    container.clock.advance(timedelta(hours=23))
    assert tc.get("/settings").status_code == 200
    container.clock.advance(timedelta(hours=2))
    r = tc.get("/settings")
    assert (r.status_code, r.headers["location"]) == (303, "/")


def test_regular_session_unchanged(container):
    cookie = container.sessions.issue(5)
    container.clock.advance(timedelta(days=29))
    assert container.sessions.read(cookie) == 5
    container.clock.advance(timedelta(days=2))
    assert container.sessions.read(cookie) is None


def test_settings_page_shows_sites_prices_total(tc, container):
    owner_id = _owner(container)
    _add_site(container, owner_id, 111111, name="בית", kw=26.55)
    _add_site(container, owner_id, 111112, name="משק", kw=65.52)
    _sign_in(tc, container, owner_id)
    r = tc.get("/settings?connected=111112")
    assert r.status_code == 200
    page = r.text
    assert 'name="name_111111"' in page and 'name="tariff_111112"' in page
    assert "49.97 ₪" in page and "65.49 ₪" in page and "115.46 ₪" in page
    assert 'action="/connect-site"' in page and CONNECT_MORE in page
    assert "חוברה. יש לך עוד מערכת?" in page


def test_settings_save_and_admin_notice(tc, container, channel):
    owner_id = _owner(container)
    _add_site(container, owner_id, 111111, name="בית", kw=26.55)
    _add_site(container, owner_id, 111112, name="משק", kw=65.52)
    _sign_in(tc, container, owner_id)
    csrf = _csrf(tc.get("/settings").text)
    r = tc.post("/settings", data={
        "csrf": csrf, "site": ["111111"],
        "name_111111": "בית", "basis_111111": "export", "tariff_111111": "0.48",
        "name_111112": "משק", "basis_111112": "production", "tariff_111112": "",
    })
    assert r.status_code == 200
    assert SAVED in r.text
    assert _site(container, 111111).tariff_ils_per_kwh == Decimal("0.48")
    assert _site(container, 111112).monitored is False
    admin = [m for m in channel.sent if m.recipient == "999"]
    assert len(admin) == 1
    text = admin[0].text.replace("\u200f", "").replace("\u200e", "")  # rtl_text marks
    assert f"בעלים {owner_id} שינה מעקב: משק. מחיר חודשי חדש: 49.97 ₪ לחודש." in text
    # a save that changes no monitoring sends nothing
    r = tc.post("/settings", data={"csrf": csrf, "site": ["111111"], "tariff_111111": "0.5"})
    assert r.status_code == 200 and SAVED in r.text
    assert len([m for m in channel.sent if m.recipient == "999"]) == 1


def test_settings_forged_site_ignored(tc, container):
    owner_id = _owner(container)
    other = _owner(container, chat=888)
    _add_site(container, owner_id, 111111, name="בית")
    _add_site(container, other, 222222, name="של מישהו אחר")
    _sign_in(tc, container, owner_id)
    page = tc.get("/settings").text
    assert "של מישהו אחר" not in page and "222222" not in page
    r = tc.post("/settings", data={
        "csrf": _csrf(page), "site": ["111111"],
        "name_222222": "נפרץ", "basis_222222": "export", "tariff_222222": "1",
    })
    assert r.status_code == 200 and SAVED in r.text
    theirs = _site(container, 222222)
    assert theirs.label_he is None and theirs.tariff_ils_per_kwh is None
    assert theirs.monitored is True


def test_settings_requires_one_monitored(tc, container, channel):
    owner_id = _owner(container)
    _add_site(container, owner_id, 111111)
    _sign_in(tc, container, owner_id)
    csrf = _csrf(tc.get("/settings").text)
    r = tc.post("/settings", data={"csrf": csrf, "tariff_111111": "0.48"})
    assert r.status_code == 400 and PICK_ONE in r.text
    site = _site(container, 111111)
    assert site.monitored is True and site.tariff_ils_per_kwh is None
    assert not [m for m in channel.sent if m.recipient == "999"]


def test_settings_post_requires_csrf_and_session(tc, container):
    owner_id = _owner(container)
    _add_site(container, owner_id, 111111)
    r = tc.post("/settings", data={"csrf": "x", "site": "111111"})
    assert (r.status_code, r.headers["location"]) == (303, "/")
    _sign_in(tc, container, owner_id)
    r = tc.post("/settings", data={"csrf": "x", "site": "111111", "tariff_111111": "1"})
    assert r.status_code == 403
    assert _site(container, 111111).tariff_ils_per_kwh is None


def test_token_stored_hashed(tc, container):
    path = _link(container, _owner(container))
    token = path.rsplit("/", 1)[1]
    with session_scope(container.session_factory) as s:
        rows = list(s.scalars(select(SettingsLink)))
        assert len(rows) == 1
        import hashlib
        assert rows[0].token_hash == hashlib.sha256(token.encode()).hexdigest()
        assert all(token not in str(getattr(rows[0], c.name))
                   for c in SettingsLink.__table__.columns)
