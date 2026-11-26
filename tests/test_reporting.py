import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest
import respx
from sqlalchemy import select

from solya.clock import FixedClock
from solya.crypto import TokenCipher
from solya.db import session_scope
from solya.messaging.base import Blocked, Invalid, Ok
from solya.messaging.outbox import Outbox
from solya.models import ApiLedger, ConnectRequest, DailyMetric, FaultRecord, OutboxEntry, Owner, Site, SiteToken
from solya.render import card as card_mod
from solya.rules.faults import ack_owner_faults
from solya.rules.ray import NullRay
from solya.services import reporting
from solya.services.reporting import Reporter
from solya.solaredge.budget import CreditBudget
from solya.solaredge.client import SolarEdgeClient
from solya.solaredge.oauth import TokenManager, TokenPair
from solya.text import he
from solya.text.he import WELCOME_1, WELCOME_2
from solya.text.rtl import RLM, rtl_text

from conftest import FakeChannel

FARM, HOME = 3033498, 4552265
BASE = "https://monitoringapi.solaredge.com/v2"
TOKEN_URL = f"{BASE}/oauth2/token"
NOW = datetime(2026, 11, 24, 15, 5, tzinfo=timezone.utc)
DAY = date(2026, 11, 24)
CHAT = 111
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

REGRESSION = (
    "ערב טוב אורן,\n"
    "משק: אין התראות פעילות. ‏317.0 קוט״ש, הכנסה ‏142.65 ₪\n"
    "בית: אין התראות פעילות. ‏85.0 קוט״ש, הכנסה מהזרמה ‏38.03 ₪\n"
    "סה״כ: ‏402.0 קוט״ש, ‏180.68 ₪ לפני מע״מ\n"
    "לכל שאלה אני כאן. המשך ערב טוב."
)

DEFAULTS = {
    FARM: dict(site="site_farm.json", overview="overview_farm_20261124.json",
               alerts="alerts_empty.json", inverters="inverters_status_ok.json",
               lifetime="lifetime_farm.json", devices="devices_farm.json"),
    HOME: dict(site="site_home.json", overview="overview_home_20261124.json",
               alerts="alerts_empty.json", inverters="inverters_status_home_ok.json",
               lifetime="lifetime_home.json", devices="devices_home.json"),
}
PATHS = dict(site="", overview="/overview", alerts="/alerts", inverters="/inverters/status",
             lifetime="/lifetime-energy", devices="/devices")


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(NOW)


def advancing_sleep(clock):
    async def sleep(seconds):
        clock.advance(timedelta(seconds=seconds))

    return sleep


def mock_site(load_fixture, site_id, **overrides):
    """Route every endpoint of one site; an override is a fixture name, a JSON body or a Response."""
    routes = {}
    for name, path in PATHS.items():
        spec = overrides.get(name, DEFAULTS[site_id][name])
        if isinstance(spec, httpx.Response):
            resp = spec
        elif isinstance(spec, str):
            resp = httpx.Response(200, json=load_fixture(f"solaredge/{spec}"))
        else:
            resp = httpx.Response(200, json=spec)
        routes[name] = respx.get(f"{BASE}/sites/{site_id}{path}").mock(return_value=resp)
    return routes


def site_body(load_fixture, site_id, last_update):
    body = load_fixture(f"solaredge/{DEFAULTS[site_id]['site']}")
    body["lastUpdateTime"] = last_update
    return body


class Harness:
    def __init__(self, db, clock, channel, reporter, tokens, retries, delayed):
        self.db = db
        self.clock = clock
        self.channel = channel
        self.reporter = reporter
        self.tokens = tokens
        self.retries = retries
        self.delayed = delayed

    def sent(self, kind=None):
        return [m for m in self.channel.sent if kind is None or m.kind == kind]


def seed_owner(s, owner_id, chat, name="אורן"):
    s.add(Owner(id=owner_id, display_name=name, telegram_chat_id=chat, active=True, created_at=NOW))


def seed_oren(db):
    with session_scope(db) as s:
        seed_owner(s, 1, CHAT)
        s.add(Site(se_site_id=FARM, owner_id=1, portal_name="אורן סופר", label_he="משק",
                   commercial=True, peak_power_kwp_reported=65.52, tz="Asia/Jerusalem",
                   tariff_ils_per_kwh=Decimal("0.45"), income_basis="production",
                   status="active", created_at=NOW))
        s.add(Site(se_site_id=HOME, owner_id=1, portal_name="IL 360 - אורן סופר", label_he="בית",
                   card_name="IL 360", peak_power_kwp_reported=26.55, tz="Asia/Jerusalem",
                   tariff_ils_per_kwh=Decimal("0.48"), income_basis="export",
                   status="active", created_at=NOW))


@pytest.fixture
async def h(db, settings, clock):
    seed_oren(db)
    channel = FakeChannel()
    retries, delayed = [], []
    async with httpx.AsyncClient() as http:
        budget = CreditBudget(db, settings, clock, sleep=advancing_sleep(clock))
        box = {}
        tokens = TokenManager(
            db, settings, TokenCipher(settings.token_encryption_key.get_secret_value()),
            http, budget, clock, lambda sid: box["reporter"].on_reauth_needed(sid),
        )
        for sid in (FARM, HOME):
            tokens.store_new_grant(sid, TokenPair("access", "refresh", 30 * 86400))
        client = SolarEdgeClient(http, settings, tokens, budget, db, clock)
        reporter = Reporter(
            db, client, tokens, Outbox(db, clock), channel, clock, settings, NullRay(),
            lambda sid, at: retries.append((sid, at)),
            delay_send=lambda seconds, factory: delayed.append((seconds, factory)),
            http=http,
        )
        box["reporter"] = reporter
        yield Harness(db, clock, channel, reporter, tokens, retries, delayed)


