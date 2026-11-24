import { NextResponse } from "next/server";
import { seedFixtureSites } from "@/lib/app/runtime";
import { loadConfig } from "@/lib/config";
import { makeSolarEdgeClient } from "@/lib/app/runtime";
import { getStore } from "@/lib/store";

export async function GET(request: Request) {
  const url = new URL(request.url);
  const state = url.searchParams.get("state") || url.searchParams.get("external_id") || "";
  const store = getStore();
  const customerId = await store.consumeOauthSession(state);
  if (!customerId) return NextResponse.redirect(new URL("/connect", request.url), 303);

  const config = loadConfig();
  if (url.searchParams.get("fixture") === "1" && config.solaredge.mode === "fixture") {
    await seedFixtureSites(store, customerId);
    return NextResponse.redirect(new URL("/sites", request.url), 303);
  }

  const code = url.searchParams.get("code");
  const siteId = url.searchParams.get("site_id") || url.searchParams.get("siteId");
  if (!code || !siteId) {
    return NextResponse.json({ error: "SolarEdge did not return a code and site id." }, { status: 400 });
  }
  const client = makeSolarEdgeClient(config);
  const redirectUri = config.solaredge.redirectUri || new URL("/api/solaredge/callback", request.url).toString();
  const tokens = await client.exchangeCode(code, redirectUri);
  const remote = await client.getSite(tokens.accessToken, siteId);
  const site = await store.upsertSite({
    id: crypto.randomUUID(),
    customerId,
    solaredgeSiteId: remote.siteId,
    portalName: remote.name,
    cardName: remote.name,
    reportLabel: null,
    timezone: remote.timeZone || "Asia/Jerusalem",
    latitude: remote.latitude,
    longitude: remote.longitude,
    installedKwDc: null,
    portalPeakKw: remote.peakPowerKw,
    tariffAgorotPerKwh: null,
    incomeBasis: "all_production",
    expectedDailyWh: null,
    soilingIndex: 0,
    monitor: true,
  });
  const now = new Date().toISOString();
  await store.saveToken({
    siteId: site.id,
    accessToken: tokens.accessToken,
    refreshToken: tokens.refreshToken,
    accessExpiresAt: new Date(Date.now() + tokens.expiresIn * 1000).toISOString(),
    updatedAt: now,
    lastRefreshError: null,
    lastRefreshOkAt: now,
  });
  await store.setSubscription(customerId, "connected");
  return NextResponse.redirect(new URL("/sites", request.url), 303);
}
