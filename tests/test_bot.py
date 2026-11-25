from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

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