@pytest.fixture
def cards(monkeypatch):
    seen = []
    real = card_mod.render_card

    def capture(data):
        seen.append(data)
        return real(data)

    monkeypatch.setattr(reporting, "render_card", capture)
    return seen


async def pull_both(h, load_fixture, farm=None, home=None):
    with respx.mock:
        mock_site(load_fixture, FARM, **(farm or {}))
        mock_site(load_fixture, HOME, **(home or {}))
        await h.reporter.evening_pull(FARM)
        await h.reporter.evening_pull(HOME)


async def test_evening_report_regression_20261124(h, load_fixture, cards):
    await pull_both(h, load_fixture)
    await h.reporter.evening_report(1, DAY)

    assert len(h.channel.sent) == 1
    msg = h.channel.sent[0]
    assert msg.kind == "report"
    assert msg.recipient == str(CHAT)
    assert msg.dedupe_key == "report:1:2026-11-24"
    assert msg.image_png.startswith(PNG_MAGIC)
    assert msg.text == REGRESSION

    (data,) = cards
    assert [(r.name, r.production_wh, r.income_ils, r.note) for r in data.rows] == [
        ("אורן סופר", 316990.0, Decimal("142.65"), None),
        ("IL 360", 85039.0, Decimal("38.03"), None),
    ]
    assert data.total == (402029.0, Decimal("180.68"))
    assert data.ray is None
    assert data.header.startswith("שלישי, 24 בנובמבר · נכון ל־")
    assert data.footnote == "הכנסה משוערת לפני מע״מ"
    assert data.site_count == 2
    assert [r.income_label for r in data.rows] == ["הכנסה", "הכנסה מהזרמה"]
    assert all(r.status == ("ok", "אין התראות פעילות") for r in data.rows)
    with session_scope(h.db) as s:
        assert s.get(DailyMetric, (FARM, DAY)).lifetime_wh == 671421060
        assert s.get(DailyMetric, (HOME, DAY)).income_ils == Decimal("38.03")

    # A restart that runs the report job again sends nothing new.
    await h.reporter.evening_report(1, DAY)
    assert len(h.channel.sent) == 1


async def test_fan_alert_sent_once(h, load_fixture):
    for _ in range(2):
        with respx.mock:
            mock_site(load_fixture, FARM, alerts="alerts_fan.json")
            await h.reporter.evening_pull(FARM)

    faults = h.sent("fault")
    assert len(faults) == 1
    text = faults[0].text
    assert "FAN_ALERT_1" in text and "7E04E44D-B3" in text
    assert "(משק)" in text
    assert f"בשעה {RLM}06:50" in text  # alert firstTrigger, site-local
    assert faults[0].dedupe_key == f"fault:{FARM}:alert:101"
    with session_scope(h.db) as s:
        (rec,) = s.scalars(select(FaultRecord)).all()
        assert rec.notified_at is not None


async def test_fault_alert_single_site_has_no_label(h, load_fixture):
    with session_scope(h.db) as s:
        s.delete(s.get(SiteToken, HOME))
        s.delete(s.get(Site, HOME))
    with respx.mock:
        mock_site(load_fixture, FARM, alerts="alerts_fan.json")
        await h.reporter.evening_pull(FARM)
    (msg,) = h.sent("fault")
    assert "(משק)" not in msg.text
    assert "במערכת הסולארית שלך: " in msg.text


async def test_undelivered_fault_not_marked_notified(h, load_fixture):
    h.channel.result = Blocked()
    with respx.mock:
        mock_site(load_fixture, FARM, alerts="alerts_fan.json")
        await h.reporter.evening_pull(FARM)
    with session_scope(h.db) as s:
        (rec,) = s.scalars(select(FaultRecord)).all()
        assert rec.notified_at is None


async def test_closed_alert_followup(h, load_fixture):
    with respx.mock:
        mock_site(load_fixture, FARM, alerts="alerts_fan.json")
        await h.reporter.evening_pull(FARM)
    with respx.mock:
        mock_site(load_fixture, FARM, alerts="alerts_empty.json")
        await h.reporter.evening_pull(FARM)

    (closed,) = [m for m in h.channel.sent if m.dedupe_key.startswith("closed:")]
    assert closed.dedupe_key == f"closed:{FARM}:alert:101"
    assert "FAN_ALERT_1" in closed.text and "נסגרה" in closed.text
    with session_scope(h.db) as s:
        (rec,) = s.scalars(select(FaultRecord)).all()
        assert rec.closed_at is not None and rec.closed_notified_at is not None
    assert len(h.sent("fault")) == 2  # the alert and the follow-up


