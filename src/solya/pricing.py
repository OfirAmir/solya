"""Per-site monthly price: 49.97 ₪ up to 50 kW, plus 1 ₪ per kW above, rounded half-up."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from solya.models import Site

BASE_AGOROT = 4997
THRESHOLD_KW = Decimal(50)
PER_KW_AGOROT = Decimal(100)

ESTIMATE_LABEL = "הערכה לפי נתוני הפורטל, המחיר הסופי ייקבע לפי ההספק המותקן"
UNKNOWN_PRICE = "המחיר יחושב כשקילוואט מותקן שמור אצלנו. נתון ההספק מהפורטל לא משמש לחיוב."
VAT_NOTE = "אופן הצגת המע״מ טרם נקבע."


@dataclass(frozen=True)
class SitePrice:
    site_id: int
    agorot: int | None
    is_estimate: bool
    kw: float | None


def _agorot_for(kw: float) -> int:
    over = max(Decimal(str(kw)) - THRESHOLD_KW, Decimal(0))
    return BASE_AGOROT + int((over * PER_KW_AGOROT).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def price_site(site: Site) -> SitePrice:
    if site.billing_kwp_override is not None:
        kw, estimate = site.billing_kwp_override, False
    elif site.peak_power_kwp_reported is not None:
        kw, estimate = site.peak_power_kwp_reported, True
    else:
        return SitePrice(site.se_site_id, None, False, None)
    return SitePrice(site.se_site_id, _agorot_for(kw), estimate, kw)


def price_total(prices: list[SitePrice]) -> tuple[int | None, bool]:
    estimate = any(p.is_estimate for p in prices)
    if any(p.agorot is None for p in prices):
        return None, estimate
    return sum(p.agorot for p in prices), estimate  # type: ignore[misc]


def describe_price(agorot: int | None, is_estimate: bool) -> str:
    if agorot is None:
        return UNKNOWN_PRICE
    text = f"{agorot / 100:.2f} ₪ לחודש."
    if is_estimate:
        text += f" {ESTIMATE_LABEL}."
    return f"{text} {VAT_NOTE}"
