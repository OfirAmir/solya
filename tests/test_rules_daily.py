from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from solya.ingest import PullBundle
from solya.models import DailyMetric, Site
from solya.rules.daily import compute_site_day, effective_label, income, persist_site_day
from solya.rules.ray import NullRay
from solya.solaredge.models import Overview, SiteInfo

NOW = datetime(2026, 11, 24, 15, 0, tzinfo=timezone.utc)


def make_site(**kw) -> Site:
    base = dict(
        se_site_id=1, owner_id=1, portal_name="Portal", tz="Asia/Jerusalem", status="active",
        tariff_ils_per_kwh=Decimal("0.45"), income_basis="production",
        peak_power_kwp_reported=90.72, last_update_time=NOW - timedelta(minutes=5),
    )
    base.update(kw)
    return Site(**base)


def make_bundle(overview, last_update=NOW - timedelta(minutes=5)) -> PullBundle:
    info = SiteInfo(1, "n", 90.72, "Asia/Jerusalem", None, None, last_update, True)
    return PullBundle(info, overview, [], {}, None, {}, NOW)


def day_for(site, bundle, settings):
    return compute_site_day(site, 1, bundle, [], NOW, settings, NullRay())


def test_income_regression_20261124():
    a = income(316990, 316990, Decimal("0.45"), "production")
    b = income(85039, 79225, Decimal("0.48"), "export")
    assert a == Decimal("142.65")
    assert b == Decimal("38.03")
    assert a + b == Decimal("180.68")


def test_income_none_tariff():
    assert income(1000, 1000, None, "production") is None
    assert income(None, 1000, Decimal("0.5"), "production") is None
    assert income(1000, None, Decimal("0.5"), "export") is None


def test_state_comm_loss_has_no_figures(settings):
    stale = NOW - timedelta(hours=2)
    site = make_site(last_update_time=stale)
    d = day_for(site, make_bundle(Overview(1, 1000.0, 500.0, 500.0, None), stale), settings)
    assert d.state == "comm_loss"
    assert d.comm_since == stale
    assert d.production_wh is None and d.income_ils is None and d.ray is None


def test_missing_overview_line(settings):
    d = day_for(make_site(), make_bundle(None), settings)
    assert d.state == "no_data"
    assert d.production_wh is None and d.income_ils is None


def test_null_production_not_zero(settings):
    d = day_for(make_site(), make_bundle(Overview(1, None, None, None, None)), settings)
    assert d.state == "no_data"
    assert d.production_wh is None


def test_reauth_state(settings):
    d = day_for(make_site(status="reauth_needed"), make_bundle(Overview(1, 1.0, 1.0, 0.0, None)), settings)
    assert d.state == "reauth_needed"
    assert d.production_wh is None


def test_ok_fills_figures_and_kwp(settings):
    d = day_for(make_site(billing_kwp_override=100.0), make_bundle(Overview(1, 316990.0, 316990.0, 0.0, None)), settings)
    assert d.state == "ok"
    assert d.income_ils == Decimal("142.65")
    assert d.kwp == 100.0


def test_effective_label_defaults():
    assert effective_label(make_site(), 1) == "המערכת"
    assert effective_label(make_site(), 2) == "Portal"
    assert effective_label(make_site(label_he="חווה"), 2) == "חווה"


def test_persist_upsert_and_non_ok_skipped(db, settings):
    day = day_for(make_site(), make_bundle(Overview(1, 1000.0, 500.0, 500.0, None)), settings)
    d = date(2026, 11, 24)
    with db() as s:
        persist_site_day(s, day, d, 5000.0, NOW)
        persist_site_day(s, day, d, 6000.0, NOW)
        s.commit()
        assert s.get(DailyMetric, (1, d)).lifetime_wh == 6000.0
        bad = day_for(make_site(status="reauth_needed"), None, settings)
        persist_site_day(s, bad, d, None, NOW)
        assert s.get(DailyMetric, (1, d)).production_wh == 1000.0
        persist_site_day(s, bad, date(2026, 11, 23), None, NOW)
        row = s.get(DailyMetric, (1, date(2026, 11, 23)))
        assert row.production_wh is None and row.comm_state == "reauth_needed"
