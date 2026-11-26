"""Owner-editable site settings: names, income basis, tariff, monitoring."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from solya.models import DailyMetric, Site
from solya.rules.daily import income

NAME_ERROR = "שם המערכת צריך להיות באורך 1 עד 30 תווים"
TARIFF_ERROR = "התעריף צריך להיות מספר בין 0.05 ל-3.00"

MAX_NAME_LEN = 30
_TARIFF_MIN, _TARIFF_MAX = Decimal("0.05"), Decimal("3.00")


@dataclass(frozen=True)
class SiteEdit:
    site_id: int
    name: str
    basis: str
    tariff: Decimal | None
    monitored: bool


def short_name(portal_name: str) -> str:
    return portal_name.split(" - ", 1)[0].strip()[:MAX_NAME_LEN]


def default_basis(commercial: bool) -> str:
    return "production" if commercial else "export"


def parse_tariff(raw: str) -> Decimal | None:
    text = raw.replace("₪", "").replace(" ", "").replace(",", ".").strip()
    if not text:
        return None
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise ValueError("tariff") from None
    if not value.is_finite() or not _TARIFF_MIN <= value <= _TARIFF_MAX:
        raise ValueError("tariff")
    if value.as_tuple().exponent < -4:
        raise ValueError("tariff")
    return value


def validate_name(raw: str) -> str:
    name = raw.strip()
    if not name or len(name) > MAX_NAME_LEN or any(ord(c) < 32 or 127 <= ord(c) < 160 for c in name):
        raise ValueError("name")
    return name


def apply_edits(
    session: Session,
    owner_id: int,
    edits: list[SiteEdit],
    today_by_site: Callable[[Site], date],
) -> list[int]:
    """Apply an owner's edits to their own sites (others are ignored). Returns the
    ids whose monitored flag changed."""
    changed: list[int] = []
    for edit in edits:
        site = session.get(Site, edit.site_id)
        if site is None or site.owner_id != owner_id:
            continue
        money_changed = (
            site.tariff_ils_per_kwh != edit.tariff or site.income_basis != edit.basis
        )
        site.label_he = edit.name
        site.card_name = edit.name
        site.income_basis = edit.basis
        site.tariff_ils_per_kwh = edit.tariff
        if site.monitored != edit.monitored:
            site.monitored = edit.monitored
            changed.append(site.se_site_id)
        if money_changed:
            _recompute_month(session, site, today_by_site(site))
    session.flush()
    return changed


def _recompute_month(session: Session, site: Site, today: date) -> None:
    rows = session.scalars(
        select(DailyMetric).where(
            DailyMetric.site_id == site.se_site_id,
            DailyMetric.local_date >= today.replace(day=1),
            DailyMetric.local_date <= today,
        )
    )
    for row in rows:
        row.income_ils = income(
            row.production_wh, row.export_wh, site.tariff_ils_per_kwh, site.income_basis
        )


def apply_name_defaults(session: Session) -> int:
    count = 0
    for site in session.scalars(select(Site).where((Site.card_name.is_(None)) | (Site.card_name == ""))):
        site.card_name = short_name(site.portal_name)
        count += 1
    session.flush()
    return count
