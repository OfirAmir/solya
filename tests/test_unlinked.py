"""Owners with no Telegram chat yet (web sign-ups) and sites the owner chose not to monitor."""
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest
import respx
from sqlalchemy import select

from solya.clock import FixedClock
from solya.crypto import TokenCipher
from solya.db import session_scope
from solya.messaging.outbox import Outbox
from solya.models import ConnectRequest, DailyMetric, FaultRecord, OutboxEntry, Owner, RawResponse, Site, SiteToken
from solya.rules.ray import NullRay
from solya.scheduler import plan_day_jobs
from solya.services import answers
from solya.services import onboarding as ob
from solya.services.reporting import Reporter, build_owner_report, owner_chat
from solya.solaredge.budget import CreditBudget
from solya.solaredge.client import SolarEdgeClient
from solya.solaredge.models import SiteInfo
from solya.solaredge.oauth import TokenManager, TokenPair

from conftest import FakeChannel
from test_reporting import DAY, FARM, HOME, NOW, TOKEN_URL, advancing_sleep, mock_site

LINKED_CHAT = 111
LATER_CHAT = 222


class Harness:
    def __init__(self, db, clock, channel, reporter, replanned):
        self.db = db
        self.clock = clock
        self.channel = channel
        self.reporter = reporter
        self.replanned = replanned


def _site(site_id, owner_id, label, **kw):
    base = dict(se_site_id=site_id, owner_id=owner_id, portal_name=f"P{site_id}", label_he=label,
                peak_power_kwp_reported=20.0, tz="Asia/Jerusalem",
                tariff_ils_per_kwh=Decimal("0.45"), income_basis="production",
                status="active", created_at=NOW)
    base.update(kw)
    return Site(**base)


def seed_two_owners(db):
    """Owner 1 is linked (FARM); owner 2 signed up on the web and has no chat (HOME)."""
    with session_scope(db) as s:
        s.add(Owner(id=1, display_name="אורן", telegram_chat_id=LINKED_CHAT, active=True,
                    created_at=NOW))
        s.add(Owner(id=2, display_name="דנה", telegram_chat_id=None, active=True,
                    signup_source="web", created_at=NOW))
        s.add(_site(FARM, 1, "משק", commercial=True))
        s.add(_site(HOME, 2, "בית"))


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(NOW)


@pytest.fixture
async def h(db, settings, clock):
    seed_two_owners(db)
    channel = FakeChannel()
    replanned = []
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
            lambda sid, at: None,
            delay_send=lambda seconds, factory: None,
            replan_owner=replanned.append,
        )
        box["reporter"] = reporter
        yield Harness(db, clock, channel, reporter, replanned)


async def pull_with_fan(h, load_fixture, *site_ids):
    with respx.mock:
        for sid in site_ids:
            mock_site(load_fixture, sid, alerts="alerts_fan.json")
        for sid in site_ids:
            await h.reporter.evening_pull(sid)


def _outbox_keys(db):
    with session_scope(db) as s:
        return sorted(e.dedupe_key for e in s.scalars(select(OutboxEntry)))


def _fault(db, site_id):
    with session_scope(db) as s:
        rec = s.scalars(select(FaultRecord).where(FaultRecord.site_id == site_id)).one()
        s.expunge(rec)
        return rec


# ------------------------------------------------------------ unlinked owner


def test_owner_chat():
    assert owner_chat(Owner(telegram_chat_id=LINKED_CHAT, active=True)) == str(LINKED_CHAT)
    assert owner_chat(Owner(telegram_chat_id=None, active=True)) is None
    assert owner_chat(Owner(telegram_chat_id=LINKED_CHAT, active=False)) is None


async def test_unlinked_owner_skipped_others_sent(h, load_fixture, caplog):
    with caplog.at_level(logging.INFO, logger="solya"):
        await pull_with_fan(h, load_fixture, FARM, HOME)
        await h.reporter.evening_report(1, DAY)
        await h.reporter.evening_report(2, DAY)

    assert {m.recipient for m in h.channel.sent} == {str(LINKED_CHAT)}
    assert sorted(m.dedupe_key for m in h.channel.sent) == [
        f"fault:{FARM}:alert:101", "report:1:2026-11-24",
    ]
    assert _fault(h.db, FARM).notified_at is not None
    assert _fault(h.db, HOME).notified_at is None
    assert _outbox_keys(h.db) == [f"fault:{FARM}:alert:101", "report:1:2026-11-24"]
    assert "skip_unlinked" in caplog.text
    with session_scope(h.db) as s:  # the unlinked owner's data is still stored
        assert s.get(DailyMetric, (HOME, DAY)) is not None


