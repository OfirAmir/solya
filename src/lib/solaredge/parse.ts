import type { SolarEdgeAlert, SolarEdgeSite } from "./types";

function asRecord(value: unknown): Record<string, unknown> | null {
  if (value && typeof value === "object" && !Array.isArray(value)) return value as Record<string, unknown>;
  return null;
}

function num(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() !== "" && Number.isFinite(Number(value))) return Number(value);
  return null;
}

/**
 * DAY energy series. A null or omitted bucket is missing data, not 0 Wh.
 * A numeric 0 is returned as 0.
 */
export function parseEnergyWh(body: unknown): number | null {
  const root = asRecord(body);
  const values = Array.isArray(root?.values) ? root.values : [];
  const numbers: number[] = [];
  let sawNull = false;
  for (const entry of values) {
    const row = asRecord(entry);
    if (!row || !("value" in row) || row.value == null) {
      sawNull = true;
      continue;
    }
    const value = num(row.value);
    if (value == null) sawNull = true;
    else numbers.push(value);
  }
  if (numbers.length === 0) return null;
  if (sawNull) return null;
  if (numbers.length === 1) return numbers[0];
  return numbers.reduce((sum, value) => sum + value, 0);
}

const EXPORT_TYPES = new Set([
  "feedin",
  "feed-in",
  "export",
  "exported",
  "gridexport",
]);

/** Export watt-hours. Unknown shape or a gap returns null, never a made-up zero. */
export function parseExportWh(body: unknown): number | null {
  const root = asRecord(body);
  if (!root) return null;

  const direct = num(root.exportEnergy) ?? num(root.energyExport) ?? num(root.feedIn);
  if (direct != null && !Array.isArray(root.values) && !Array.isArray(root.meters)) return direct;

  const meters = collectMeters(root);
  for (const meter of meters) {
    const type = String(meter.type ?? meter.meterType ?? meter.name ?? "").toLowerCase().replace(/\s+/g, "");
    if (!EXPORT_TYPES.has(type)) continue;
    return parseEnergyWh(meter.values ? { values: meter.values } : meter);
  }

  if (Array.isArray(root.values)) {
    const exports: number[] = [];
    let sawNull = false;
    for (const entry of root.values) {
      const row = asRecord(entry);
      if (!row) continue;
      const value = num(row.exportEnergy) ?? num(row.energyExport) ?? num(row.feedIn) ?? num(row.positiveActiveEnergy);
      if (value == null) {
        if ("exportEnergy" in row || "energyExport" in row || "feedIn" in row) sawNull = true;
        continue;
      }
      exports.push(value);
    }
    if (exports.length === 0 || sawNull) return exports.length && !sawNull ? exports[0] : null;
    return exports.reduce((sum, value) => sum + value, 0);
  }
  return null;
}

function collectMeters(root: Record<string, unknown>): Record<string, unknown>[] {
  const buckets: unknown[] = [];
  if (Array.isArray(root.meters)) buckets.push(...root.meters);
  const details = asRecord(root.energyDetails);
  if (details && Array.isArray(details.meters)) buckets.push(...details.meters);
  return buckets.map(asRecord).filter((row): row is Record<string, unknown> => row != null);
}

export function parseAlerts(body: unknown): SolarEdgeAlert[] {
  const root = asRecord(body);
  const list = Array.isArray(root?.alerts)
    ? root.alerts
    : Array.isArray(root?.list)
      ? root.list
      : Array.isArray(body)
        ? body
        : [];
  const alerts: SolarEdgeAlert[] = [];
  for (const entry of list) {
    const row = asRecord(entry);
    if (!row) continue;
    const statusRaw = String(row.status ?? "OPEN").toUpperCase();
    if (statusRaw && statusRaw !== "OPEN") continue;
    const code = String(row.type ?? row.alertType ?? row.code ?? row.name ?? "").trim();
    if (!code) continue;
    const serialValue = row.componentSerial ?? row.serialNumber ?? row.inverterSerial ?? row.deviceSerial ?? row.serial;
    const opened = row.firstTriggerTime ?? row.openTime ?? row.startTime ?? row.createdAt ?? row.time;
    alerts.push({
      code,
      serial: serialValue == null ? null : String(serialValue),
      openedAt: opened == null ? new Date(0).toISOString() : String(opened),
      status: "OPEN",
      raw: entry,
    });
  }
  return alerts;
}

export function parseSite(body: unknown, fallbackId: string): SolarEdgeSite {
  const root = asRecord(body) ?? {};
  const site = asRecord(root.site) ?? root;
  const location = asRecord(site.location) ?? {};
  const id = site.id ?? site.siteId ?? fallbackId;
  return {
    siteId: String(id),
    name: String(site.name ?? site.siteName ?? id),
    status: site.status == null ? null : String(site.status),
    peakPowerKw: num(site.peakPower) ?? num(site.peakPowerKw),
    latitude: num(location.latitude) ?? num(location.lat),
    longitude: num(location.longitude) ?? num(location.long) ?? num(location.lng),
    timeZone: location.timeZone == null && location.timezone == null
      ? null
      : String(location.timeZone ?? location.timezone),
  };
}

export function parseTokenResponse(body: unknown): {
  accessToken: string;
  refreshToken: string;
  expiresIn: number;
  tokenType: string;
} {
  const root = asRecord(body);
  const accessToken = root?.access_token;
  const refreshToken = root?.refresh_token;
  if (typeof accessToken !== "string" || typeof refreshToken !== "string" || !accessToken || !refreshToken) {
    throw new Error("SolarEdge token response did not include a new access token and refresh token.");
  }
  const expiresIn = num(root.expires_in) ?? 7200;
  const tokenType = typeof root.token_type === "string" ? root.token_type : "Bearer";
  return { accessToken, refreshToken, expiresIn, tokenType };
}
