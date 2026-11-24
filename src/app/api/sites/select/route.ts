import { NextResponse } from "next/server";
import { loadConfig } from "@/lib/config";
import { readCustomerId } from "@/lib/app/session";
import { createPaymentProvider } from "@/lib/payments/provider";
import { customerMonthlyAgorot } from "@/lib/payments/pricing";
import { getStore } from "@/lib/store";

export async function POST(request: Request) {
  const customerId = await readCustomerId();
  if (!customerId) return NextResponse.redirect(new URL("/", request.url), 303);
  const form = await request.formData();
  const selected = form.getAll("site").map(String);
  const store = getStore();
  const sites = await store.listSites(customerId);
  await store.setMonitor(customerId, sites.map((site) => site.id), false);
  await store.setMonitor(customerId, selected, true);
  const monitored = sites
    .filter((site) => selected.includes(site.id))
    .map((site) => ({ ...site, monitor: true }));
  const price = customerMonthlyAgorot(monitored);
  const config = loadConfig();
  let pay = "";
  if (price != null) {
    const checkout = await createPaymentProvider(config.payments.provider, store, config.publicAppUrl).createCheckout({
      customerId,
      amountAgorot: price,
      description: "מנוי חודשי, ספק טרם נבחר",
    });
    pay = checkout.id;
  }
  const target = new URL("/ready", request.url);
  if (pay) target.searchParams.set("pay", pay);
  return NextResponse.redirect(target, 303);
}
