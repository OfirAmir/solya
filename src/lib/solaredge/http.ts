import type { AppConfig } from "../config";
import { parseAlerts, parseEnergyWh, parseExportWh, parseSite, parseTokenResponse } from "./parse";
import type { DailyPull, PulledBody, SolarEdgeClient, TokenSet } from "./types";
import { alertsUrl, energyUrl, meterTelemetryUrl, siteUrl, tokenRequestBody } from "./urls";

type FetchLike = typeof fetch;

export function createHttpSolarEdgeClient(config: AppConfig, fetchImpl: FetchLike = fetch): SolarEdgeClient {
  const { apiBase, tokenUrl, clientId, clientSecret } = config.solaredge;

  async function token(body: string): Promise<TokenSet> {
    const response = await fetchImpl(tokenUrl, {
      method: "POST",
      headers: { "content-type": "application/json", accept: "application/json" },
      body,
    });
    const json: unknown = await response.json().catch(() => null);
    if (!response.ok) {
      throw new Error(`SolarEdge token request failed with status ${response.status}.`);
    }
    return parseTokenResponse(json);
  }

  async function getJson(accessToken: string, url: string, endpoint: string): Promise<PulledBody> {
    const response = await fetchImpl(url, {
      headers: {
        accept: "application/json",
        authorization: `Bearer ${accessToken}`,
      },
    });
    const body: unknown = await response.json().catch(() => null);
    return {
      endpoint,
      urlPath: urlPathOnly(url),
      credits: response.ok ? 1 : 0,
      ok: response.ok,
      body,
    };
  }

  return {
    exchangeCode(code, redirectUri) {
      return token(
        tokenRequestBody({
          grantType: "authorization_code",
          code,
          clientId,
          clientSecret,
          redirectUri,
        }),
      );
    },
    refresh(refreshToken) {
      return token(
        tokenRequestBody({
          grantType: "refresh_token",
          refreshToken,
          clientId,
          clientSecret,
        }),
      );
    },
    async getSite(accessToken, siteId) {
      const call = await getJson(accessToken, siteUrl(apiBase, siteId), "site");
      if (!call.ok) throw new Error(`SolarEdge site lookup failed for ${siteId}.`);
      return parseSite(call.body, siteId);
    },
    async getDaily(accessToken, siteId, window): Promise<DailyPull> {
      const energy = await getJson(accessToken, energyUrl(apiBase, siteId, window.from, window.to), "energy");
      const meters = await getJson(
        accessToken,
        meterTelemetryUrl(apiBase, siteId, window.from, window.to),
        "meters",
      );
      const alerts = await getJson(accessToken, alertsUrl(apiBase, siteId), "alerts");
      const productionWh = energy.ok ? parseEnergyWh(energy.body) : null;
      const exportWh = meters.ok ? parseExportWh(meters.body) : null;
      return {
        calls: [energy, meters, alerts],
        productionWh,
        exportWh,
        alerts: alerts.ok ? parseAlerts(alerts.body) : [],
        communicationLost: !energy.ok || productionWh == null,
      };
    },
    async getOpenAlerts(accessToken, siteId) {
      const call = await getJson(accessToken, alertsUrl(apiBase, siteId), "alerts");
      return { call, alerts: call.ok ? parseAlerts(call.body) : [] };
    },
  };
}

function urlPathOnly(url: string): string {
  try {
    const parsed = new URL(url);
    return `${parsed.pathname}${parsed.search}`;
  } catch {
    return url.split("?")[0] ?? url;
  }
}
