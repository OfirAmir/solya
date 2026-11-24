import type { VatMode } from "../config";
import { formatIlsFromAgorot } from "../format";

/** 49.97 ILS up to 50 kW installed, then 1 ILS per kW above 50. Portal peak power is not an input. */
export const PRICE_BASE_AGOROT = 4997;
export const PRICE_THRESHOLD_KW = 50;
export const PRICE_EXTRA_AGOROT_PER_KW = 100;

export function monthlyPriceAgorot(installedKwDc: number | null): number | null {
  if (installedKwDc == null || !Number.isFinite(installedKwDc) || installedKwDc <= 0) return null;
  if (installedKwDc <= PRICE_THRESHOLD_KW) return PRICE_BASE_AGOROT;
  const extraKw = installedKwDc - PRICE_THRESHOLD_KW;
  return PRICE_BASE_AGOROT + Math.round(extraKw * PRICE_EXTRA_AGOROT_PER_KW);
}

export function priceForSite(site: { installedKwDc: number | null; portalPeakKw?: number | null }): number | null {
  return monthlyPriceAgorot(site.installedKwDc);
}

export function customerMonthlyAgorot(
  sites: { installedKwDc: number | null; monitor: boolean }[],
): number | null {
  const monitored = sites.filter((site) => site.monitor);
  if (monitored.length === 0) return null;
  let total = 0;
  for (const site of monitored) {
    const price = monthlyPriceAgorot(site.installedKwDc);
    if (price == null) return null;
    total += price;
  }
  return total;
}

export function describePrice(
  agorot: number | null,
  vat: { mode: VatMode; rate: number | null },
): string {
  if (agorot == null) {
    return "המחיר יחושב כשקילוואט מותקן שמור אצלנו. נתון ההספק מהפורטל לא משמש לחיוב.";
  }
  const amount = `${formatIlsFromAgorot(agorot)} ₪ לחודש`;
  if (vat.mode === "unset" || vat.rate == null) {
    return `${amount}. אופן הצגת המע״מ טרם נקבע.`;
  }
  if (vat.mode === "excluded") {
    return `${amount} לפני מע״מ (${vat.rate}%).`;
  }
  const gross = Math.round(agorot * (1 + vat.rate / 100));
  return `${formatIlsFromAgorot(gross)} ₪ לחודש כולל מע״מ (${vat.rate}%).`;
}
