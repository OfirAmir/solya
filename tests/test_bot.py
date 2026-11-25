from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy import inspect, text

from solya.bot import build_bot_application
from solya.models import ConnectRequest, FaultRecord, Owner, Site
from solya.services import onboarding as ob

NOW = datetime(2026, 11, 24, 15, 0, tzinfo=timezone.utc)


def _handlers(settings, clock, db):
    container = SimpleNamespace(settings=settings, session_factory=db, clock=clock)
    app = build_bot_application("123456:test-bot-token", container)
    out = {}
    for h in app.handlers[0]:
        for c in getattr(h, "commands", ["__text__"]):
            out[c] = h.callback
    return out


def _upd(chat=5, text="", args=()):
    msg = SimpleNamespace(text=text, reply_text=AsyncMock())
    upd = SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat),
        effective_user=SimpleNamespace(first_name="דנה"),
        message=msg,
        effective_message=msg,
    )
    return upd, SimpleNamespace(args=list(args)), msg


def _owner(db, chat=5, active=True):
    with db() as s:
        tok = ob.create_invite(s, "admin", NOW)
        o = ob.redeem_invite(s, tok, chat, "דנה", NOW)
        o.active = active
        o.last_inbound_at = None
        s.add(Site(se_site_id=1, owner_id=o.id, portal_name="P", tz="Asia/Jerusalem", created_at=NOW))
        s.commit()
        return o.id


def _snapshot(db):
    with db() as s:
        return {
            t: [tuple(r) for r in s.execute(text(f"select * from {t} order by 1"))]
            for t in inspect(s.get_bind()).get_table_names()
        }


async def test_start_valid_invite(settings, clock, db):
    h = _handlers(settings, clock, db)
    with db() as s:
        tok = ob.create_invite(s, "admin", NOW)
        s.commit()
    upd, ctx, msg = _upd(args=[tok])
    await h["start"](upd, ctx)
    assert "הצטרפת" in msg.reply_text.call_args[0][0]
    with db() as s:
        assert s.query(Owner).filter_by(telegram_chat_id=5).count() == 1


async def test_start_bad_invite(settings, clock, db):
    h = _handlers(settings, clock, db)
    upd, ctx, msg = _upd(args=["bogus"])
    await h["start"](upd, ctx)
    assert "בהזמנה בלבד" in msg.reply_text.call_args[0][0]
    with db() as s:
        assert s.query(Owner).count() == 0


async def test_connect_unregistered(settings, clock, db):
    h = _handlers(settings, clock, db)
    upd, ctx, msg = _upd()
    await h["connect"](upd, ctx)
    assert "בהזמנה בלבד" in msg.reply_text.call_args[0][0]
    with db() as s:
        assert s.query(ConnectRequest).count() == 0


async def test_connect_registered(settings, clock, db):
    _owner(db)
    h = _handlers(settings, clock, db)
    upd, ctx, msg = _upd()
    await h["connect"](upd, ctx)
    assert "https://solya.example/connect/" in msg.reply_text.call_args[0][0]
    with db() as s:
        assert s.query(ConnectRequest).count() == 1


async def test_text_acks_fault_and_routes(settings, clock, db):
    oid = _owner(db, active=False)
    with db() as s:
        s.add(FaultRecord(site_id=1, key="k", source="x", code="C", first_seen=NOW, notified_at=NOW))
        s.commit()
    h = _handlers(settings, clock, db)
    upd, ctx, msg = _upd(text="יש תקלה?")
    await h["__text__"](upd, ctx)
    assert "C" in msg.reply_text.call_args[0][0]
    with db() as s:
        assert s.query(FaultRecord).one().acked_at == clock.now()
        o = s.get(Owner, oid)
        assert o.last_inbound_at == clock.now() and o.active is True


async def test_text_only_allowed_changes(settings, clock, db):
    _owner(db)
    with db() as s:
        s.add(FaultRecord(site_id=1, key="k", source="x", code="C", first_seen=NOW, notified_at=NOW))
        s.commit()
    before = _snapshot(db)
    h = _handlers(settings, clock, db)
    upd, ctx, msg = _upd(text="/disconnect cancel everything")
    await h["__text__"](upd, ctx)
    after = _snapshot(db)
    assert msg.reply_text.await_count == 1
    for table in before:
        if table in ("faults", "owners"):
            continue
        assert after[table] == before[table], table
    f0, f1 = before["faults"][0], after["faults"][0]
    diff = [i for i, (a, b) in enumerate(zip(f0, f1)) if a != b]
    assert len(diff) == 1 and f1[diff[0]] is not None  # acked_at only
    o0, o1 = before["owners"][0], after["owners"][0]
    assert sum(a != b for a, b in zip(o0, o1)) == 1  # last_inbound_at only


def _notified_fault(db):
    with db() as s:
        s.add(FaultRecord(site_id=1, key="k", source="x", code="C", first_seen=NOW, notified_at=NOW))
        s.commit()


async def test_start_from_unblocked_owner_reactivates(settings, clock, db):
    """I9/R27: Telegram's Restart button after unblocking sends /start."""
    oid = _owner(db, active=False)
    h = _handlers(settings, clock, db)
    upd, ctx, msg = _upd(text="/start")
    await h["start"](upd, ctx)
    assert "הצטרפת" in msg.reply_text.call_args[0][0]
    with db() as s:
        o = s.get(Owner, oid)
        assert o.active is True and o.last_inbound_at == clock.now()


