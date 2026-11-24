import { firstName, formatHourMinute, formatIlsFromAgorot, formatKwhFromWh, greetingForHour, hourInZone } from "../format";
import type { DailySummary, SiteDayResult } from "./types";

const FORBIDDEN = ["החווה", "ביחד", "רווח", "ניסיוני", "הכל תקין", "הכול תקין"];

export function assertCustomerWording(text: string): void {
  for (const word of FORBIDDEN) {
    if (text.includes(word)) {
      throw new Error(`Customer text used a forbidden word: ${word}`);
    }
  }
}

export function summaryText(
  summary: DailySummary,
  ctx: { customerName: string; sentAt: Date; timeZone: string },
): string {
  const hour = hourInZone(ctx.sentAt, ctx.timeZone);
  const name = firstName(ctx.customerName);
  const lines: string[] = [`${greetingForHour(hour)} ${name}`.trim()];

  const healthy = summary.sites.filter((site) => !site.communicationLost && !site.measuredZero);
  if (summary.sites.length >= 2 && summary.sites.length <= 8 && summary.overallRay != null && !summary.anyCommunicationLost) {
    lines.push(`הציון הכולל RAY FACTOR עומד על ${summary.overallRay}`);
  }
  if (summary.sites.length > 8) {
    lines.push(...compactManySites(summary));
  } else {
    for (const site of summary.sites) lines.push(...siteLines(site));
  }

  if (summary.totalProductionWh != null && summary.totalIncomeAgorot != null && summary.sites.length > 1) {
    lines.push(
      `סה״כ ייצור ${formatKwhFromWh(summary.totalProductionWh, 1)} קוט״ש והכנסה ${formatIlsFromAgorot(summary.totalIncomeAgorot)} ₪`,
    );
  }

  if (!summary.anyCommunicationLost && !summary.anyMeasuredZero && summary.openAlertCount === 0) {
    lines.push("אין התראות פעילות");
  }

  if (summary.anyCommunicationLost) {
    lines.push("המידע עדיין לא הגיע אלי, אני על זה.");
  } else if (!summary.anyMeasuredZero) {
    lines.push("לכל שאלה אני כאן. המשך ערב טוב.");
  }

  const text = lines.join("\n");
  assertCustomerWording(text);
  return text;
}

function siteLines(site: SiteDayResult): string[] {
  const who = subject(site);
  if (site.communicationLost) {
    return [`${who} נפסקה התקשורת. אין קריאה עדכנית ולא מוצג ייצור או מצב.`];
  }
  if (site.measuredZero) {
    return [`${who} נמדד ייצור אפס. ייצור אפס הוא תקלה, ולא חוסר קריאה.`];
  }
  const lines: string[] = [];
  if (site.ray != null) {
    lines.push(`${who} עומד RAY FACTOR על ${site.ray}`);
  }
  const bits: string[] = [];
  if (site.productionWh != null) {
    bits.push(`ייצור ${formatKwhFromWh(site.productionWh, 2)} קוט״ש`);
  }
  if (site.incomeAgorot != null) {
    bits.push(`הכנסה ${formatIlsFromAgorot(site.incomeAgorot)} ₪`);
  }
  if (bits.length) lines.push(bits.join(" ו"));
  return lines;
}

function subject(site: SiteDayResult): string {
  if (site.reportLabel === "משק") return "במשק";
  if (site.reportLabel === "בית") return "בבית";
  return `במערכת ${site.cardName}`;
}

function compactManySites(summary: DailySummary): string[] {
  const lines: string[] = [];
  if (summary.overallRay != null) {
    lines.push(`הציון הכולל RAY FACTOR עומד על ${summary.overallRay}`);
  }
  const attention = summary.sites.filter(
    (site) => site.communicationLost || site.measuredZero || (site.ray != null && site.ray < 80),
  );
  const quiet = summary.sites.length - attention.length;
  lines.push(`מערכות ללא חריגה ${quiet}`);
  for (const site of attention) lines.push(...siteLines(site));
  return lines;
}

export function faultAlertText(input: {
  customerName: string;
  sentAt: Date;
  timeZone: string;
  detectedAt: Date;
  code: string;
  serial: string | null;
  mayOpenServiceCall: boolean;
}): string {
  const greet = greetingForHour(hourInZone(input.sentAt, input.timeZone));
  const name = firstName(input.customerName);
  const when = formatHourMinute(input.detectedAt, input.timeZone);
  const serial = input.serial ? `בממיר ${input.serial}` : "בממיר שהמספר שלו לא הגיע בקריאה";
  const cause =
    input.code === "FAN_ALERT_1"
      ? "ככל הנראה מדובר בתקלה במאוורר הקירור של הממיר."
      : "ככל הנראה נדרשת בדיקה של איש מקצוע מוסמך, והסיבה המדויקת לא נקבעה מהנתונים.";
  // The flag is a capability switch. Chat still cannot open the call.
  // The approved wording recommends that the customer open it.
  void input.mayOpenServiceCall;
  const text = [
    `${greet} ${name}, בשעה ${when} זיהיתי התראה על תקלה במערכת הסולארית שלך: ${input.code} ${serial}.`,
    cause,
    "נדרשת בדיקה של איש מקצוע מוסמך לאבחון ולטיפול, ולא טיפול עצמי.",
    "אני ממליצה לפתוח קריאת שירות מול SolarEdge או להזמין טכנאי מוסמך.",
    "עדכן אותי ואעקוב עד לסיום הטיפול.",
  ].join(" ");
  assertCustomerWording(text);
  return text;
}

export const WELCOME_MESSAGES = [
  "ברוך הבא לסוליה ☀️ אני סול, הנציגה שלך. אני שומרת על המערכת הסולארית שלך וכאן בשבילך 24/7.",
  "בכל ערב אשלח סיכום עם הציון RAY FACTOR, הייצור וההכנסה. אם אזהה תקלה אעדכן.",
] as const;
