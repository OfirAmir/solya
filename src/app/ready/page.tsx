import { loadConfig } from "@/lib/config";
import { NOV24_SENT_AT, NOV24_TIME_ZONE, nov24Sites } from "@/lib/fixtures/nov24";
import { telegramDeepLink } from "@/lib/messaging/telegram";
import { describePrice, customerMonthlyAgorot } from "@/lib/payments/pricing";
import { buildDailySummary } from "@/lib/rules/engine";
import { summaryText } from "@/lib/rules/hebrew";
import { readCustomerId } from "@/lib/app/session";
import { getStore } from "@/lib/store";
import { redirect } from "next/navigation";

export const dynamic = "force-dynamic";

export default async function ReadyPage({
  searchParams,
}: {
  searchParams: Promise<{ pay?: string }>;
}) {
  const customerId = await readCustomerId();
  if (!customerId) redirect("/");
  const config = loadConfig();
  const store = getStore();
  const customer = await getStore().getCustomer(customerId);
  if (!customer) redirect("/");
  const sites = (await store.listSites(customerId)).filter((site) => site.monitor);
  const price = customerMonthlyAgorot(sites);
  const query = await searchParams;
  const payId = typeof query.pay === "string" ? query.pay : "";
  const found = payId ? await store.getPayment(payId) : null;
  const payments = found && found.customerId === customerId ? found : null;
  const link = telegramDeepLink(config.telegram.botUsername, customer.telegramLinkToken);
  const preview = config.solaredge.mode === "fixture"
    ? summaryText(buildDailySummary(nov24Sites, { includeWeather: config.ray.includeWeather }), {
        customerName: customer.name,
        sentAt: NOV24_SENT_AT,
        timeZone: NOV24_TIME_ZONE,
      })
    : null;
  return (
    <main className="grid">
      <section className="card">
        <h1>המערכות נשמרו.</h1>
        <p>{describePrice(price, config.vat)}</p>
        <p className="note">לא בוצע חיוב. ספק הסליקה טרם נבחר, והעמוד הבא רק מציג את הכוונה.</p>
        {payments ? <p><a className="button secondary" href={`/pay/${payments.id}`}>לעמוד התשלום</a></p> : null}
        <h2>טלגרם</h2>
        {link ? (
          <p><a className="button" href={link}>פתיחת סול בטלגרם</a></p>
        ) : (
          <p>שם הבוט עדיין לא הוגדר. קוד החיבור: <code>{customer.telegramLinkToken}</code></p>
        )}
        <p className="note">אחרי החיבור נשלחים כרטיס ואז טקסט, כחצי שעה אחרי השקיעה המקומית.</p>
      </section>
      {preview ? (
        <section className="card">
          <h2>דוגמה מנתוני 24 בנובמבר</h2>
          <img className="preview" alt="כרטיס הסיכום ל-24 בנובמבר" src="/api/sample-card" />
          <pre style={{ whiteSpace: "pre-wrap", font: "inherit" }}>{preview}</pre>
        </section>
      ) : null}
    </main>
  );
}
