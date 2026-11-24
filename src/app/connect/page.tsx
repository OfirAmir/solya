import { loadConfig } from "@/lib/config";
import { getStore } from "@/lib/store";
import { readCustomerId } from "@/lib/app/session";
import { redirect } from "next/navigation";

export const dynamic = "force-dynamic";

export default async function ConnectPage() {
  const customerId = await readCustomerId();
  if (!customerId) redirect("/");
  const customer = await getStore().getCustomer(customerId);
  if (!customer) redirect("/");
  const config = loadConfig();
  const liveReady = config.solaredge.clientId && config.solaredge.clientSecret && config.solaredge.redirectUri;
  return (
    <main className="card">
      <h1>תודה {customer.name.split(" ")[0]}.</h1>
      <p>השלב הבא הוא אישור של בעל המערכת ב-SolarEdge. אישור אחד לכל אתר. סוליה שומרת אסימון מוצפן, ולא מבקשת סיסמה לפורטל.</p>
      {config.solaredge.mode === "fixture" ? (
        <p className="warn">מצב הדגמה: החיבור יטען את שני האתרים השמורים, בלי יציאה ל-SolarEdge.</p>
      ) : liveReady ? null : (
        <p className="warn">חסרים SOLAREDGE_CLIENT_ID, SOLAREDGE_CLIENT_SECRET או SOLAREDGE_REDIRECT_URI.</p>
      )}
      {config.solaredge.mode === "fixture" || liveReady ? (
        <p><a className="button" href="/api/connect/solaredge">חיבור ל-SolarEdge</a></p>
      ) : null}
      <p className="note">אחרי האישור בוחרים אילו מערכות לעקוב. נתון ההספק שמופיע בפורטל לא משמש לחיוב.</p>
    </main>
  );
}
