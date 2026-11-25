from solya.models import Owner, Payment, Site
from solya.pricing import (
    ESTIMATE_LABEL,
    UNKNOWN_PRICE,
    SitePrice,
    describe_price,
    price_site,
    price_total,
)
from solya.services.payments import create_stub_checkout


def _site(override=None, reported=None, sid=1):
    return Site(se_site_id=sid, billing_kwp_override=override, peak_power_kwp_reported=reported)


def test_price_under_50kw():
    p = price_site(_site(override=26.55, reported=99.0))
    assert (p.agorot, p.is_estimate) == (4997, False)


def test_price_above_50kw():
    assert price_site(_site(override=90.72)).agorot == 4997 + 4072 == 9069


def test_half_up_rounding():
    assert price_site(_site(override=50.005)).agorot == 4997 + 1


def test_estimate_from_portal():
    p = price_site(_site(reported=65.52))
    assert (p.agorot, p.is_estimate, p.kw) == (6549, True, 65.52)


def test_unknown_price():
    p = price_site(_site())
    assert (p.agorot, p.is_estimate, p.kw) == (None, False, None)


def test_total_none_if_any_unknown():
    prices = [SitePrice(1, 4997, False, 10), SitePrice(2, None, False, None)]
    assert price_total(prices) == (None, False)
    prices = [SitePrice(1, 4997, False, 10), SitePrice(2, 6549, True, 65.52)]
    assert price_total(prices) == (11546, True)


def test_describe_price_estimate_copy():
    assert describe_price(6549, True) == (
        "65.49 ₪ לחודש. " + ESTIMATE_LABEL + ". אופן הצגת המע״מ טרם נקבע."
    )
    assert describe_price(4997, False) == "49.97 ₪ לחודש. אופן הצגת המע״מ טרם נקבע."
    assert describe_price(None, True) == UNKNOWN_PRICE


def test_stub_checkout_sets_pending(db, clock):
    with db() as s:
        owner = Owner(display_name="x", created_at=clock.now())
        s.add(owner)
        s.flush()
        pay = create_stub_checkout(s, owner.id, 6549, True, "x", clock.now())
        s.commit()
        assert s.get(Owner, owner.id).subscription_status == "pending_payment"
        got = s.get(Payment, pay.id)
        assert (got.amount_agorot, got.amount_is_estimate, got.provider) == (6549, True, "stub")
