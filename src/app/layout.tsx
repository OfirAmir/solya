import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "סוליה",
  description: "סיכום ערב למערכת הסולארית, מתוך הנתונים של המערכת.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="he" dir="rtl">
      <body>
        <div className="wrap">
          <header className="top">
            <div className="brand">
              <div className="sun" aria-hidden="true" />
              <span>סוליה</span>
            </div>
            <a href="/admin">ניהול</a>
          </header>
          {children}
        </div>
      </body>
    </html>
  );
}