async def test_report_mixed_tariffs_total_kwh_only(h, load_fixture, cards):
    """Final fix 4 (spec section 1): one site without a tariff: the total shows kWh only,
    in the text and on the card; the priced site keeps its ₪."""
    with session_scope(h.db) as s:
        s.get(Site, HOME).tariff_ils_per_kwh = None
    await pull_both(h, load_fixture)
    await h.reporter.evening_report(1, DAY)
    (report,) = h.sent("report")
    lines = report.text.split("\n")
    assert "סה״כ: ‏402.0 קוט״ש" in lines
    assert "משק: אין התראות פעילות. ‏317.0 קוט״ש, הכנסה ‏142.65 ₪" in lines
    (data,) = cards
    assert data.total == (402029.0, None)
    assert [r.income_ils for r in data.rows] == [Decimal("142.65"), None]


async def test_comm_loss_message_and_report(h, load_fixture, cards):
    stale = site_body(load_fixture, FARM, "2026-11-24T14:05:00+02:00")  # 3 h before 15:05Z
    for _ in range(2):  # the second stale pull must not repeat the message
        await pull_both(h, load_fixture, farm=dict(site=stale, alerts="alerts_fan.json"))

    (loss,) = h.sent("comm_loss")
    assert loss.dedupe_key == f"comm:{FARM}:2026-11-24T12:05:00+00:00"
    assert f"משק: אין תקשורת עם המערכת מאז {RLM}14:05." in loss.text
    assert h.sent("fault") == []  # R12: stale data never opens faults
    with session_scope(h.db) as s:
        site = s.get(Site, FARM)
        assert site.comm_state == "loss"
        assert site.comm_since == datetime(2026, 11, 24, 12, 5, tzinfo=timezone.utc)
        assert s.scalars(select(FaultRecord)).all() == []

    await h.reporter.evening_report(1, DAY)
    (report,) = h.sent("report")
    farm_line = [line for line in report.text.split("\n") if line.startswith("משק:")]
    assert farm_line == [f"משק: אין תקשורת עם המערכת מאז {RLM}14:05. אני עוקבת ואעדכן."]
    assert "קוט״ש" not in farm_line[0]
    assert "סה״כ" not in report.text
    assert "בית: אין התראות פעילות." in report.text
    assert [r.note for r in cards[0].rows] == ["אין תקשורת", None]
    assert cards[0].total is None

    # Fresh data again: one restored message.
    with respx.mock:
        mock_site(load_fixture, FARM)
        await h.reporter.evening_pull(FARM)
    (back,) = [m for m in h.sent("comm_loss") if m is not loss]
    assert "משק: התקשורת עם המערכת חזרה." in back.text
    with session_scope(h.db) as s:
        assert s.get(Site, FARM).comm_state == "ok"


async def test_comm_loss_never_reported_uses_pull_time(h, load_fixture):
    body = load_fixture("solaredge/site_farm.json")
    del body["lastUpdateTime"]
    with respx.mock:
        mock_site(load_fixture, FARM, site=body)
        await h.reporter.evening_pull(FARM)
    (loss,) = h.sent("comm_loss")
    assert f"מאז {RLM}17:05." in loss.text
    await h.reporter.evening_report(1, DAY)
    (report,) = h.sent("report")
    assert f"משק: אין תקשורת עם המערכת מאז {RLM}17:05." in report.text


async def test_retry_after_missing(h, load_fixture, cards):
    await pull_both(h, load_fixture, farm=dict(overview=httpx.Response(500)))
    await h.reporter.evening_report(1, DAY)

    (first,) = h.sent("report")
    lines = first.text.split("\n")
    assert lines[1] == "משק: המידע עדיין לא הגיע אלי, אני על זה."
    assert "317.0" not in first.text and "סה״כ" not in first.text
    assert [r.note for r in cards[0].rows] == ["אין נתונים", None]
    assert len(h.retries) == 1
    site_id, at = h.retries[0]
    assert site_id == FARM
    pulled_at = at - timedelta(minutes=30)
    assert NOW <= pulled_at <= NOW + timedelta(minutes=2)

    # The day's retry is scheduled only once.
    with respx.mock:
        mock_site(load_fixture, FARM, overview=httpx.Response(500))
        await h.reporter.evening_pull(FARM)
    assert len(h.retries) == 1

    h.clock.set(at)
    with respx.mock:
        mock_site(load_fixture, FARM)
        await h.reporter.retry_pull_and_report(FARM, DAY)

    second = h.sent("report")[1]
    assert second.dedupe_key == f"report:1:2026-11-24:retry:{FARM}"
    assert second.text == REGRESSION
    assert second.image_png.startswith(PNG_MAGIC)


async def test_retry_still_missing_sends_nothing(h, load_fixture):
    await pull_both(h, load_fixture, farm=dict(overview="overview_null.json"))
    await h.reporter.evening_report(1, DAY)
    with respx.mock:
        mock_site(load_fixture, FARM, overview="overview_null.json")
        await h.reporter.retry_pull_and_report(FARM, DAY)
    assert len(h.sent("report")) == 1


class RoutingChannel(FakeChannel):
    """Blocked for one chat, an unexpected error for another, Ok for the rest."""

    def __init__(self, blocked: str, broken: str):
        super().__init__()
        self.blocked, self.broken = blocked, broken

    async def send(self, msg):
        self.sent.append(msg)
        if msg.recipient == self.blocked:
            return Blocked()
        if msg.recipient == self.broken:
            raise RuntimeError("network down")
        return Ok()


