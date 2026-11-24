import { describePrice, customerMonthlyAgorot } from "@/lib/payments/pricing";
import { loadConfig } from "@/lib/config";
import { readCustomerId } from "@/lib/app/session";
import { getStore } from "@/lib/store";
import { redirect } from "next/navigation";

export const dynamic = "force-dynamic";

export default async function SitesPage() {
  const customerId = await readCustomerId();
  if (!customerId) redirect("/");
  const store = getStore();
  const sites = await store.listSites(customerId);
  const config = loadConfig();
  if (sites.length === 0) {
    return (
      <main className="card">
        <h1>עדיין אין אתר מחובר.</h1>
        <p><a className="button" href="/connect">חזרה לחיבור</a></p>
      </main>
    );
  }
  const price = customerMonthlyAgorot(sites.map((site) => ({ ...site, monitor: true })));
  return (
    <main className="card">
      <h1>אילו מערכות לעקוב?</h1>
      <form action="/api/sites/select" method="post" className="grid">
        {sites.map((site) => (
          <label className="site" key={site.id}>
            <input type="checkbox" name="site" value={site.id} defaultChecked={site.monitor} />
            <span>
              <strong>{site.cardName}</strong>
              <span className="note"> מזהה {site.solaredgeSiteId}</span>
              {site.portalPeakKw != null ? (
                <span className="note"> · הספק בפורטל {site.portalPeakKw} קילוואט, לא לחיוב</span>
              ) : null}
              {site.installedKwDc != null ? (
                <span className="note"> · מותקן שמור {site.installedKwDc} קילוואט</span>
              ) : (
                <span className="warn"> · קילוואט מותקן עדיין לא שמור, אז אין מחיר סופי</span>
              )}
            </span>
          </label>
        ))}
        <p>{describePrice(price, config.vat)}</p>
        <button type="submit">שמירה והמשך</button>
      </form>
    </main>
  );
}
