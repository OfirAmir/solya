import { loadConfig } from "@/lib/config";

export const dynamic = "force-dynamic";

export default function HomePage() {
  const config = loadConfig();
  const fixture = config.solaredge.mode === "fixture";
  return (
    <main className="grid" style={{ gap: 22 }}>
      <section className="card">
        <div className="badge">30 יום 100% שביעות רצון מובטחת</div>
        <h1>סול שומרת על המערכת, כל ערב.</h1>
        <p className="lede">
          סוליה מחברת את מערכת SolarEdge שלך ושולחת כרטיס סיכום אחרי השקיעה. הציון RAY FACTOR, הייצור וההכנסה. בלי ניחושים.
        </p>
        <p className="note">לא אהבתם? בטלו וקבלו החזר על החודש הראשון. החזר כספי מלא.</p>
        <p>
          49.97 ₪ לחודש למערכת עד 50 קילוואט מותקן, ועוד 1 ₪ לחודש לכל קילוואט מעל 50.
          {" "}אופן הצגת המע״מ טרם נקבע, וספק הסליקה טרם נבחר.
        </p>
        {fixture ? <p className="warn">הסביבה הזו רצה על נתונים שמורים מ-24 בנובמבר, בלי קריאה ל-SolarEdge.</p> : null}
      </section>
      <section className="card">
        <h2>הרשמה</h2>
        <p className="note">אחר כך עוברים לאישור קריאה בלבד אצל SolarEdge. לא מדביקים מפתח API.</p>
        <form className="grid" action="/api/signup" method="post">
          <label>
            שם
            <input name="name" required autoComplete="name" />
          </label>
          <label>
            אימייל
            <input name="email" type="email" required autoComplete="email" />
          </label>
          <label>
            טלפון
            <input name="phone" type="tel" required autoComplete="tel" />
          </label>
          <button type="submit">המשך לחיבור SolarEdge</button>
        </form>
      </section>
    </main>
  );
}