async def test_one_owner_blocked_others_sent(h, load_fixture):
    with session_scope(h.db) as s:
        for owner_id, chat, site_id in ((2, 222, 9002), (3, 333, 9003)):
            seed_owner(s, owner_id, chat, name="דנה")
            s.add(Site(se_site_id=site_id, owner_id=owner_id, portal_name=f"p{site_id}",
                       tz="Asia/Jerusalem", status="active", created_at=NOW))
            s.add(DailyMetric(site_id=site_id, local_date=DAY, production_wh=1000.0,
                              comm_state="ok", as_of=NOW, computed_at=NOW))
    await pull_both(h, load_fixture)
    h.reporter.channel = h.channel = RoutingChannel(blocked="222", broken="333")

    for owner_id in (2, 3, 1):
        await h.reporter.evening_report(owner_id, DAY)

    assert [m.recipient for m in h.channel.sent] == ["222", "333", str(CHAT)]
    assert h.channel.sent[-1].text == REGRESSION
    with session_scope(h.db) as s:
        assert s.get(Owner, 2).active is False
        assert s.get(OutboxEntry, "report:2:2026-11-24").status == "blocked"
        assert s.get(OutboxEntry, "report:1:2026-11-24").status == "sent"

    # No retries for the blocked owner.
    await h.reporter.evening_report(2, DAY)
    assert [m.recipient for m in h.channel.sent].count("222") == 1


async def test_report_with_reauth_site(h, load_fixture, cards):
    with session_scope(h.db) as s:
        s.get(Site, HOME).status = "reauth_needed"
    with respx.mock:
        mock_site(load_fixture, FARM)
        await h.reporter.evening_pull(FARM)
    await h.reporter.evening_report(1, DAY)

    (report,) = h.sent("report")
    assert report.text == (
        "ערב טוב אורן,\n"
        "בית: החיבור ל־SolarEdge פג, שלחתי קישור לחיבור מחדש.\n"
        "משק: אין התראות פעילות. ‏317.0 קוט״ש, הכנסה ‏142.65 ₪\n"
        "לכל שאלה אני כאן. המשך ערב טוב."
    )
    assert [(r.name, r.note) for r in cards[0].rows] == [
        ("אורן סופר", None), ("IL 360", "נדרש חיבור מחדש")]
    assert cards[0].total is None


async def test_report_skips_inactive_owner(h, load_fixture):
    await pull_both(h, load_fixture)
    with session_scope(h.db) as s:
        s.get(Owner, 1).active = False
    await h.reporter.evening_report(1, DAY)
    assert h.channel.sent == []


async def test_reminder_after_3h_then_ack_stops(h, load_fixture):
    with respx.mock:
        mock_site(load_fixture, FARM, alerts="alerts_fan.json")
        await h.reporter.evening_pull(FARM)
    assert len(h.sent("fault")) == 1

    h.clock.advance(timedelta(hours=2))
    await h.reporter.send_reminders()
    assert len(h.sent("fault")) == 1

    h.clock.advance(timedelta(hours=1, minutes=5))
    await h.reporter.send_reminders()
    await h.reporter.send_reminders()
    reminders = [m for m in h.channel.sent if m.dedupe_key.startswith("reminder:")]
    assert len(reminders) == 1
    assert reminders[0].dedupe_key == f"reminder:{FARM}:alert:101"
    assert "תזכורת" in reminders[0].text and "FAN_ALERT_1" in reminders[0].text
    with session_scope(h.db) as s:
        assert s.scalars(select(FaultRecord)).one().reminded_at is not None


async def test_ack_before_reminder_suppresses_it(h, load_fixture):
    with respx.mock:
        mock_site(load_fixture, FARM, alerts="alerts_fan.json")
        await h.reporter.evening_pull(FARM)
    h.clock.advance(timedelta(hours=1))
    with session_scope(h.db) as s:
        assert ack_owner_faults(s, 1, h.clock.now()) == 1
    h.clock.advance(timedelta(hours=3))
    await h.reporter.send_reminders()
    assert not [m for m in h.channel.sent if m.dedupe_key.startswith("reminder:")]


async def test_midday_sends_fault_never_report(h, load_fixture):
    h.clock.set(datetime(2026, 11, 24, 10, 30, tzinfo=timezone.utc))
    fresh = site_body(load_fixture, FARM, "2026-11-24T12:25:00+02:00")
    with respx.mock:
        r = mock_site(load_fixture, FARM, site=fresh, alerts="alerts_fan.json",
                      inverters="inverters_status_fault.json")
        await h.reporter.midday_check(FARM)
    assert not r["lifetime"].called
    assert h.sent("report") == []
    texts = [m.text for m in h.sent("fault")]
    assert len(texts) == 2
    assert any("FAN_ALERT_1" in t for t in texts) and any("LOCKED_GRID" in t for t in texts)
    with session_scope(h.db) as s:
        assert s.get(DailyMetric, (FARM, DAY)) is None


async def test_midday_budget_exceeded_skips(h, settings, caplog):
    settings.budget_monthly_credits = 10
    with session_scope(h.db) as s:
        for _ in range(9):
            s.add(ApiLedger(ts=NOW - timedelta(days=1), site_id=FARM, endpoint="x",
                            http_status=200, credited=True))
        s.get(Site, FARM).last_update_time = None
    caplog.set_level(logging.WARNING)
    with respx.mock:  # no routes: any HTTP call would fail the test
        await h.reporter.midday_check(FARM)
    assert h.channel.sent == []
    assert "budget" in caplog.text.lower()
    with session_scope(h.db) as s:
        assert s.get(Site, FARM).comm_state == "ok"


