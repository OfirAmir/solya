import type { IncomeBasis } from "./types";

/**
 * Shekels from watt-hours and agorot-per-kWh.
 * 316990 Wh at 45 agorot = 142.65 ILS. 79225 Wh at 48 agorot = 38.03 ILS.
 * A missing reading returns null. It is never coerced to zero income.
 */
export function incomeAgorot(input: {
  productionWh: number | null;
  exportWh: number | null;
  tariffAgorotPerKwh: number | null;
  basis: IncomeBasis;
}): number | null {
  if (input.tariffAgorotPerKwh == null) return null;
  const wh = input.basis === "export_only" ? input.exportWh : input.productionWh;
  if (wh == null) return null;
  return Math.round((wh * input.tariffAgorotPerKwh) / 1000);
}
