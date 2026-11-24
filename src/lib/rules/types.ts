export type IncomeBasis = "all_production" | "export_only";

export type OpenAlert = {
  code: string;
  serial: string | null;
  openedAt: string;
  status: "OPEN";
};

export type SiteDayInput = {
  siteId: string;
  portalName: string;
  cardName: string;
  /** משק or בית only when configured for that customer. Never invented. */
  reportLabel: string | null;
  installedKwDc: number | null;
  productionWh: number | null;
  exportWh: number | null;
  tariffAgorotPerKwh: number | null;
  incomeBasis: IncomeBasis;
  expectedWh: number | null;
  cloudCoverPercent: number | null;
  soilingIndex: number;
  openAlerts: OpenAlert[];
  /**
   * Set when updates stopped. A null production reading is also treated as
   * communication loss. A numeric zero is not this flag.
   */
  communicationLost: boolean;
};

export type RayBand = "green" | "orange" | "yellow" | "red";

export type SiteDayResult = {
  siteId: string;
  portalName: string;
  cardName: string;
  reportLabel: string | null;
  installedKwDc: number | null;
  productionWh: number | null;
  exportWh: number | null;
  incomeAgorot: number | null;
  ray: number | null;
  rayBand: RayBand | null;
  performance: number | null;
  health: number | null;
  environment: number | null;
  communicationLost: boolean;
  measuredZero: boolean;
  openAlerts: OpenAlert[];
};

export type DailySummary = {
  sites: SiteDayResult[];
  totalProductionWh: number | null;
  totalIncomeAgorot: number | null;
  overallRay: number | null;
  overallBand: RayBand | null;
  anyCommunicationLost: boolean;
  anyMeasuredZero: boolean;
  openAlertCount: number;
};
