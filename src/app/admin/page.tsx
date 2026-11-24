import { formatIlsFromAgorot } from "@/lib/format";
import { readAdmin } from "@/lib/app/session";
import { getStore } from "@/lib/store";
import { redirect } from "next/navigation";

export const dynamic = "force-dynamic";

const STATUS_LABELS: Record<string, string> = {
  lead: "נרשם",
  connected: "מחובר",
  pending_payment: "ממתין לתשלום",
  active: "פעיל",
  cancelled: "בוטל",
};

function statusLabel(status: string): string {
  return STATUS_LABELS[status] ?? status;
}

export default async function AdminPage() {
  if (!(await readAdmin())) redirect("/admin/login");
  const rows = await getStore().adminRows();
  return (
    <main className="card">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h1>לקוחות</h1>
        <form action="/api/admin/logout" method="post"><button className="button secondary" type="submit">יציאה</button></form>
      </div>
      <p className="note">האסימונים מוצפנים ולא מוצגים. כאן רואים משיכה אחרונה, קרדיטים, כשל רענון ושימוש במודל.</p>
      <table>
        <thead>
          <tr>
            <th>לקוח</th>
            <th>מערכות</th>
            <th>משיכה אחרונה</th>
            <th>קרדיטים החודש</th>
            <th>טוקנים החודש</th>
            <th>רענון</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.customer.id}>
              <td>
                {row.customer.name}<br />
                <span className="note">{row.customer.email}<br />{row.customer.phone}<br />{statusLabel(row.customer.subscriptionStatus)}</span>
              </td>
              <td>
                {row.sites.map((site) => (
                  <div key={site.id}>{site.cardName} · {site.solaredgeSiteId}{site.monitor ? "" : " · לא במעקב"}</div>
                ))}
              </td>
              <td>{row.lastPullAt ?? "אין"}</td>
              <td>{row.creditsThisMonth}</td>
              <td>{row.llmTokensThisMonth}</td>
              <td>
                {row.failedRefreshes.map((token) => (
                  <div key={token.siteId} className={token.lastRefreshError ? "fail" : undefined}>
                    {token.hasToken ? "יש אסימון" : "אין אסימון"}
                    {token.lastRefreshError ? ` · כשל: ${token.lastRefreshError}` : ""}
                  </div>
                ))}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="note">מחיר לדוגמה לא מוצג כאן כחיוב. {formatIlsFromAgorot(4997)} ₪ הוא בסיס המחירון למערכת עד 50 קילוואט.</p>
    </main>
  );
}
