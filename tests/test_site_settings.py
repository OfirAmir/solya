from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from solya.models import ConnectRequest, DailyMetric, Owner, Site
from solya.rules.daily import effective_label
from solya.services import onboarding as ob
from solya.services import site_settings as ss
from solya.solaredge.models import SiteInfo

NOW = datetime(2026, 11, 24, 15, 0, tzinfo=timezone.utc)
TODAY = date(2026, 11, 24)


def test_short_name():
    assert ss.short_name("IL 360 - אורן סופר") == "IL 360"
    assert ss.short_name("אורן סופר") == "אורן סופר"
    assert ss.short_name("x" * 40) == "x" * 30


@pytest.mark.parametrize("raw", ["0,48", " 0.48 ", "₪0.48", "0.48 ₪"])
def test_parse_tariff_accepts_bill_formats(raw):
    assert ss.parse_tariff(raw) == Decimal("0.48")
    assert ss.parse_tariff("") is None


@pytest.mark.parametrize("raw", ["0.04", "3.01", "0.12345", "abc"])
def test_parse_tariff_rejects(raw):
    with pytest.raises(ValueError, match="tariff"):
        ss.parse_tariff(raw)


@pytest.mark.parametrize("raw", ["   ", "x" * 31, "a\x07b"])
def test_validate_name_rejects(raw):
    with pytest.raises(ValueError, match="name"):
        ss.validate_name(raw)
    assert ss.validate_name("  ok  ") == "ok"


def _owner(s, chat):
    o = Owner(display_name="d", telegram_chat_id=chat, active=True, created_at=NOW)
    s.add(o)
    s.flush()
    return o


def _site(s, owner, sid=1, **kw):
    base = dict(se_site_id=sid, owner_id=owner.id, portal_name="P", tz="Asia/Jerusalem",
                income_basis="production", created_at=NOW)
    base.update(kw)
    site = Site(**base)
    s.add(site)
    s.flush()
    return site


def _row(s, sid, d, prod, exp=None):
    s.add(DailyMetric(site_id=sid, local_date=d, production_wh=prod, export_wh=exp,
                      comm_state="ok", as_of=NOW, computed_at=NOW))


def _edit(sid, **kw):
    base = dict(site_id=sid, name="שם", basis="production", tariff=Decimal("0.45"), monitored=True)
    base.update(kw)
    return ss.SiteEdit(**base)


def test_apply_edits_recomputes_month_income(db):
    with db() as s:
        o = _owner(s, 1)
        _site(s, o)
        for d in (date(2026, 10, 31), date(2026, 11, 1), date(2026, 11, 24)):
            _row(s, 1, d, 10005.0)
        s.flush()
        ss.apply_edits(s, o.id, [_edit(1)], lambda site: TODAY)
        s.flush()
        inc = {r.local_date: r.income_ils for r in s.query(DailyMetric)}
        assert inc[date(2026, 11, 1)] == Decimal("4.50")  # 10.005 * 0.45 = 4.50225
        assert inc[date(2026, 11, 24)] == Decimal("4.50")
        assert inc[date(2026, 10, 31)] is None
        site = s.get(Site, 1)
        assert site.label_he == "שם" and site.card_name == "שם"
        assert site.tariff_ils_per_kwh == Decimal("0.45")


def test_apply_edits_export_basis_uses_export(db):
    with db() as s:
        o = _owner(s, 1)
        _site(s, o)
        _row(s, 1, date(2026, 11, 2), 9000.0, 4000.0)
        _row(s, 1, date(2026, 11, 3), 9000.0, None)
        s.flush()
        ss.apply_edits(s, o.id, [_edit(1, basis="export")], lambda site: TODAY)
        s.flush()
        inc = {r.local_date: r.income_ils for r in s.query(DailyMetric)}
        assert inc[date(2026, 11, 2)] == Decimal("1.80")
        assert inc[date(2026, 11, 3)] is None


def test_apply_edits_ignores_other_owner(db):
    with db() as s:
        a, b = _owner(s, 1), _owner(s, 2)
        _site(s, b, label_he="orig", monitored=True)
        ss.apply_edits(s, a.id, [_edit(1, name="hacked", monitored=False)], lambda site: TODAY)
        site = s.get(Site, 1)
        assert site.label_he == "orig" and site.monitored is True


def test_apply_edits_returns_monitoring_changes(db):
    with db() as s:
        o = _owner(s, 1)
        _site(s, o, 1, monitored=True)
        _site(s, o, 2, monitored=True)
        changed = ss.apply_edits(
            s, o.id, [_edit(1, monitored=False), _edit(2, monitored=True)], lambda site: TODAY
        )
        assert changed == [1]


def _info(site_id, kwp, name="IL 360 - אורן סופר"):
    return SiteInfo(site_id, name, kwp, "Asia/Jerusalem", 32.0, 34.8, NOW, True)


def _req(s, owner):
    n = s.query(ConnectRequest).count()
    r = ConnectRequest(token=f"t{n}", owner_id=owner.id, nonce=f"n{n}", created_at=NOW)
    s.add(r)
    s.flush()
    return r


def test_register_site_sets_short_card_name_and_basis(db):
    with db() as s:
        o = _owner(s, 1)
        small, _ = ob.register_site(s, _req(s, o), _info(10, 26.55), NOW)
        assert small.card_name == "IL 360" and small.income_basis == "export"
        big, _ = ob.register_site(s, _req(s, o), _info(11, 65.52), NOW)
        assert big.income_basis == "production"


def test_register_site_keeps_existing_card_name_and_basis(db):
    with db() as s:
        o = _owner(s, 1)
        site, _ = ob.register_site(s, _req(s, o), _info(10, 26.55), NOW)
        site.card_name, site.income_basis = "שלי", "production"
        s.flush()
        again, _ = ob.register_site(s, _req(s, o), _info(10, 26.55, "Other - x"), NOW)
        assert again.card_name == "שלי" and again.income_basis == "production"


def test_apply_name_defaults_only_empty(db):
    with db() as s:
        o = _owner(s, 1)
        _site(s, o, 1, portal_name="A - b", card_name=None)
        _site(s, o, 2, portal_name="C - d", card_name="")
        _site(s, o, 3, portal_name="E - f", card_name="keep")
        assert ss.apply_name_defaults(s) == 2
        assert [s.get(Site, i).card_name for i in (1, 2, 3)] == ["A", "C", "keep"]


def test_effective_label_falls_back_to_card_name():
    site = Site(se_site_id=1, owner_id=1, portal_name="Portal", card_name="IL 360")
    assert effective_label(site, 2) == "IL 360"
    assert effective_label(site, 1) == "המערכת"
