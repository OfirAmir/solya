import { formatIlsFromAgorot } from "@/lib/format";
import { getStore } from "@/lib/store";
import { notFound } from "next/navigation";

export const dynamic = "force-dynamic";

export default async function PayPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const payment = await getStore().getPayment(id);
  if (!payment) notFound();
  return (
    <main className="card">
      <h1>תשלום</h1>
      <p>סכום לתצוגה: {formatIlsFromAgorot(payment.amountAgorot)} ₪.</p>
      <p>טרם שולם. ספק הסליקה טרם נבחר, ולא בוצע חיוב ולא הופעל מנוי.</p>
      <p className="note">כשיוחלט על ספק, העמוד הזה יוחלף בקופה מתארחת. הממשק כבר מופרד מהשאר.</p>
    </main>
  );
}