async def test_on_reauth_needed_sends_link_and_admin(h, settings):
    settings.admin_chat_ids = [999]
    await h.reporter.on_reauth_needed(HOME)

    owner_msg, admin_msg = h.channel.sent
    assert owner_msg.recipient == str(CHAT)
    assert owner_msg.dedupe_key == f"reauth:{HOME}:2026-11-24"
    assert "החיבור ל־SolarEdge עבור בית פג" in owner_msg.text
    assert "https://solya.example/connect/" in owner_msg.text
    assert admin_msg.recipient == "999"
    assert admin_msg.text == f"נדרש חיבור מחדש לאתר {RLM}{HOME}"
    with session_scope(h.db) as s:
        (req,) = s.scalars(select(ConnectRequest)).all()
        assert req.owner_id == 1 and owner_msg.text.count(req.token) == 1


async def test_evening_pull_auth_expired_runs_reauth_flow(h, load_fixture, settings):
    h.tokens.store_new_grant(HOME, TokenPair("old", "dead-refresh", 60))
    with respx.mock:
        respx.post(TOKEN_URL).mock(return_value=httpx.Response(400, json={"error": "invalid_grant"}))
        await h.reporter.evening_pull(HOME)  # must not raise
    with session_scope(h.db) as s:
        assert s.get(Site, HOME).status == "reauth_needed"
    (link,) = h.channel.sent
    assert link.dedupe_key == f"reauth:{HOME}:2026-11-24"


async def test_on_site_connected_welcomes(h):
    with session_scope(h.db) as s:
        site = s.get(Site, FARM)
    await h.reporter.on_site_connected(site, is_first=True)

    texts = [m.text for m in h.channel.sent]
    connected = rtl_text("המערכת משק חוברה בהצלחה. יש לך עוד מערכת? שלחו /connect.")
    assert texts == [connected, rtl_text(WELCOME_1)]
    assert f"{RLM}24/7" in texts[1]
    assert [seconds for seconds, _ in h.delayed] == [60]
    await h.delayed[0][1]()
    assert h.channel.sent[-1].text == rtl_text(WELCOME_2)

    h.clock.advance(timedelta(days=1))  # a later reconnect of the same site
    await h.reporter.on_site_connected(site, is_first=False)
    assert len(h.channel.sent) == 4 and len(h.delayed) == 1
    assert h.channel.sent[-1].text == connected


async def test_on_site_connected_already_message(h):
    await h.reporter.on_site_connected(HOME, is_first=False, already=True)
    (msg,) = h.channel.sent
    assert msg.text == rtl_text("המערכת בית כבר מחוברת. בחלון של SolarEdge בחרו מערכת אחרת.")
    assert "\u200e" not in msg.text
    assert not h.delayed


async def test_on_site_connected_command_has_no_direction_marks(h):
    await h.reporter.on_site_connected(HOME, is_first=False)
    (msg,) = h.channel.sent
    assert msg.text.endswith("שלחו /connect.")
    assert "\u200f/connect" not in msg.text and "\u200e" not in msg.text


async def test_on_site_connected_replans_owner(h):
    """R26: connecting a site re-plans today's jobs for its owner."""
    replanned = []
    h.reporter.replan_owner = replanned.append
    await h.reporter.on_site_connected(FARM, is_first=False)
    assert replanned == [1]


async def test_default_delay_send_runs_later(h):
    ran = []

    async def factory():
        ran.append(True)

    h.reporter._default_delay_send(0, factory)
    assert ran == []
    await asyncio.gather(*h.reporter._background)
    assert ran == [True] and not h.reporter._background


async def test_keepalive_refreshes_idle_tokens(h):
    with session_scope(h.db) as s:
        s.get(SiteToken, HOME).refreshed_at = NOW - timedelta(days=8)
        s.get(SiteToken, FARM).refreshed_at = NOW - timedelta(days=2)
    with respx.mock:
        route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(
            200, json={"access_token": "a2", "refresh_token": "r2", "expires_in": 7200}))
        await h.reporter.keepalive()
    assert route.call_count == 1
    with session_scope(h.db) as s:
        assert s.get(SiteToken, HOME).refreshed_at == NOW
        assert s.get(SiteToken, FARM).refreshed_at == NOW - timedelta(days=2)


async def test_keepalive_weekly_from_daily_job(h):
    """A refresh made by last week's run (a moment after it started) is due again today."""
    with session_scope(h.db) as s:
        s.get(SiteToken, HOME).refreshed_at = NOW - timedelta(days=7) + timedelta(seconds=2)
        s.get(SiteToken, FARM).refreshed_at = NOW - timedelta(days=6)
    with respx.mock:
        route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(
            200, json={"access_token": "a2", "refresh_token": "r2", "expires_in": 7200}))
        await h.reporter.keepalive()
    assert route.call_count == 1
    with session_scope(h.db) as s:
        assert s.get(SiteToken, HOME).refreshed_at == NOW
        assert s.get(SiteToken, FARM).refreshed_at == NOW - timedelta(days=6)


async def test_keepalive_includes_paused_skips_reauth(h):
    with session_scope(h.db) as s:
        for sid in (FARM, HOME):
            s.get(SiteToken, sid).refreshed_at = NOW - timedelta(days=8)
        s.get(Site, FARM).status = "paused"
        s.get(Site, HOME).status = "reauth_needed"
    with respx.mock:
        route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(
            200, json={"access_token": "a2", "refresh_token": "r2", "expires_in": 7200}))
        await h.reporter.keepalive()
    assert route.call_count == 1
    with session_scope(h.db) as s:
        assert s.get(SiteToken, FARM).refreshed_at == NOW
        assert s.get(SiteToken, HOME).refreshed_at == NOW - timedelta(days=8)


