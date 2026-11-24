import { cookies } from "next/headers";
import { loadConfig } from "../config";
import { signValue, verifyValue } from "../crypto";

export const CUSTOMER_COOKIE = "solya_customer";
export const ADMIN_COOKIE = "solya_admin";

export async function readCustomerId(): Promise<string | null> {
  const config = loadConfig();
  if (!config.appSecret) return null;
  const jar = await cookies();
  const raw = jar.get(CUSTOMER_COOKIE)?.value;
  if (!raw) return null;
  return verifyValue(raw, config.appSecret);
}

export async function writeCustomerCookie(customerId: string): Promise<void> {
  const config = loadConfig();
  if (!config.appSecret) throw new Error("APP_SECRET is required.");
  const jar = await cookies();
  jar.set(CUSTOMER_COOKIE, signValue(customerId, config.appSecret), {
    httpOnly: true,
    sameSite: "lax",
    path: "/",
    secure: process.env.NODE_ENV === "production",
    maxAge: 60 * 60 * 24 * 30,
  });
}

export async function readAdmin(): Promise<boolean> {
  const config = loadConfig();
  if (!config.admin.sessionSecret) return false;
  const jar = await cookies();
  const raw = jar.get(ADMIN_COOKIE)?.value;
  if (!raw) return false;
  return verifyValue(raw, config.admin.sessionSecret) === "admin";
}

export async function writeAdminCookie(): Promise<void> {
  const config = loadConfig();
  const jar = await cookies();
  jar.set(ADMIN_COOKIE, signValue("admin", config.admin.sessionSecret), {
    httpOnly: true,
    sameSite: "lax",
    path: "/",
    secure: process.env.NODE_ENV === "production",
    maxAge: 60 * 60 * 12,
  });
}

export async function clearAdminCookie(): Promise<void> {
  const jar = await cookies();
  jar.set(ADMIN_COOKIE, "", { httpOnly: true, path: "/", maxAge: 0 });
}