async def test_unlinked_no_stale_reminder(h, load_fixture):
    await pull_with_fan(h, load_fixture, FARM, HOME)
    await h.reporter.evening_report(2, DAY)
    with session_scope(h.db) as s:
        s.get(Owner, 2).telegram_chat_id = LATER_CHAT

    h.clock.advance(timedelta(hours=4))
    await h.reporter.send_reminders()
    assert [m for m in h.channel.sent if m.recipient == str(LATER_CHAT)] == []
    assert _fault(h.db, HOME).reminded_at is None

    await pull_with_fan(h, load_fixture, HOME)
    to_dana = [m for m in h.channel.sent if m.recipient == str(LATER_CHAT)]
    assert [(m.kind, m.dedupe_key) for m in to_dana] == [("fault", f"fault:{HOME}:alert:101")]
    assert _fault(h.db, HOME).notified_at is not None


async def test_unlinked_reauth_tells_admins_only(h, settings):
    settings.admin_chat_ids = [999]
    await h.reporter.on_reauth_needed(HOME)
    assert [m.recipient for m in h.channel.sent] == ["999"]
    with session_scope(h.db) as s:
        assert s.scalars(select(ConnectRequest)).all() == []


async def test_unlinked_site_connected_sends_nothing(h):
    await h.reporter.on_site_connected(HOME, is_first=True)
    assert h.channel.sent == []
    assert _outbox_keys(h.db) == []
    assert h.replanned == [2]  # the site's pulls are still planned


async def test_outbox_refuses_missing_recipient(db, clock):
    from solya.messaging.base import Invalid, OutboundMessage

    channel = FakeChannel()
    result = await Outbox(db, clock).deliver(channel, OutboundMessage("None", "fault", "x", None, "k"))
    assert isinstance(result, Invalid)
    assert channel.sent == []
    assert _outbox_keys(db) == []


# ------------------------------------------------------------ unmonitored site


def _plan_site(site_id, owner_id, **kw):
    base = dict(se_site_id=site_id, owner_id=owner_id, portal_name=f"p{site_id}",
                tz="Asia/Jerusalem", lat=32.33, lon=34.89, status="active", commercial=True,
                check_times=None, monitored=True, created_at=NOW)
    base.update(kw)
    return Site(**base)


async def test_unmonitored_not_planned_but_kept_alive(h, settings):
    sites = [
        _plan_site(1, owner_id=1),
        _plan_site(2, owner_id=1, monitored=False),
        _plan_site(3, owner_id=2, monitored=False),  # owner 3's only site
        _plan_site(4, owner_id=3),                   # owner 3 has no chat
    ]
    jobs = plan_day_jobs(sites, DAY, settings, linked_owners={1, 2})
    assert {(j.kind, j.target) for j in jobs if j.kind != "midday_check"} == {
        ("evening_pull", 1), ("evening_pull", 4), ("evening_report", 1),
    }
    assert {j.target for j in jobs if j.kind == "midday_check"} == {1, 4}

    with session_scope(h.db) as s:
        s.get(Site, HOME).monitored = False
        for sid in (FARM, HOME):
            s.get(SiteToken, sid).refreshed_at = NOW - timedelta(days=7)
    with respx.mock:
        route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(
            200, json={"access_token": "a2", "refresh_token": "r2", "expires_in": 7200}))
        await h.reporter.keepalive()
    assert route.call_count == 2
    with session_scope(h.db) as s:  # the budget's pacing sleep moves the clock on a little
        assert s.get(SiteToken, HOME).refreshed_at >= NOW


async def test_plan_today_skips_unlinked_report(db, settings):
    from solya.scheduler import SchedulerService

    seed_two_owners(db)
    svc = SchedulerService(reporter=None, session_factory=db, settings=settings,
                           clock=FixedClock(datetime(2026, 11, 24, 10, 0, tzinfo=timezone.utc)))
    svc.plan_today()
    ids = {job.id for job in svc.scheduler.get_jobs()}
    assert "evening_report:1:2026-11-24" in ids
    assert "evening_report:2:2026-11-24" not in ids
    assert f"evening_pull:{HOME}:2026-11-24" in ids  # the unlinked owner's data is still pulled


def test_unmonitored_excluded_from_report_and_answers(db, settings):
    with session_scope(db) as s:
        s.add(Owner(id=1, display_name="אורן", telegram_chat_id=LINKED_CHAT, active=True,
                    created_at=NOW))
        s.add(_site(FARM, 1, None, monitored=False))
        s.add(_site(HOME, 1, None))
        for sid, wh in ((FARM, 300000), (HOME, 85000)):
            s.add(DailyMetric(site_id=sid, local_date=DAY, production_wh=wh,
                              income_ils=Decimal("10.00"), comm_state="ok", as_of=NOW,
                              computed_at=NOW))
        s.add(FaultRecord(site_id=FARM, key="alert:9", code="FAN_ALERT_1", serial=None,
                          source="alert", first_seen=NOW))

    with session_scope(db) as s:
        card, text = build_owner_report(s, 1, DAY, NOW, settings, NullRay())
        assert [r.name for r in card.rows] == [f"P{HOME}"]
        assert card.total is None  # a single shown site has no total line
        assert "המערכת" in text  # one monitored site: the single-site label

        owner = s.get(Owner, 1)
        assert answers.answer_today(s, owner, NOW).count("\n") == 0
        assert answers.answer_today(s, owner, NOW).startswith("המערכת: ")
        assert answers.answer_yesterday(s, owner, NOW).count("\n") == 0
        assert answers.answer_month(s, owner, NOW).count("\n") == 0
        assert answers.answer_alerts(s, owner) == answers.rtl_text(answers.NO_ALERTS)

        s.get(Site, HOME).monitored = False
        s.flush()
        assert build_owner_report(s, 1, DAY, NOW, settings, NullRay()) is None
        assert answers.answer_today(s, owner, NOW) == answers.answer_for("help", s, owner, NOW)