async def test_stale_pull_leaves_open_faults_open(h, load_fixture):
    with respx.mock:
        mock_site(load_fixture, FARM, alerts="alerts_fan.json")
        await h.reporter.evening_pull(FARM)
    stale = site_body(load_fixture, FARM, "2026-11-24T14:05:00+02:00")
    with respx.mock:
        mock_site(load_fixture, FARM, site=stale, alerts="alerts_empty.json")
        await h.reporter.evening_pull(FARM)
    assert not [m for m in h.channel.sent if m.dedupe_key.startswith("closed:")]
    with session_scope(h.db) as s:
        assert s.scalars(select(FaultRecord)).one().closed_at is None


async def test_failed_alerts_call_closes_nothing(h, load_fixture):
    with respx.mock:
        mock_site(load_fixture, FARM, alerts="alerts_fan.json")
        await h.reporter.evening_pull(FARM)
    with respx.mock:
        mock_site(load_fixture, FARM, alerts=httpx.Response(500))
        await h.reporter.evening_pull(FARM)
    with session_scope(h.db) as s:
        assert s.scalars(select(FaultRecord)).one().closed_at is None


class FlakyChannel(FakeChannel):
    """The first send raises a Telegram timeout; later sends succeed."""

    def __init__(self):
        super().__init__()
        self.delivered = []

    async def send(self, msg):
        self.sent.append(msg)
        if len(self.sent) == 1:
            from telegram.error import TimedOut

            raise TimedOut()
        self.delivered.append(msg)
        return Ok()


@pytest.mark.parametrize("first", ["timeout", "invalid"])
async def test_undelivered_fault_alert_retried_next_pull(h, load_fixture, first):
    if first == "timeout":
        h.reporter.channel = h.channel = FlakyChannel()
    else:
        h.channel.result = [Invalid("bad request"), Ok()]
    for _ in range(3):
        with respx.mock:
            mock_site(load_fixture, FARM, alerts="alerts_fan.json")
            await h.reporter.evening_pull(FARM)

    faults = h.sent("fault")
    assert len(faults) == 2  # the failed attempt, then one successful alert; none after
    assert {m.dedupe_key for m in faults} == {f"fault:{FARM}:alert:101"}
    if first == "timeout":
        assert len(h.channel.delivered) == 1
    with session_scope(h.db) as s:
        assert s.scalars(select(FaultRecord)).one().notified_at is not None
        assert s.get(OutboxEntry, f"fault:{FARM}:alert:101").status == "sent"


async def test_two_sites_recover_on_separate_retries(h, load_fixture):
    await pull_both(h, load_fixture, farm=dict(overview=httpx.Response(500)),
                    home=dict(overview=httpx.Response(500)))
    await h.reporter.evening_report(1, DAY)
    assert sorted(sid for sid, _ in h.retries) == [FARM, HOME]

    for sid in (FARM, HOME):
        h.clock.set(dict(h.retries)[sid])
        with respx.mock:
            mock_site(load_fixture, sid)
            await h.reporter.retry_pull_and_report(sid, DAY)

    reports = h.sent("report")
    assert [m.dedupe_key for m in reports] == [
        "report:1:2026-11-24",
        f"report:1:2026-11-24:retry:{FARM}",
        f"report:1:2026-11-24:retry:{HOME}",
    ]
    assert "בית: המידע עדיין לא הגיע אלי, אני על זה." in reports[1].text
    assert reports[2].text == REGRESSION


async def test_failed_site_call_is_no_data_with_retry(h, load_fixture, cards):
    """I1: /sites failing (overview fine) means unknown freshness: no_data and a retry,
    never a comm_loss day built from yesterday's lastUpdateTime."""
    with session_scope(h.db) as s:  # what the previous evening's pull left behind
        s.get(Site, FARM).last_update_time = NOW - timedelta(hours=24)
    await pull_both(h, load_fixture, farm=dict(site=httpx.Response(503)))
    with session_scope(h.db) as s:
        row = s.get(DailyMetric, (FARM, DAY))
        assert row.comm_state == "no_data" and row.production_wh is None
        assert s.get(Site, FARM).comm_state == "ok"
    assert [sid for sid, _ in h.retries] == [FARM]
    assert h.sent("comm_loss") == []

    await h.reporter.evening_report(1, DAY)
    (report,) = h.sent("report")
    assert report.text.split("\n")[1] == "משק: המידע עדיין לא הגיע אלי, אני על זה."

    h.clock.set(h.retries[0][1])
    with respx.mock:
        mock_site(load_fixture, FARM)
        await h.reporter.retry_pull_and_report(FARM, DAY)
    assert h.sent("report")[1].text == REGRESSION


