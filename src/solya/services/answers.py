"""Store-only Q&A: every answer is read from the database, never from SolarEdge."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from solya.models import DailyMetric, Owner, Site
from solya.rules.daily import effective_label
from solya.rules.faults import open_fault_views
from solya.text.he import HELP, NO_DATA_YET, fmt_ils, fmt_kwh
from solya.text.rtl import rtl_text

NO_ALERTS = "אין התראות פעילות."
DEFAULT_TZ = "Asia/Jerusalem"

Route = Literal["connect", "settings", "today", "yesterday", "month", "alerts", "help"]

_MARKS = re.compile("[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")


def strip_marks(text: str) -> str:
    """Drop invisible direction marks (copied from Hebrew messages), then trim."""
    return _MARKS.sub("", text).strip()


_ROUTES: list[tuple[re.Pattern[str], Route]] = [
    (re.compile("לחבר|תחבר|חיבור|מערכת נוספת|עוד מערכת|מערכת שני(?:ה|יה)"), "connect"),
    (re.compile("הגדרות|תעריף"), "settings"),
    (re.compile("אתמול"), "yesterday"),
    (re.compile("התראות|התראה|תקלה"), "alerts"),
    (re.compile("החודש|חודש"), "month"),
    (re.compile("היום"), "today"),
]


def route_text(text: str) -> Route:
    for pattern, route in _ROUTES:
        if pattern.search(text):
            return route
    return "help"


def _sites(session: Session, owner: Owner) -> list[Site]:
    """The owner's monitored sites: an unmonitored one never appears in an answer."""
    return list(
        session.scalars(
            select(Site)
            .where(Site.owner_id == owner.id, Site.monitored.is_(True))
            .order_by(Site.se_site_id)
        ).all()
    )


def _local(site: Site, now: datetime) -> datetime:
    return now.astimezone(ZoneInfo(site.tz or DEFAULT_TZ))


def _figures(wh: float, income: Decimal | None) -> str:
    return fmt_kwh(wh) if income is None else f"{fmt_kwh(wh)}, {fmt_ils(income)}"


def _no_sites() -> str:
    return rtl_text(HELP)


def _day_lines(session: Session, owner: Owner, now: datetime, offset_days: int, with_time: bool) -> str:
    sites = _sites(session, owner)
    if not sites:
        return _no_sites()
    lines = []
    for site in sites:
        label = effective_label(site, len(sites))
        local = _local(site, now)
        day = local.date() - timedelta(days=offset_days)
        row = session.get(DailyMetric, (site.se_site_id, day))
        if row is None or row.production_wh is None:
            lines.append(f"{label}: {NO_DATA_YET}")
            continue
        text = f"{label}: {_figures(row.production_wh, row.income_ils)}"
        if with_time:
            as_of = row.as_of.astimezone(ZoneInfo(site.tz or DEFAULT_TZ))
            text += f" (נכון לשעה {as_of:%H:%M})"
        lines.append(text)
    return rtl_text("\n".join(lines))


def answer_today(session: Session, owner: Owner, now: datetime) -> str:
    return _day_lines(session, owner, now, 0, True)


def answer_yesterday(session: Session, owner: Owner, now: datetime) -> str:
    return _day_lines(session, owner, now, 1, False)


def answer_month(session: Session, owner: Owner, now: datetime) -> str:
    sites = _sites(session, owner)
    if not sites:
        return _no_sites()
    lines = []
    for site in sites:
        label = effective_label(site, len(sites))
        today = _local(site, now).date()
        first = date(today.year, today.month, 1)
        rows = session.scalars(
            select(DailyMetric).where(
                DailyMetric.site_id == site.se_site_id,
                DailyMetric.local_date >= first,
                DailyMetric.local_date <= today,
            )
        ).all()
        rows = [r for r in rows if r.production_wh is not None]
        n = len(rows)
        if n == 0:
            lines.append(f"{label}: {NO_DATA_YET}")
            continue
        wh = sum(r.production_wh for r in rows)
        incomes = [r.income_ils for r in rows if r.income_ils is not None]
        income = sum(incomes, Decimal("0")) if incomes else None
        text = f"{label}: {_figures(wh, income)} ב־{n} ימים"
        missing = today.day - n
        if missing > 0:
            text += f" (חסרים נתונים ל־{missing} ימים)"
        lines.append(text)
    return rtl_text("\n".join(lines))


def answer_alerts(session: Session, owner: Owner) -> str:
    sites = _sites(session, owner)
    lines = []
    for site in sites:
        label = effective_label(site, len(sites))
        for f in open_fault_views(session, site.se_site_id):
            serial = f" בממיר {f.serial}" if f.serial else ""
            lines.append(f"{label}: {f.code}{serial}")
    return rtl_text("\n".join(lines) if lines else NO_ALERTS)


def answer_for(route: Route, session: Session, owner: Owner, now: datetime) -> str:
    if route == "today":
        return answer_today(session, owner, now)
    if route == "yesterday":
        return answer_yesterday(session, owner, now)
    if route == "month":
        return answer_month(session, owner, now)
    if route == "alerts":
        return answer_alerts(session, owner)
    return rtl_text(HELP)
