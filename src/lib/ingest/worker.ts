import { renderCardPng } from "../card/render";
import type { AppConfig } from "../config";
import { DAILY_DATA_CALLS_PER_SITE } from "../cost";
import { hourInZone, localDateString, localDayWindow } from "../format";
import type { MessagingAdapter } from "../messaging/adapter";
import { faultAlertText, summaryText } from "../rules/hebrew";
import { buildDailySummary } from "../rules/engine";
import type { OpenAlert, SiteDayInput } from "../rules/types";
import { parseAlerts, parseEnergyWh, parseExportWh } from "../solaredge/parse";
import type { SolarEdgeClient } from "../solaredge/types";
import { eveningSendAt, isInSendWindow } from "../sunset";
import { refreshAndPersist } from "../tokens/rotate";
import type { SiteRecord, Store } from "../store/types";

export type IngestResult = {
  pulledSites: number;
  sentCustomers: number;
  skipped: string[];
};

export async function runIngest(input: {
  store: Store;
  client: SolarEdgeClient;
  config: AppConfig;
  now: Date;
  kind: "daily" | "early";
  messenger?: MessagingAdapter | null;
  force?: boolean;
}): Promise<IngestResult> {
  const skipped: string[] = [];
  if (input.kind === "daily" && !input.config.polling.dailyEnabled) {
    return { pulledSites: 0, sentCustomers: 0, skipped: ["daily pull disabled"] };
  }
  if (input.kind === "early" && !input.config.polling.earlyPullEnabled) {
    return { pulledSites: 0, sentCustomers: 0, skipped: ["early pull disabled"] };
  }

  const sites = await input.store.listMonitoredSites();
  let pulledSites = 0;
  for (const site of sites) {
    const localDate = localDateString(input.now, site.timezone);
    const endpoint = input.kind === "early" ? "alerts" : "energy";
    if (await input.store.hasPull(site.id, localDate, input.kind, endpoint)) {
      skipped.push(`${site.solaredgeSiteId} already pulled`);
      continue;
    }
    const monthCredits = await input.store.creditsSince(site.customerId, monthStart(input.now));
    const nextCredits = input.kind === "early" ? 1 : DAILY_DATA_CALLS_PER_SITE;
    if (input.kind === "early" && nextCredits > input.config.polling.earlyPullMaxCreditsPerSite) {
      skipped.push(`${site.solaredgeSiteId} early cap`);
      continue;
    }
    if (monthCredits + nextCredits > input.config.polling.maxCreditsPerCustomerMonth) {
      skipped.push(`${site.solaredgeSiteId} monthly credit cap`);
      continue;
    }

    const refreshed = await refreshAndPersist(
      site.id,
      {
        read: (id) => input.store.readToken(id),
        replace: async (id, token) => {
          await input.store.saveToken({
            siteId: id,
            accessToken: token.accessToken,
            refreshToken: token.refreshToken,
            accessExpiresAt: token.accessExpiresAt,
            updatedAt: input.now.toISOString(),
            lastRefreshError: null,
            lastRefreshOkAt: input.now.toISOString(),
          });
        },
        recordFailure: (id, error) => input.store.recordRefreshFailure(id, error),
      },
      (refreshToken) => input.client.refresh(refreshToken),
      input.now,
    );
    if (!refreshed.ok) {
      skipped.push(`${site.solaredgeSiteId} refresh failed`);
      continue;
    }

    const window = localDayWindow(input.now, site.timezone);
    if (input.kind === "early") {
      const alerts = await input.client.getOpenAlerts(refreshed.accessToken, site.solaredgeSiteId);
      await storeCall(input, site, localDate, alerts.call.endpoint, alerts.call.credits, alerts.call.ok, alerts.call.body);
      await rememberAlerts(input.store, site, alerts.alerts, input.now);
    } else {
      const daily = await input.client.getDaily(refreshed.accessToken, site.solaredgeSiteId, window);
      for (const call of daily.calls) {
        await storeCall(input, site, localDate, call.endpoint, call.credits, call.ok, call.body);
      }
      await rememberAlerts(input.store, site, daily.alerts, input.now);
    }
    pulledSites += 1;
  }

  const sentCustomers = input.kind === "daily" ? await sendDue(input) : 0;
  return { pulledSites, sentCustomers, skipped };
}

async function storeCall(
  input: { store: Store; now: Date; kind: "daily" | "early" },
  site: SiteRecord,
  localDate: string,
  endpoint: string,
  credits: number,
  ok: boolean,
  body: unknown,
): Promise<void> {
  await input.store.insertPull({
    siteId: site.id,
    customerId: site.customerId,
    pulledAt: input.now.toISOString(),
    localDate,
    kind: input.kind,
    endpoint,
    credits,
    ok,
    body,
  });
  if (credits > 0) {
    await input.store.addCredits({
      customerId: site.customerId,
      siteId: site.id,
      credits,
      endpoint,
      localDate,
      at: input.now.toISOString(),
    });
  }
}

