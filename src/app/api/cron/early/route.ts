import { NextResponse } from "next/server";
import { cronAuthorized, makeSolarEdgeClient } from "@/lib/app/runtime";
import { loadConfig } from "@/lib/config";
import { runIngest } from "@/lib/ingest/worker";
import { getStore } from "@/lib/store";

export async function GET(request: Request) {
  return run(request);
}

export async function POST(request: Request) {
  return run(request);
}

async function run(request: Request) {
  const config = loadConfig();
  if (!cronAuthorized(request.headers.get("authorization"), config.cronSecret)) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }
  const result = await runIngest({
    store: getStore(),
    client: makeSolarEdgeClient(config),
    config,
    now: new Date(),
    kind: "early",
    messenger: null,
  });
  return NextResponse.json(result);
}
