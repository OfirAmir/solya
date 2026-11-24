export function formatIlsFromAgorot(agorot: number): string {
  const sign = agorot < 0 ? "-" : "";
  const abs = Math.abs(Math.round(agorot));
  const shekels = Math.floor(abs / 100);
  const agora = abs % 100;
  return `${sign}${shekels}.${agora.toString().padStart(2, "0")}`;
}

export function formatKwhFromWh(wh: number, decimals: number): string {
  const factor = 10 ** decimals;
  const rounded = Math.round((wh / 1000) * factor) / factor;
  return rounded.toFixed(decimals);
}

export function formatHourMinute(date: Date, timeZone: string): string {
  const parts = new Intl.DateTimeFormat("en-GB", {
    timeZone,
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).formatToParts(date);
  const hour = parts.find((p) => p.type === "hour")?.value ?? "00";
  const minute = parts.find((p) => p.type === "minute")?.value ?? "00";
  return `${hour}:${minute}`;
}

export function localDateString(date: Date, timeZone: string): string {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(date);
  const y = parts.find((p) => p.type === "year")?.value;
  const m = parts.find((p) => p.type === "month")?.value;
  const d = parts.find((p) => p.type === "day")?.value;
  return `${y}-${m}-${d}`;
}

export function hourInZone(date: Date, timeZone: string): number {
  const parts = new Intl.DateTimeFormat("en-GB", {
    timeZone,
    hour: "2-digit",
    hourCycle: "h23",
  }).formatToParts(date);
  return Number(parts.find((p) => p.type === "hour")?.value ?? "0");
}

export function greetingForHour(hour: number): string {
  if (hour >= 5 && hour < 12) return "בוקר טוב";
  if (hour >= 12 && hour < 17) return "צהריים טובים";
  if (hour >= 17 && hour < 22) return "ערב טוב";
  return "לילה טוב";
}

export function firstName(name: string): string {
  const trimmed = name.trim();
  if (!trimmed) return "";
  return trimmed.split(/\s+/)[0] ?? trimmed;
}

/** Local midnight as an ISO string with the numeric offset SolarEdge expects. */
export function localDayWindow(anchor: Date, timeZone: string): { from: string; to: string; localDate: string } {
  const localDate = localDateString(anchor, timeZone);
  const from = zonedMidnightIso(localDate, timeZone);
  const next = nextLocalDate(localDate);
  const to = zonedMidnightIso(next, timeZone);
  return { from, to, localDate };
}

function nextLocalDate(isoDate: string): string {
  const [y, m, d] = isoDate.split("-").map(Number);
  const utc = new Date(Date.UTC(y, m - 1, d));
  utc.setUTCDate(utc.getUTCDate() + 1);
  const yy = utc.getUTCFullYear();
  const mm = String(utc.getUTCMonth() + 1).padStart(2, "0");
  const dd = String(utc.getUTCDate()).padStart(2, "0");
  return `${yy}-${mm}-${dd}`;
}

function zonedMidnightIso(isoDate: string, timeZone: string): string {
  const [y, m, d] = isoDate.split("-").map(Number);
  // Probe UTC noon, then shift back to local midnight.
  const probe = new Date(Date.UTC(y, m - 1, d, 12, 0, 0));
  const offsetMinutes = offsetMinutesAt(probe, timeZone);
  const utcMillis = Date.UTC(y, m - 1, d, 0, 0, 0) - offsetMinutes * 60_000;
  const instant = new Date(utcMillis);
  const resolvedOffset = offsetMinutesAt(instant, timeZone);
  const corrected = new Date(Date.UTC(y, m - 1, d, 0, 0, 0) - resolvedOffset * 60_000);
  return formatOffsetIso(corrected, offsetMinutesAt(corrected, timeZone));
}

function offsetMinutesAt(date: Date, timeZone: string): number {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone,
    timeZoneName: "longOffset",
    hour: "2-digit",
  }).formatToParts(date);
  const name = parts.find((p) => p.type === "timeZoneName")?.value ?? "GMT+00:00";
  const match = name.match(/GMT([+-])(\d{1,2})(?::(\d{2}))?/);
  if (!match) return 0;
  const sign = match[1] === "-" ? -1 : 1;
  return sign * (Number(match[2]) * 60 + Number(match[3] ?? "0"));
}

function formatOffsetIso(date: Date, offsetMinutes: number): string {
  const local = new Date(date.getTime() + offsetMinutes * 60_000);
  const y = local.getUTCFullYear();
  const m = String(local.getUTCMonth() + 1).padStart(2, "0");
  const d = String(local.getUTCDate()).padStart(2, "0");
  const hh = String(local.getUTCHours()).padStart(2, "0");
  const mm = String(local.getUTCMinutes()).padStart(2, "0");
  const ss = String(local.getUTCSeconds()).padStart(2, "0");
  const sign = offsetMinutes >= 0 ? "+" : "-";
  const abs = Math.abs(offsetMinutes);
  const oh = String(Math.floor(abs / 60)).padStart(2, "0");
  const om = String(abs % 60).padStart(2, "0");
  return `${y}-${m}-${d}T${hh}:${mm}:${ss}${sign}${oh}:${om}`;
}
