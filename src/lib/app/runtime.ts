import { timingSafeEqual } from "node:crypto";
import type { AppConfig } from "../config";
import {
  BEIT_YITZHAK,
  FARM_PORTAL_PEAK_KW,
  FARM_SITE_ID,
  nov24Sites,
} from "../fixtures/nov24";
import { createFixtureSolarEdgeClient } from "../solaredge/fixture";
import { createHttpSolarEdgeClient } from "../solaredge/http";
import type { SolarEdgeClient } from "../solaredge/types";
import type { Store } from "../store/types";

export function makeSolarEdgeClient(config: AppConfig): SolarEdgeClient {
  if (config.solaredge.mode === "live") return createHttpSolarEdgeClient(config);
  return createFixtureSolarEdgeClient();
}

export function cronAuthorized(header: string | null, secret: string): boolean {
  if (!secret || !header?.startsWith("Bearer ")) return false;
  const token = header.slice("Bearer ".length);
  const a = Buffer.from(token);
  const b = Buffer.from(secret);
  if (a.length !== b.length) return false;
  return timingSafeEqual(a, b);
}

export async function seedFixtureSites(store: Store, customerId: string): Promise<void> {
  for (const input of nov24Sites) {
    const site = await store.upsertSite({
      id: crypto.randomUUID(),
      customerId,
      solaredgeSiteId: input.siteId,
      portalName: input.portalName,
      cardName: input.cardName,
      reportLabel: input.reportLabel,
      timezone: "Asia/Jerusalem",
      latitude: BEIT_YITZHAK.latitude,
      longitude: BEIT_YITZHAK.longitude,
      installedKwDc: input.installedKwDc,
      portalPeakKw: input.siteId === FARM_SITE_ID ? FARM_PORTAL_PEAK_KW : input.installedKwDc,
      tariffAgorotPerKwh: input.tariffAgorotPerKwh,
      incomeBasis: input.incomeBasis,
      expectedDailyWh: input.expectedWh,
      soilingIndex: input.soilingIndex,
      monitor: true,
    });
    const now = new Date().toISOString();
    await store.saveToken({
      siteId: site.id,
      accessToken: `fixture-access-${input.siteId}`,
      refreshToken: `fixture-refresh-${input.siteId}`,
      accessExpiresAt: new Date(Date.now() + 7200_000).toISOString(),
      updatedAt: now,
      lastRefreshError: null,
      lastRefreshOkAt: now,
    });
  }
  await store.setSubscription(customerId, "connected");
}
