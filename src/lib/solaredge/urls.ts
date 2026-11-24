/**
 * V2 query names confirmed against the Monitoring API OpenAPI notes
 * (api-docs.solaredge.com, info.version 2.0.1, via the published connector map)
 * and the Nov 2026 owner-authorization traces.
 *
 * The handover's working history calls used `resolution` and `from`.
 * `timeUnit`, `startDate`, `endDate`, and `startTime` returned the wrong day
 * and are not sent.
 */

export function energyUrl(apiBase: string, siteId: string, from: string, to: string): string {
  const query = new URLSearchParams({
    from,
    to,
    resolution: "DAY",
    unit: "WH",
  });
  return `${apiBase}/sites/${encodeURIComponent(siteId)}/energy?${query.toString()}`;
}

export function meterTelemetryUrl(apiBase: string, siteId: string, from: string, to: string): string {
  const query = new URLSearchParams({
    from,
    to,
    resolution: "DAY",
    unit: "WH",
  });
  return `${apiBase}/sites/${encodeURIComponent(siteId)}/meters/telemetry?${query.toString()}`;
}

export function alertsUrl(apiBase: string, siteId: string): string {
  const query = new URLSearchParams({
    "only-open": "true",
    "alerts-in-page": "100",
  });
  return `${apiBase}/sites/${encodeURIComponent(siteId)}/alerts?${query.toString()}`;
}

export function siteUrl(apiBase: string, siteId: string): string {
  return `${apiBase}/sites/${encodeURIComponent(siteId)}`;
}

export function authorizeUrl(input: {
  authorizeUrl: string;
  clientId: string;
  redirectUri: string;
  scopes: string;
  state: string;
  accessDuration: string;
}): string {
  const url = new URL(input.authorizeUrl);
  url.searchParams.set("response_type", "code");
  url.searchParams.set("client_id", input.clientId);
  url.searchParams.set("redirect_uri", input.redirectUri);
  url.searchParams.set("scope", input.scopes);
  url.searchParams.set("state", input.state);
  // SolarEdge often omits `state` on the way back and returns `external_id`.
  url.searchParams.set("external_id", input.state);
  url.searchParams.set("access_duration", input.accessDuration);
  return url.toString();
}

export function tokenRequestBody(input: {
  grantType: "authorization_code" | "refresh_token";
  code?: string;
  refreshToken?: string;
  clientId: string;
  clientSecret: string;
  redirectUri?: string;
}): string {
  const body: Record<string, string> = {
    grant_type: input.grantType,
    client_id: input.clientId,
    client_secret: input.clientSecret,
  };
  if (input.grantType === "authorization_code") {
    body.code = input.code ?? "";
    if (input.redirectUri) body.redirect_uri = input.redirectUri;
  } else {
    body.refresh_token = input.refreshToken ?? "";
  }
  return JSON.stringify(body);
}