async def test_manual_pull_unmonitored_stores_raw_skips_faults(h, load_fixture):
    with session_scope(h.db) as s:
        s.get(Site, FARM).monitored = False
    with respx.mock:
        mock_site(load_fixture, FARM, alerts="alerts_fan.json")
        bundle = await h.reporter.manual_pull(FARM, "evening", notify=True)
    assert bundle is not None
    assert h.channel.sent == []
    with session_scope(h.db) as s:
        assert s.scalars(select(RawResponse).where(RawResponse.site_id == FARM)).first() is not None
        assert s.scalars(select(FaultRecord)).all() == []


# ------------------------------------------------------------ reassignment


def test_reassign_resets_monitored(db, settings):
    info = SiteInfo(7, "Site", 10.0, "Asia/Jerusalem", 32.0, 34.8, NOW, True)
    with db() as s:
        first = Owner(display_name="א", telegram_chat_id=1, created_at=NOW)
        second = Owner(display_name="ב", telegram_chat_id=2, created_at=NOW)
        s.add_all([first, second])
        s.flush()
        site, _ = ob.register_site(s, _req(s, settings, first), info, NOW)
        site.monitored = False
        site.call_plan = "low"
        site.billing_kwp_override = 12.0

        same, _ = ob.register_site(s, _req(s, settings, first), info, NOW)
        assert same.monitored is False  # the same owner reconnecting keeps their choice

        moved, _ = ob.register_site(s, _req(s, settings, second), info, NOW)
        assert moved.owner_id == second.id
        assert moved.monitored is True
        assert moved.call_plan == "low" and moved.billing_kwp_override == 12.0


def _req(s, settings, owner):
    url = ob.create_connect_request(s, owner.id, NOW, settings)
    return s.get(ConnectRequest, url.rsplit("/", 1)[1])


# ------------------------------------- state from while unlinked, sent at link (M6, M7)


def _link_dana(db):
    with session_scope(db) as s:
        s.get(Owner, 2).telegram_chat_id = LATER_CHAT


async def test_link_sends_reauth_link_missed_while_unlinked(h, settings):
    settings.admin_chat_ids = [999]
    with session_scope(h.db) as s:
        s.get(Site, HOME).status = "reauth_needed"
    await h.reporter.on_reauth_needed(HOME)  # unlinked: only the admins hear of it
    _link_dana(h.db)
    await h.reporter.on_owner_linked(2)
    await h.reporter.on_owner_linked(2)  # a retried link sends nothing twice

    to_dana = [m for m in h.channel.sent if m.recipient == str(LATER_CHAT)]
    assert [m.dedupe_key for m in to_dana] == [
        "welcome1:2", "monitored:2", f"reauth:{HOME}:2026-11-24",
    ]
    with session_scope(h.db) as s:
        req = s.scalars(select(ConnectRequest)).one()
        assert req.owner_id == 2
        assert f"/connect/{req.token}" in to_dana[-1].text
    assert [m.recipient for m in h.channel.sent].count("999") == 1  # admins told once


async def test_link_sends_comm_loss_started_while_unlinked(h):
    since = NOW - timedelta(hours=3)
    with session_scope(h.db) as s:
        site = s.get(Site, HOME)
        site.comm_state, site.comm_since = "loss", since
    _link_dana(h.db)
    await h.reporter.on_owner_linked(2)
    await h.reporter.on_owner_linked(2)

    to_dana = [m for m in h.channel.sent if m.recipient == str(LATER_CHAT)]
    assert [m.dedupe_key for m in to_dana] == [
        "welcome1:2", "monitored:2", f"comm:{HOME}:{since.isoformat()}",
    ]
    assert to_dana[-1].kind == "comm_loss"
    assert "אין תקשורת עם המערכת מאז" in to_dana[-1].text and "14:05" in to_dana[-1].text


async def test_link_skips_comm_loss_of_unmonitored_site(h):
    with session_scope(h.db) as s:
        site = s.get(Site, HOME)
        site.comm_state, site.comm_since = "loss", NOW - timedelta(hours=3)
        s.add(_site(HOME + 1, 2, "מחסן", monitored=False, comm_state="loss",
                    comm_since=NOW - timedelta(hours=3)))
    _link_dana(h.db)
    await h.reporter.on_owner_linked(2)
    keys = [m.dedupe_key for m in h.channel.sent]
    assert f"comm:{HOME}:{(NOW - timedelta(hours=3)).isoformat()}" in keys
    assert not any(k.startswith(f"comm:{HOME + 1}:") for k in keys)
