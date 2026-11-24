import { NextResponse } from "next/server";
import { loadConfig } from "@/lib/config";
import { readCustomerId } from "@/lib/app/session";
import { getStore } from "@/lib/store";
import { authorizeUrl } from "@/lib/solaredge/urls";

export async function GET(request: Request) {
  const customerId = await readCustomerId();
  if (!customerId) return NextResponse.redirect(new URL("/", request.url), 303);
  const config = loadConfig();
  const store = getStore();
  const session = await store.createOauthSession(customerId);
  if (config.solaredge.mode === "fixture") {
    return NextResponse.redirect(new URL(`/api/solaredge/callback?fixture=1&state=${session}`, request.url), 303);
  }
  if (!config.solaredge.clientId || !config.solaredge.clientSecret) {
    return NextResponse.json({ error: "SolarEdge app credentials are not configured." }, { status: 503 });
  }
  const redirectUri = config.solaredge.redirectUri || new URL("/api/solaredge/callback", request.url).toString();
  const url = authorizeUrl({
    authorizeUrl: config.solaredge.authorizeUrl,
    clientId: config.solaredge.clientId,
    redirectUri,
    scopes: config.solaredge.scopes,
    state: session,
    accessDuration: config.solaredge.accessDuration,
  });
  return NextResponse.redirect(url, 303);
}
