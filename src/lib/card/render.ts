import { createCanvas, GlobalFonts } from "@napi-rs/canvas";
import { existsSync } from "node:fs";
import path from "node:path";
import { formatIlsFromAgorot, formatKwhFromWh } from "../format";
import { rayBandLabel } from "../rules/ray";
import type { DailySummary, RayBand, SiteDayResult } from "../rules/types";

const BAND: Record<RayBand, string> = {
  green: "#1F8A4C",
  orange: "#E07A2F",
  yellow: "#C8962E",
  red: "#C5362B",
};

let fontsReady = false;

function registerFonts(): void {
  if (fontsReady) return;
  const dir = path.join(process.cwd(), "assets", "fonts");
  const regular = path.join(dir, "NotoSansHebrew-Regular.ttf");
  const bold = path.join(dir, "NotoSansHebrew-Bold.ttf");
  if (!existsSync(regular) || !existsSync(bold)) {
    throw new Error("Hebrew fonts are missing from assets/fonts.");
  }
  GlobalFonts.registerFromPath(regular, "SolyaHebrew");
  GlobalFonts.registerFromPath(bold, "SolyaHebrewBold");
  fontsReady = true;
}

export type CardModel = {
  ray: number | null;
  band: RayBand | null;
  bandLabel: string | null;
  rows: { name: string; production: string | null; income: string | null; missing: boolean; fault: boolean }[];
  totalProduction: string | null;
  totalIncome: string | null;
};

export function cardModel(summary: DailySummary): CardModel {
  return {
    ray: summary.overallRay,
    band: summary.overallBand,
    bandLabel: summary.overallBand ? rayBandLabel(summary.overallBand) : null,
    rows: summary.sites.map((site) => rowFor(site)),
    totalProduction: summary.totalProductionWh == null ? null : formatKwhFromWh(summary.totalProductionWh, 1),
    totalIncome: summary.totalIncomeAgorot == null ? null : formatIlsFromAgorot(summary.totalIncomeAgorot),
  };
}

function rowFor(site: SiteDayResult): CardModel["rows"][number] {
  if (site.measuredZero) {
    return { name: site.cardName, production: null, income: null, missing: false, fault: true };
  }
  if (site.communicationLost || site.productionWh == null) {
    return { name: site.cardName, production: null, income: null, missing: true, fault: false };
  }
  return {
    name: site.cardName,
    production: formatKwhFromWh(site.productionWh, 2),
    income: site.incomeAgorot == null ? null : formatIlsFromAgorot(site.incomeAgorot),
    missing: false,
    fault: false,
  };
}

export function renderCardPng(summary: DailySummary): Buffer {
  registerFonts();
  const model = cardModel(summary);
  const width = 880;
  const height = 360 + model.rows.length * 168 + 160;
  const canvas = createCanvas(width, height);
  const ctx = canvas.getContext("2d");
  ctx.direction = "rtl";

  ctx.fillStyle = "#E6F0E8";
  ctx.fillRect(0, 0, width, height);

  roundRect(ctx, 28, 28, width - 56, height - 56, 28);
  ctx.fillStyle = "#FFFFFF";
  ctx.fill();

  const band = model.band ? BAND[model.band] : "#8AA394";
  ctx.beginPath();
  ctx.arc(width / 2, 168, 78, 0, Math.PI * 2);
  ctx.fillStyle = band;
  ctx.fill();

  ctx.fillStyle = "#FFFFFF";
  ctx.font = "64px SolyaHebrewBold";
  ctx.textAlign = "center";
  ctx.direction = "ltr";
  ctx.fillText(model.ray == null ? "—" : String(model.ray), width / 2, 190);

  ctx.fillStyle = "#6B7280";
  ctx.font = "18px SolyaHebrew";
  ctx.fillText("RAY FACTOR", width / 2, 270);

  if (model.bandLabel) {
    ctx.fillStyle = band;
    ctx.font = "22px SolyaHebrewBold";
    ctx.direction = "rtl";
    ctx.fillText(model.bandLabel, width / 2, 304);
  }

  let y = model.bandLabel ? 340 : 310;
  for (const row of model.rows) {
    y += 36;
    ctx.textAlign = "right";
    ctx.direction = "rtl";
    ctx.fillStyle = "#163026";
    ctx.font = "28px SolyaHebrewBold";
    ctx.fillText(row.name, width - 72, y);

    y += 34;
    ctx.fillStyle = "#8A938C";
    ctx.font = "16px SolyaHebrew";
    ctx.textAlign = "left";
    ctx.direction = "rtl";
    ctx.fillText("הכנסה", 80, y);
    ctx.textAlign = "right";
    ctx.fillText("ייצור", width - 80, y);

    y += 42;
    ctx.fillStyle = "#163026";
    ctx.font = "36px SolyaHebrewBold";
    ctx.textAlign = "left";
    ctx.direction = "ltr";
    const empty = row.fault ? "תקלה" : "אין קריאה";
    ctx.fillText(row.income ? `${row.income} ₪` : empty, 80, y);
    ctx.textAlign = "right";
    ctx.fillText(row.production ? `${row.production} קוט״ש` : empty, width - 80, y);
    y += 28;
  }

  y += 16;
  ctx.strokeStyle = "#E1E8E3";
  ctx.beginPath();
  ctx.moveTo(72, y);
  ctx.lineTo(width - 72, y);
  ctx.stroke();

  y += 42;
  ctx.direction = "rtl";
  ctx.textAlign = "right";
  ctx.fillStyle = "#163026";
  ctx.font = "22px SolyaHebrewBold";
  ctx.fillText("סה״כ", width - 72, y);

  y += 40;
  ctx.font = "32px SolyaHebrewBold";
  ctx.textAlign = "left";
  ctx.direction = "ltr";
  ctx.fillText(model.totalIncome ? `${model.totalIncome} ₪` : "אין סה״כ", 80, y);
  ctx.textAlign = "right";
  ctx.fillText(model.totalProduction ? `${model.totalProduction} קוט״ש` : "אין סה״כ", width - 80, y);

  ctx.fillStyle = "#8A938C";
  ctx.font = "14px SolyaHebrew";
  ctx.textAlign = "center";
  ctx.direction = "rtl";
  ctx.fillText("נתוני ייצור מ-SolarEdge", width / 2, height - 52);

  return canvas.toBuffer("image/png");
}

function roundRect(
  ctx: {
    beginPath: () => void;
    moveTo: (x: number, y: number) => void;
    arcTo: (x1: number, y1: number, x2: number, y2: number, r: number) => void;
    closePath: () => void;
  },
  x: number,
  y: number,
  w: number,
  h: number,
  r: number,
): void {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}