async def test_late_retry_after_dark_no_false_comm_loss(h, load_fixture):
    """I2 probe: overview 500, the retry runs 12 min late (lastUpdateTime 16:45, retry
    17:47 local). No comm-loss alert; the retry report goes out."""
    await pull_both(h, load_fixture, farm=dict(overview=httpx.Response(500)))
    await h.reporter.evening_report(1, DAY)
    (_, at) = h.retries[0]
    h.clock.set(datetime(2026, 11, 24, 15, 47, tzinfo=timezone.utc))
    assert h.clock.now() > at
    with respx.mock:
        mock_site(load_fixture, FARM)
        await h.reporter.retry_pull_and_report(FARM, DAY)
    assert h.sent("comm_loss") == []
    assert len(h.sent("report")) == 2
    assert h.sent("report")[1].text == REGRESSION


async def test_restart_in_grace_report_waits_for_pulls(h, load_fixture, settings, monkeypatch):
    """I3: after a restart 40 min past sunset, jobs that share a run time run
    concurrently (as APScheduler would); the report must still see both pulls."""
    from solya.scheduler import SchedulerService, sunset_utc

    http = h.reporter._client._http
    real_get = http.get

    async def slow_get(*args, **kwargs):  # network latency, so concurrent jobs interleave
        await asyncio.sleep(0.01)
        return await real_get(*args, **kwargs)

    monkeypatch.setattr(http, "get", slow_get)

    with session_scope(h.db) as s:
        sunset = sunset_utc(s.get(Site, FARM), DAY)
    h.clock.set(sunset + timedelta(minutes=40))
    svc = SchedulerService(h.reporter, h.db, settings, h.clock)
    svc.plan_today()
    by_time = {}
    for job in svc.scheduler.get_jobs():
        by_time.setdefault(job.trigger.run_date, []).append(job)
    with respx.mock:
        mock_site(load_fixture, FARM)
        mock_site(load_fixture, HOME)
        for run_at in sorted(by_time):
            h.clock.set(max(run_at, h.clock.now()))
            await asyncio.gather(*(svc._run(*job.args) for job in by_time[run_at]))
    (report,) = h.sent("report")
    assert report.text == REGRESSION
    assert h.retries == []


def home_status(*samples):
    values = [{"timestamp": f"2026-11-24T{hhmm}:00+02:00", "value": v} for hhmm, v in samples]
    return {"inverters": {"7F11A2B3-C4": {"inverterMode": {"values": values}}}}


async def test_evening_pull_sees_daytime_inverter_fault(h, load_fixture):
    """I4: a home site (evening pull only) with a midday LOCKED_GRID gets the alert,
    although the last sample at dusk is no longer a fault mode."""
    status = home_status(("09:00", "MPPT"), ("11:00", "LOCKED_GRID"), ("12:00", "MPPT"),
                         ("16:00", "SLEEPING"))
    with respx.mock:
        mock_site(load_fixture, HOME, inverters=status)
        await h.reporter.evening_pull(HOME)
    (alert,) = h.sent("fault")
    assert "LOCKED_GRID" in alert.text and "7F11A2B3-C4" in alert.text
    assert alert.dedupe_key == f"fault:{HOME}:mode:7F11A2B3-C4"


async def test_internal_fault_close_does_not_claim_solaredge_closed_it(h, load_fixture):
    """I4/R24: a mode fault clearing gets the "back to normal" text, not the
    "according to SolarEdge the alert closed" text."""
    with respx.mock:
        mock_site(load_fixture, HOME, inverters=home_status(("13:00", "LOCKED_GRID")))
        await h.reporter.evening_pull(HOME)
    with respx.mock:
        mock_site(load_fixture, HOME, inverters=home_status(("13:00", "MPPT")))
        await h.reporter.evening_pull(HOME)
    (closed,) = [m for m in h.channel.sent if m.dedupe_key.startswith("closed:")]
    assert closed.text == rtl_text(
        "ערב טוב אורן,\n"
        "לפי הנתונים מ־SolarEdge הממיר 7F11A2B3-C4 חזר לפעול כרגיל. "
        "האם בוצע טיפול? מחכה לעדכון שלך."
    )
    assert "ההתראה" not in closed.text and "נסגרה" not in closed.text


async def test_zero_production_closes_silently(h, load_fixture):
    """I4/R24: a closed ZERO_PRODUCTION has no inverter to name, so no follow-up."""
    zero = load_fixture("solaredge/overview_home_20261124.json")
    zero["production"]["total"] = 0
    with respx.mock:
        mock_site(load_fixture, HOME, overview=zero)
        await h.reporter.evening_pull(HOME)
    assert [m.dedupe_key for m in h.sent("fault")] == [f"fault:{HOME}:zero:2026-11-24"]
    h.clock.set(NOW + timedelta(days=1))
    with respx.mock:
        mock_site(load_fixture, HOME, site=site_body(load_fixture, HOME, "2026-11-25T16:40:00+02:00"))
        await h.reporter.evening_pull(HOME)
    with session_scope(h.db) as s:
        (rec,) = s.scalars(select(FaultRecord)).all()
        assert rec.closed_at is not None and rec.closed_notified_at is None
    assert [m for m in h.channel.sent if m.dedupe_key.startswith("closed:")] == []


