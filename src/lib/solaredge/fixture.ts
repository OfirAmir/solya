import {
  BEIT_YITZHAK,
  FARM_PORTAL_PEAK_KW,
  FARM_SITE_ID,
  HOME_SITE_ID,
  nov24Sites,
} from "../fixtures/nov24";
import type { DailyPull, SolarEdgeClient, SolarEdgeSite, TokenSet } from "./types";

const sites = new Map<string, SolarEdgeSite>([
  [
    FARM_SITE_ID,
    {
      siteId: FARM_SITE_ID,
      name: "אורן סופר",
      status: "ACTIVE",
      peakPowerKw: FARM_PORTAL_PEAK_KW,
      latitude: BEIT_YITZHAK.latitude,
      longitude: BEIT_YITZHAK.longitude,
      timeZone: "Asia/Jerusalem",
    },
  ],
  [
    HOME_SITE_ID,
    {
      siteId: HOME_SITE_ID,
      name: "IL 360 - אורן סופר",
      status: "ACTIVE",
      peakPowerKw: 26.55,
      latitude: BEIT_YITZHAK.latitude,
      longitude: BEIT_YITZHAK.longitude,
      timeZone: "Asia/Jerusalem",
    },
  ],
]);

/**
 * Recorded 24 Nov responses. Refresh rotates the token every call.
 * No network. Credits are counted the same way as a live data call.
 */
export function createFixtureSolarEdgeClient(): SolarEdgeClient {
  let refreshCount = 0;
  return {
    async exchangeCode(code: string): Promise<TokenSet> {
      return {
        accessToken: `fixture-access-${code}`,
        refreshToken: `fixture-refresh-${code}`,
        expiresIn: 7200,
        tokenType: "Bearer",
      };
    },
    async refresh(refreshToken: string): Promise<TokenSet> {
      refreshCount += 1;
      return {
        accessToken: `fixture-access-${refreshCount}`,
        refreshToken: `rotated-${refreshCount}-${refreshToken.slice(0, 12)}`,
        expiresIn: 7200,
        tokenType: "Bearer",
      };
    },
    async getSite(_accessToken, siteId) {
      const site = sites.get(siteId);
      if (!site) throw new Error(`Fixture has no site ${siteId}.`);
      return site;
    },
    async getDaily(_accessToken, siteId): Promise<DailyPull> {
      const input = nov24Sites.find((site) => site.siteId === siteId);
      if (!input) throw new Error(`Fixture has no day for ${siteId}.`);
      return {
        calls: [
          {
            endpoint: "energy",
            urlPath: `/sites/${siteId}/energy?resolution=DAY&unit=WH`,
            credits: 1,
            ok: true,
            body: {
              unit: "WH",
              resolution: "DAY",
              values: [{ timestamp: "2026-11-24T00:00:00+02:00", value: input.productionWh }],
              recordedContext: {
                cloudCoverPercent: input.cloudCoverPercent,
                note: "Recorded with the 24 Nov report. Live responses do not include this field.",
              },
            },
          },
          {
            endpoint: "meters",
            urlPath: `/sites/${siteId}/meters/telemetry?resolution=DAY&unit=WH`,
            credits: 1,
            ok: true,
            body: {
              meters: [
                {
                  type: "FeedIn",
                  values: [{ timestamp: "2026-11-24T00:00:00+02:00", value: input.exportWh }],
                },
              ],
            },
          },
          {
            endpoint: "alerts",
            urlPath: `/sites/${siteId}/alerts?only-open=true`,
            credits: 1,
            ok: true,
            body: { alerts: [] },
          },
        ],
        productionWh: input.productionWh,
        exportWh: input.exportWh,
        alerts: [],
        communicationLost: false,
      };
    },
    async getOpenAlerts(_accessToken, siteId) {
      return {
        call: {
          endpoint: "alerts",
          urlPath: `/sites/${siteId}/alerts?only-open=true`,
          credits: 1,
          ok: true,
          body: { alerts: [] },
        },
        alerts: [],
      };
    },
  };
}
