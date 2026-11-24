import { loadConfig } from "@/lib/config";
import { renderCardPng } from "@/lib/card/render";
import { nov24Sites } from "@/lib/fixtures/nov24";
import { buildDailySummary } from "@/lib/rules/engine";

export async function GET() {
  const config = loadConfig();
  if (config.solaredge.mode !== "fixture") {
    return new Response("not found", { status: 404 });
  }
  const png = renderCardPng(buildDailySummary(nov24Sites, { includeWeather: config.ray.includeWeather }));
  return new Response(new Uint8Array(png), {
    headers: { "content-type": "image/png", "cache-control": "no-store" },
  });
}