async def test_silent_pull_leaves_notified_fault_open(h, load_fixture):
    """A silent `solya pull` must not close a notified fault: closing it there would drop
    the spec §5.5 follow-up for good. The next notifying pull closes it and says so."""
    with respx.mock:
        mock_site(load_fixture, HOME, inverters=home_status(("13:00", "LOCKED_GRID")))
        await h.reporter.evening_pull(HOME)
    assert len(h.sent("fault")) == 1
    with respx.mock:
        mock_site(load_fixture, HOME, inverters=home_status(("13:00", "MPPT")))
        await h.reporter.manual_pull(HOME, "evening", notify=False)
    with session_scope(h.db) as s:
        (rec,) = s.scalars(select(FaultRecord)).all()
        assert rec.notified_at is not None and rec.closed_at is None
    assert [m for m in h.channel.sent if m.dedupe_key.startswith("closed:")] == []
    with respx.mock:
        mock_site(load_fixture, HOME, inverters=home_status(("13:00", "MPPT")))
        await h.reporter.evening_pull(HOME)
    with session_scope(h.db) as s:
        (rec,) = s.scalars(select(FaultRecord)).all()
        assert rec.closed_at is not None and rec.closed_notified_at is not None
    (closed,) = [m for m in h.channel.sent if m.dedupe_key.startswith("closed:")]
    assert "7F11A2B3-C4" in closed.text


def test_card_row_fault_status():
    from solya.rules.daily import FaultView, SiteDay

    fv = [FaultView("FAN_ALERT_1", None, NOW, "alert"), FaultView("X", None, NOW, "alert")]
    d = SiteDay(site_id=1, label="משק", card_name="משק", state="ok", production_wh=1000.0,
                income_ils=Decimal("1.00"), open_faults=fv)
    row = reporting._card_row(d)
    assert row.status == ("fault", "התראה פתוחה: FAN_ALERT_1 +1")
    assert row.income_label == "הכנסה"


def test_report_header_as_of(h):
    seed_stamps = {
        FARM: datetime(2026, 11, 25, 15, 6, tzinfo=timezone.utc),
        HOME: datetime(2026, 11, 25, 15, 1, tzinfo=timezone.utc),
    }
    with session_scope(h.db) as s:
        for sid, ts in seed_stamps.items():
            s.get(Site, sid).last_update_time = ts
        now = datetime(2026, 11, 25, 15, 10, tzinfo=timezone.utc)
        card, _ = reporting.build_owner_report(
            s, 1, date(2026, 11, 25), now, h.reporter._settings, NullRay()
        )
        assert card.header == "רביעי, 25 בנובמבר · נכון ל־17:01"
        s.get(Site, HOME).last_update_time = None
        card, _ = reporting.build_owner_report(
            s, 1, date(2026, 11, 25), now, h.reporter._settings, NullRay()
        )
        assert card.header == "רביעי, 25 בנובמבר"


async def test_tariff_hint_weekly(h):
    with session_scope(h.db) as s:
        s.get(Site, FARM).tariff_ils_per_kwh = None
        for sid, d in [(f, d) for f in (FARM, HOME) for d in (DAY, date(2026, 11, 25))]:
            s.merge(DailyMetric(site_id=sid, local_date=d, production_wh=1000.0,
                                    comm_state="ok", as_of=NOW, computed_at=NOW))
    await h.reporter.evening_report(1, DAY)
    await h.reporter.evening_report(1, date(2026, 11, 25))
    first, second = h.sent("report")
    assert he.TARIFF_HINT in first.text.replace(RLM, "")
    assert he.TARIFF_HINT not in second.text.replace(RLM, "")
    with session_scope(h.db) as s:
        assert s.get(OutboxEntry, "tariff_hint:1:2026-W48") is not None


D26 = date(2026, 11, 26)
NOW26 = datetime(2026, 11, 26, 15, 5, tzinfo=timezone.utc)


def _seed_ray_day(h, farm_ray=91.0, home_ray=92.0):
    with session_scope(h.db) as s:
        for sid, wh, ray in ((FARM, 310100.0, farm_ray), (HOME, 86600.0, home_ray)):
            s.merge(DailyMetric(site_id=sid, local_date=D26, production_wh=wh, ray=ray,
                                income_ils=Decimal("10.00"), comm_state="ok",
                                as_of=NOW26, computed_at=NOW26))


def _report26(h, visible, **kw):
    _seed_ray_day(h, **kw)
    h.reporter._settings.ray_visible = visible
    with session_scope(h.db) as s:
        return reporting.build_owner_report(s, 1, D26, NOW26, h.reporter._settings, NullRay())


async def test_ray_hidden_when_not_visible(h):
    card, text = _report26(h, False)
    assert card.ray is None
    assert all(r.ray is None for r in card.rows)
    assert "RAY" not in text


async def test_ray_visible_weighted_by_production(h):
    card, text = _report26(h, True)
    assert round(card.ray) == 91
    assert [round(r.ray) for r in card.rows] == [91, 92]
    assert text.replace(RLM, "").count("סה״כ: RAY 91, 396.7 קוט״ש") == 1


async def test_ray_overall_skips_none_site(h):
    card, _ = _report26(h, True, farm_ray=None, home_ray=92.0)
    assert card.ray == 92.0
    assert card.rows[0].ray is None


async def test_ray_all_none_no_gauge(h):
    card, text = _report26(h, True, farm_ray=None, home_ray=None)
    assert card.ray is None
    assert "RAY" not in text


def test_weighted_ray_production_weights():
    from solya.rules.ray import weighted_ray

    assert weighted_ray([(None, 5.0), (80.0, 100.0)]) == 80.0
    assert weighted_ray([(90.0, 0.0), (80.0, 0.0)]) is None
    assert weighted_ray([(None, None)]) is None
    assert weighted_ray([(90.0, 300.0), (60.0, 100.0)]) == 82.5