async function rememberAlerts(
  store: Store,
  site: SiteRecord,
  alerts: { code: string; serial: string | null; openedAt: string; status: string }[],
  now: Date,
): Promise<void> {
  for (const alert of alerts) {
    if (alert.status !== "OPEN") continue;
    await store.upsertAlert({
      siteId: site.id,
      customerId: site.customerId,
      dedupeKey: `${alert.code}:${alert.serial ?? ""}:${alert.openedAt}`,
      code: alert.code,
      serial: alert.serial,
      openedAt: alert.openedAt,
      status: "OPEN",
      detectedAt: now.toISOString(),
      notifiedAt: null,
    });
  }
}

async function sendDue(input: {
  store: Store;
  config: AppConfig;
  now: Date;
  messenger?: MessagingAdapter | null;
  force?: boolean;
}): Promise<number> {
  const sites = await input.store.listMonitoredSites();
  const byCustomer = new Map<string, SiteRecord[]>();
  for (const site of sites) {
    const list = byCustomer.get(site.customerId) ?? [];
    list.push(site);
    byCustomer.set(site.customerId, list);
  }
  let sent = 0;
  for (const [customerId, customerSites] of byCustomer) {
    const zone = customerSites[0]?.timezone ?? "Asia/Jerusalem";
    const localDate = localDateString(input.now, zone);
    const existing = await input.store.getSummary(customerId, localDate);
    if (existing?.sentAt) continue;
    if (!input.force && !readyToSend(input.now, customerSites, input.config)) continue;

    const dayInputs: SiteDayInput[] = [];
    for (const site of customerSites) {
      dayInputs.push(await siteInputFromStore(input.store, site, localDate));
    }
    const summary = buildDailySummary(dayInputs, { includeWeather: input.config.ray.includeWeather });
    const customer = await input.store.getCustomer(customerId);
    if (!customer) continue;
    const text = summaryText(summary, { customerName: customer.name, sentAt: input.now, timeZone: zone });
    await input.store.saveSummary({
      customerId,
      localDate,
      payload: summary,
      text,
      sentAt: null,
    });

    if (input.messenger && customer.telegramChatId) {
      const png = renderCardPng(summary);
      await input.messenger.sendCard(customer.telegramChatId, png);
      await input.messenger.sendText(customer.telegramChatId, text);
      const alerts = await input.store.unnotifiedAlerts(customerId);
      for (const alert of alerts) {
        const fault = faultAlertText({
          customerName: customer.name,
          sentAt: input.now,
          timeZone: zone,
          detectedAt: new Date(alert.detectedAt),
          code: alert.code,
          serial: alert.serial,
          mayOpenServiceCall: input.config.solMayOpenServiceCalls,
        });
        await input.messenger.sendText(customer.telegramChatId, fault);
        await input.store.markAlertNotified(alert.id, input.now.toISOString());
      }
      await input.store.markSummarySent(customerId, localDate, input.now.toISOString());
      sent += 1;
    }
  }
  return sent;
}

function readyToSend(now: Date, sites: SiteRecord[], config: AppConfig): boolean {
  const site = sites.find((item) => item.latitude != null && item.longitude != null) ?? sites[0];
  if (!site) return false;
  const localDate = localDateString(now, site.timezone);
  if (site.latitude == null || site.longitude == null) {
    return hourInZone(now, site.timezone) >= config.polling.fallbackSendHour;
  }
  const sendAt = eveningSendAt(localDate, site.latitude, site.longitude, 30);
  return isInSendWindow(now, sendAt, config.polling.sendWindowHours);
}

async function siteInputFromStore(store: Store, site: SiteRecord, localDate: string): Promise<SiteDayInput> {
  const pulls = (await store.listPulls(site.id)).filter((pull) => pull.localDate === localDate && pull.kind === "daily" && pull.ok);
  const energy = pulls.find((pull) => pull.endpoint === "energy");
  const meters = pulls.find((pull) => pull.endpoint === "meters");
  const productionWh = energy ? parseEnergyWh(energy.body) : null;
  const exportWh = meters ? parseExportWh(meters.body) : null;
  const recorded = energy && typeof energy.body === "object" && energy.body && "recordedContext" in energy.body
    ? (energy.body as { recordedContext?: { cloudCoverPercent?: unknown } }).recordedContext
    : undefined;
  const cloudCoverPercent = typeof recorded?.cloudCoverPercent === "number" ? recorded.cloudCoverPercent : null;
  const alertPull = pulls.find((pull) => pull.endpoint === "alerts");
  const openAlerts: OpenAlert[] = (alertPull ? parseAlerts(alertPull.body) : [])
    .filter((alert) => alert.status === "OPEN")
    .map((alert) => ({
      code: alert.code,
      serial: alert.serial,
      openedAt: alert.openedAt,
      status: "OPEN" as const,
    }));
  return {
    siteId: site.solaredgeSiteId,
    portalName: site.portalName,
    cardName: site.cardName,
    reportLabel: site.reportLabel,
    installedKwDc: site.installedKwDc,
    productionWh,
    exportWh,
    tariffAgorotPerKwh: site.tariffAgorotPerKwh,
    incomeBasis: site.incomeBasis,
    expectedWh: site.expectedDailyWh,
    cloudCoverPercent,
    soilingIndex: site.soilingIndex,
    openAlerts,
    communicationLost: productionWh == null,
  };
}

function monthStart(now: Date): string {
  return new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), 1)).toISOString();
}
