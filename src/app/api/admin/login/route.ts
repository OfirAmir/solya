import { timingSafeEqual } from "node:crypto";
import { NextResponse } from "next/server";
import { writeAdminCookie } from "@/lib/app/session";
import { loadConfig } from "@/lib/config";

export async function POST(request: Request) {
  const config = loadConfig();
  if (!config.admin.password || !config.admin.sessionSecret) {
    return NextResponse.json({ error: "admin password is not configured" }, { status: 503 });
  }
  const form = await request.formData();
  const password = String(form.get("password") ?? "");
  const a = Buffer.from(password);
  const b = Buffer.from(config.admin.password);
  const ok = a.length === b.length && timingSafeEqual(a, b);
  if (!ok) return NextResponse.redirect(new URL("/admin/login", request.url), 303);
  await writeAdminCookie();
  return NextResponse.redirect(new URL("/admin", request.url), 303);
}
