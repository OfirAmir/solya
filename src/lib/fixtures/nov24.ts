import type { SiteDayInput } from "../rules/types";

/**
 * 24 Nov 2026, 16:30 bucket, from handover section 1.4.
 *
 * Energy and the reported shekel figures are the regression target.
 * Tariffs sit on the site (45 agorot on farm production, 48 on home export)
 * because that is the arithmetic of the approved report. The handover still
 * marks the tariff mapping as unconfirmed, so nothing else inherits these rates.
 *
 * RAY 92 and 93 are the reported composites. The component inputs are the
 * frozen 40/35/25 weights with both sites healthy, farm performance capped
 * at 100, one shared cloud cover, and a 4-point farm soiling gap. The handover
 * did not publish that split. Turning weather off will not reproduce 92 / 93.
 */
export const NOV24_LOCAL_DATE = "2026-11-24";
export const NOV24_TIME_ZONE = "Asia/Jerusalem";
export const NOV24_SENT_AT = new Date("2026-11-24T15:10:00.000Z"); // 17:10 IST

export const FARM_SITE_ID = "3033498";
export const HOME_SITE_ID = "4552265";

export const NOV24_REPORTED = {
  farmWh: 316_990,
  farmIncomeIls: 142.65,
  farmRay: 92,
  homeWh: 85_039,
  homeExportWh: 79_225,
  homeExportIncomeIls: 38.03,
  homeRay: 93,
  totalKwh: 402.0,
  totalIncomeIls: 180.68,
} as const;

const shared = {
  cloudCoverPercent: 70,
  openAlerts: [],
  communicationLost: false,
};

export const nov24Sites: SiteDayInput[] = [
  {
    siteId: FARM_SITE_ID,
    portalName: "אורן סופר",
    cardName: "אורן סופר",
    reportLabel: "משק",
    installedKwDc: 90.72,
    productionWh: NOV24_REPORTED.farmWh,
    exportWh: null,
    tariffAgorotPerKwh: 45,
    incomeBasis: "all_production",
    expectedWh: 300_000,
    soilingIndex: 4,
    ...shared,
  },
  {
    siteId: HOME_SITE_ID,
    portalName: "IL 360 - אורן סופר",
    cardName: "IL 360",
    reportLabel: "בית",
    installedKwDc: 26.55,
    productionWh: NOV24_REPORTED.homeWh,
    exportWh: NOV24_REPORTED.homeExportWh,
    tariffAgorotPerKwh: 48,
    incomeBasis: "export_only",
    expectedWh: 80_000,
    soilingIndex: 0,
    ...shared,
  },
];

export const BEIT_YITZHAK = { latitude: 32.325, longitude: 34.889 };

/** Portal registry figure the handover says is wrong. Never bill from this. */
export const FARM_PORTAL_PEAK_KW = 65.52;
