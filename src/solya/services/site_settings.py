"""Owner-editable site settings: names, income basis, tariff, monitoring."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from solya.models import DailyMetric, Site
from solya.rules.daily import income

NAME_ERROR = "שם המערכת צריך להיות באורך 1 עד 30 תווים"
TARIFF_ERROR = "התעריף צריך להיות מספר בין 0.05 ל-3.00"
BASIS_ERROR = "יש לבחור איך מקבלים תשלום"
BASES = ("production", "export")

MAX_NAME_LEN = 30
_TARIFF_MIN, _TARIFF_MAX = Decimal("0.05"), Decimal("3.00")


@dataclass(frozen=True)
class SiteEdit:
    site_id: int
    name: str | None  # None keeps the stored names
    basis: str
    tariff: Decimal | None
    monitored: bool


def short_name(portal_name: str) -> str:
    return portal_name.split(" - ", 1)[0].strip()[:MAX_NAME_LEN]


def display_name(site: Site) -> str:
    """The name shown to the owner: their own label, else the card name, else the
    shortened portal name."""
    return site.label_he or site.card_name or short_name(site.portal_name)


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


PRESENT_PREFIX = "present_"


def monitored_choices(form: Mapping, sites: list[Site]) -> dict[int, bool]:
    """The monitoring each of ``sites`` gets from a submitted form. The page renders a
    hidden ``present_{id}`` per site: a site whose marker is missing was not on the page
    the owner saw (e.g. connected in another tab meanwhile), so it keeps its stored flag
    rather than being unmonitored by an unticked box it never showed. A form with no
    markers at all (a page rendered before the markers existed) reads every box."""
    ticked = {str(v) for v in form.getlist("site")}
    shown = {str(k)[len(PRESENT_PREFIX):] for k in form.keys()
             if str(k).startswith(PRESENT_PREFIX)}
    return {
        site.se_site_id: (str(site.se_site_id) in ticked
                          if not shown or str(site.se_site_id) in shown else site.monitored)
        for site in sites
    }


def parse_site_form(
    form: Mapping, sites: list[Site]
) -> tuple[list[SiteEdit], dict[int, dict[str, str]]]:
    """Read ``name_{id}``, ``basis_{id}``, ``tariff_{id}`` and the ``site`` checkboxes
    for ``sites`` only (the caller passes this owner's sites; any other id in the form is
    ignored). A field missing from the form keeps the stored value, and a site not on the
    submitted page keeps its monitoring (``monitored_choices``). Returns the edits of
    the sites without errors, and the errors as {site id: {field: message}}."""
    monitored = monitored_choices(form, sites)
    edits: list[SiteEdit] = []
    errors: dict[int, dict[str, str]] = {}
    for site in sites:
        sid = site.se_site_id
        errs: dict[str, str] = {}
        raw_name = form.get(f"name_{sid}")
        name = None
        if raw_name is not None:
            try:
                name = validate_name(str(raw_name))
            except ValueError:
                errs["name"] = NAME_ERROR
            # R3: the unchanged pre-filled default is not an owner's name: keep the stored
            # names, so a one-system owner keeps reading "המערכת"
            if name is not None and site.label_he is None and name == display_name(site):
                name = None
        raw_basis = form.get(f"basis_{sid}")
        basis = site.income_basis if raw_basis is None else str(raw_basis)
        if basis not in BASES:
            errs["basis"] = BASIS_ERROR
        raw_tariff = form.get(f"tariff_{sid}")
        tariff = site.tariff_ils_per_kwh
        if raw_tariff is not None:
            try:  # any Unicode whitespace, NBSP included, as pasted from a bill
                tariff = parse_tariff("".join(str(raw_tariff).split()))
            except ValueError:
                errs["tariff"] = TARIFF_ERROR
        if errs:
            errors[sid] = errs
        else:
            edits.append(SiteEdit(sid, name, basis, tariff, monitored[sid]))
    return edits, errors


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
        if edit.name is not None:
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
