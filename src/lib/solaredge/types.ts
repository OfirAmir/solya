export type TokenSet = {
  accessToken: string;
  refreshToken: string;
  expiresIn: number;
  tokenType: string;
};

export type SolarEdgeSite = {
  siteId: string;
  name: string;
  status: string | null;
  peakPowerKw: number | null;
  latitude: number | null;
  longitude: number | null;
  timeZone: string | null;
};

export type SolarEdgeAlert = {
  code: string;
  serial: string | null;
  openedAt: string;
  status: "OPEN" | "CLOSED";
  raw: unknown;
};

export type PulledBody = {
  endpoint: string;
  urlPath: string;
  credits: number;
  ok: boolean;
  body: unknown;
};

export type DailyPull = {
  calls: PulledBody[];
  productionWh: number | null;
  exportWh: number | null;
  alerts: SolarEdgeAlert[];
  /** True when the day bucket is missing. Numeric zero stays false. */
  communicationLost: boolean;
};

export interface SolarEdgeClient {
  exchangeCode(code: string, redirectUri: string): Promise<TokenSet>;
  refresh(refreshToken: string): Promise<TokenSet>;
  getSite(accessToken: string, siteId: string): Promise<SolarEdgeSite>;
  getDaily(accessToken: string, siteId: string, window: { from: string; to: string }): Promise<DailyPull>;
  getOpenAlerts(accessToken: string, siteId: string): Promise<{ call: PulledBody; alerts: SolarEdgeAlert[] }>;
}