async def test_connect_from_inactive_owner_reactivates_and_links(settings, clock, db):
    oid = _owner(db, active=False)
    h = _handlers(settings, clock, db)
    upd, ctx, msg = _upd(text="/connect")
    await h["connect"](upd, ctx)
    assert "https://solya.example/connect/" in msg.reply_text.call_args[0][0]
    with db() as s:
        assert s.get(Owner, oid).active is True
        assert s.query(ConnectRequest).count() == 1


async def test_commands_ack_faults(settings, clock, db):
    """Spec §5.5: any owner message after an alert acknowledges it, commands included."""
    for command in ("alerts", "today", "help"):
        with db() as s:
            s.query(FaultRecord).delete()
            s.query(Site).delete()
            s.query(Owner).delete()
            s.commit()
        oid = _owner(db, active=False)
        _notified_fault(db)
        h = _handlers(settings, clock, db)
        upd, ctx, msg = _upd(text=f"/{command}")
        await h[command](upd, ctx)
        assert msg.reply_text.await_count == 1
        with db() as s:
            assert s.query(FaultRecord).one().acked_at == clock.now(), command
            o = s.get(Owner, oid)
            assert o.active is True and o.last_inbound_at == clock.now(), command


# --- web sign-up linking ----------------------------------------------------


def _web_owner(db, token="linktok", monitored_sites=1):
    with db() as s:
        o = Owner(display_name="Web", signup_source="web", link_token=token, active=True,
                  created_at=NOW)
        s.add(o)
        s.flush()
        for i in range(monitored_sites):
            s.add(Site(se_site_id=900 + i, owner_id=o.id, portal_name="P", tz="Asia/Jerusalem",
                       created_at=NOW))
        s.commit()
        return o.id


def _linking(settings, clock, db, fake_channel):
    from conftest import FakeChannel  # noqa: F401
    from solya.messaging.outbox import Outbox
    from solya.rules.ray import NullRay
    from solya.services.reporting import Reporter

    delayed = []
    reporter = Reporter(
        db, None, None, Outbox(db, clock), fake_channel, clock, settings, NullRay(),
        lambda sid, at: None,
        delay_send=lambda seconds, factory: delayed.append((seconds, factory)),
    )
    scheduler = MagicMock()
    container = SimpleNamespace(settings=settings, session_factory=db, clock=clock,
                                reporter=reporter, scheduler=scheduler)
    app = build_bot_application("123456:test-bot-token", container)
    h = {c: x.callback for x in app.handlers[0] for c in getattr(x, "commands", [])}
    return h, scheduler, delayed


async def test_start_link_token_links_and_welcomes(settings, clock, db, fake_channel):
    from solya.text import he
    from solya.text.rtl import rtl_text

    oid = _web_owner(db, monitored_sites=2)
    h, scheduler, delayed = _linking(settings, clock, db, fake_channel)
    upd, ctx, msg = _upd(chat=77, args=["linktok"])
    await h["start"](upd, ctx)
    with db() as s:
        o = s.get(Owner, oid)
        assert o.telegram_chat_id == 77 and o.link_token is None
        assert o.last_inbound_at is not None
    texts = [m.text for m in fake_channel.sent]
    assert rtl_text(he.WELCOME_1) in texts
    assert rtl_text(he.sites_monitored(2)) in texts
    assert [m.dedupe_key for m in fake_channel.sent] == [f"welcome1:{oid}", f"monitored:{oid}"]
    assert len(delayed) == 1 and delayed[0][0] == 60
    await delayed[0][1]()
    assert fake_channel.sent[-1].dedupe_key == f"welcome2:{oid}"
    scheduler.plan_owner.assert_called_once_with(oid)


async def test_start_link_token_single_use(settings, clock, db, fake_channel):
    oid = _web_owner(db)
    h, scheduler, _ = _linking(settings, clock, db, fake_channel)
    upd, ctx, _m = _upd(chat=77, args=["linktok"])
    await h["start"](upd, ctx)
    sent = len(fake_channel.sent)
    upd2, ctx2, msg2 = _upd(chat=88, args=["linktok"])
    await h["start"](upd2, ctx2)
    assert "בהזמנה בלבד" in msg2.reply_text.call_args[0][0]
    with db() as s:
        assert s.get(Owner, oid).telegram_chat_id == 77
    assert len(fake_channel.sent) == sent
    scheduler.plan_owner.assert_called_once()


async def test_start_bound_elsewhere_changes_nothing(settings, clock, db, fake_channel):
    other = _owner(db, chat=5)
    oid = _web_owner(db)
    h, scheduler, delayed = _linking(settings, clock, db, fake_channel)
    upd, ctx, msg = _upd(chat=5, args=["linktok"])
    await h["start"](upd, ctx)
    assert "כבר מחובר לחשבון אחר" in msg.reply_text.call_args[0][0]
    with db() as s:
        o = s.get(Owner, oid)
        assert o.telegram_chat_id is None and o.link_token == "linktok"
        assert s.get(Owner, other).telegram_chat_id == 5
    assert fake_channel.sent == [] and delayed == []
    scheduler.plan_owner.assert_not_called()


async def test_start_invite_still_works(settings, clock, db, fake_channel):
    h, scheduler, _ = _linking(settings, clock, db, fake_channel)
    with db() as s:
        tok = ob.create_invite(s, "admin", NOW)
        s.commit()
    upd, ctx, msg = _upd(chat=9, args=[tok])
    await h["start"](upd, ctx)
    assert "הצטרפת" in msg.reply_text.call_args[0][0]
    with db() as s:
        assert s.query(Owner).filter_by(telegram_chat_id=9).count() == 1
    scheduler.plan_owner.assert_not_called()
    assert fake_channel.sent == []
