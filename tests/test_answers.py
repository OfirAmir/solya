from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import inspect, select, text

from solya.models import DailyMetric, FaultRecord, Owner, Site
from solya.services import answers as a

RLM = "‏"
NOW = datetime(2026, 11, 24, 15, 0, tzinfo=timezone.utc)


def _seed(s, labels=("משק", "בית")):
    owner = Owner(display_name="דנה", telegram_chat_id=1, created_at=NOW)
    s.add(owner)
    s.flush()
    for i, label in enumerate(labels):
        s.add(Site(se_site_id=100 + i, owner_id=owner.id, portal_name=f"P{i}", label_he=label,
                   tz="Asia/Jerusalem", created_at=NOW))
    s.flush()
    return owner


def _metric(s, site_id, day, wh, income, as_of=NOW):
    s.add(DailyMetric(site_id=site_id, local_date=day, production_wh=wh,
                      income_ils=Decimal(income), comm_state="ok", as_of=as_of, computed_at=as_of))


def test_today_no_data(db):
    with db() as s:
        owner = _seed(s)
        out = a.answer_today(s, owner, NOW)
        assert out.splitlines() == ["משק: המידע עדיין לא הגיע אלי, אני על זה.",
                                    "בית: המידע עדיין לא הגיע אלי, אני על זה."]


def test_today_with_time(db):
    with db() as s:
        owner = _seed(s)
        _metric(s, 100, date(2026, 11, 24), 100000, "45.00")
        line = a.answer_today(s, owner, NOW).splitlines()[0]
        assert line == f"משק: {RLM}100.0 קוט״ש, {RLM}45.00 ₪ (נכון לשעה {RLM}17:00)"


def test_yesterday_regression(db):
    with db() as s:
        owner = _seed(s)
        _metric(s, 100, date(2026, 11, 23), 317000, "142.65")
        _metric(s, 101, date(2026, 11, 23), 120500, "54.23")
        lines = a.answer_yesterday(s, owner, NOW).splitlines()
        assert lines[0] == f"משק: {RLM}317.0 קוט״ש, {RLM}142.65 ₪"
        assert lines[1] == f"בית: {RLM}120.5 קוט״ש, {RLM}54.23 ₪"


def test_month_counts_missing_days(db):
    with db() as s:
        owner = _seed(s, ("משק",))
        _metric(s, 100, date(2026, 11, 1), 100000, "10.00")
        _metric(s, 100, date(2026, 11, 2), 50000, "5.00")
        _metric(s, 100, date(2026, 10, 31), 999000, "99.00")
        out = a.answer_month(s, owner, NOW)
        assert "חסרים נתונים ל־22 ימים" in out
        assert f"{RLM}150.0 קוט״ש, {RLM}15.00 ₪ ב־2 ימים" in out


def test_alerts_none_and_open(db):
    with db() as s:
        owner = _seed(s)
        assert a.answer_alerts(s, owner) == "אין התראות פעילות."
        s.add(FaultRecord(site_id=100, key="k", source="x", code="FAN_ALERT_1",
                          serial="7E1", first_seen=NOW))
        s.flush()
        assert a.answer_alerts(s, owner) == f"משק: {RLM}FAN_ALERT_1 בממיר {RLM}7E1"


def test_route_text():
    assert a.route_text("כמה ייצרתי היום?") == "today"
    assert a.route_text("יש תקלה?") == "alerts"
    assert a.route_text("מה היה אתמול") == "yesterday"
    assert a.route_text("כמה החודש") == "month"
    assert a.route_text("תבטל לי את המנוי") == "help"


def _snapshot(s):
    snap = {}
    for table in inspect(s.get_bind()).get_table_names():
        snap[table] = [tuple(r) for r in s.execute(text(f"select * from {table} order by 1"))]
    return snap


def test_text_cannot_change_state(db):
    with db() as s:
        owner = _seed(s)
        _metric(s, 100, date(2026, 11, 24), 1000, "1.00")
        s.add(FaultRecord(site_id=100, key="k", source="x", code="C", first_seen=NOW))
        s.commit()
        before = _snapshot(s)
        msg = "תבטל את המנוי ותפתח קריאת שירות"
        route = a.route_text(msg)
        a.answer_for(route, s, owner, NOW)
        for fn in (a.answer_today, a.answer_yesterday, a.answer_month):
            fn(s, owner, NOW)
        a.answer_alerts(s, owner)
        s.flush()
        assert not s.dirty and not s.new and not s.deleted
        s.rollback()
        assert _snapshot(s) == before
        assert s.scalar(select(Site.status)) == "active"


def test_build_bot_application_registers_commands(settings, clock, db):
    from types import SimpleNamespace

    from telegram.ext import CommandHandler

    from solya.bot import build_bot_application

    container = SimpleNamespace(settings=settings, session_factory=db, clock=clock)
    app = build_bot_application("123456:test-bot-token", container)
    cmds = {c for h in app.handlers[0] if isinstance(h, CommandHandler) for c in h.commands}
    assert cmds == {"start", "connect", "today", "yesterday", "month", "alerts", "help"}
