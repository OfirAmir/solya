import { NextResponse } from "next/server";
import { writeCustomerCookie } from "@/lib/app/session";
import { getStore } from "@/lib/store";

export async function POST(request: Request) {
  const form = await request.formData();
  const name = String(form.get("name") ?? "").trim();
  const email = String(form.get("email") ?? "").trim();
  const phone = String(form.get("phone") ?? "").trim();
  if (name.length < 2 || !email.includes("@") || phone.replace(/\D/g, "").length < 9) {
    return NextResponse.redirect(new URL("/?error=form", request.url), 303);
  }
  const customer = await getStore().createCustomer({ name, email, phone });
  await writeCustomerCookie(customer.id);
  return NextResponse.redirect(new URL("/connect", request.url), 303);
}
