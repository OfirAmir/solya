import { mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import { renderCardPng } from "../src/lib/card/render";
import { NOV24_SENT_AT, NOV24_TIME_ZONE, nov24Sites } from "../src/lib/fixtures/nov24";
import { buildDailySummary } from "../src/lib/rules/engine";
import { summaryText } from "../src/lib/rules/hebrew";

const summary = buildDailySummary(nov24Sites, { includeWeather: true });
const text = summaryText(summary, {
  customerName: "אורן סופר",
  sentAt: NOV24_SENT_AT,
  timeZone: NOV24_TIME_ZONE,
});
const png = renderCardPng(summary);
const dir = path.join(process.cwd(), "docs");
mkdirSync(dir, { recursive: true });
const file = path.join(dir, "sample-card-2026-11-24.png");
writeFileSync(file, png);
process.stdout.write(`${text}\n\n${file}\n`);
