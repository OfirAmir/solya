"""Hebrew message templates (Sol speaks feminine Hebrew). Plain functions, no Jinja."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from solya.rules.daily import FaultView, SiteDay
from solya.text.rtl import rtl_text

WELCOME_1 = "ברוך הבא לסוליה ☀️ אני סול, הנציגה שלך. אני שומרת על המערכת הסולארית שלך וכאן בשבילך 24/7."
WELCOME_2 = "כל ערב, אחרי השקיעה, אשלח לך סיכום קצר של הייצור וההכנסה. אם אזהה תקלה, אעדכן אותך."
NOT_INVITED = "הפיילוט של סוליה פתוח כרגע בהזמנה בלבד."
INVITE_OK = "הצטרפת לפיילוט של סוליה. כדי לחבר מערכת שלחו /connect"
LINKED_OK = "החשבון חובר לטלגרם."
HELP = (
    "אני יכולה לענות על: היום, אתמול, החודש, התראות.\n"
    "לחיבור מערכת נוספת שלחו /connect\n"
    "לשינוי שם, תעריף או מעקב שלחו /settings"
)


def settings_link(url: str) -> str:
    return f"כדי לשנות שם, תעריף או מעקב, פתחו את הקישור (בתוקף ל־15 דקות):\n{url}"

NO_DATA_YET = "המידע עדיין לא הגיע אלי, אני על זה."

CAUSES = {"FAN_ALERT_1": "ככל הנראה מדובר בתקלה במאוורר הקירור של הממיר. "}

_ADVICE = "אני ממליצה לפתוח קריאת שירות מול SolarEdge או להזמין טכנאי מוסמך."
_FOLLOW_UP = "עדכן אותי ואעקוב עד לסיום הטיפול."
_PROFESSIONAL = "נדרשת בדיקה של איש מקצוע מוסמך לאבחון ולטיפול, ולא טיפול עצמי."


def greeting(local_dt: datetime) -> str:
    h = local_dt.hour
    if 5 <= h < 12:
        return "בוקר טוב"
    if 12 <= h < 17:
        return "צהריים טובים"
    if 17 <= h < 22:
        return "ערב טוב"
    return "לילה טוב"


def _hhmm(dt: datetime) -> str:
    return f"{dt:%H:%M}"


def fmt_kwh(wh: float) -> str:
    return f"{wh / 1000:.1f} קוט״ש"


def fmt_ils(d: Decimal) -> str:
    return f"{d:.2f} ₪"


def _in_serial(serial: str | None) -> str:
    return f" בממיר {serial}" if serial else ""


TARIFF_HINT = "כדי לראות הכנסה, שלחו /settings והזינו תעריף."

_WEEKDAYS = ["שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת", "ראשון"]  # Monday first
_MONTHS = [
    "ינואר", "פברואר", "מרץ", "אפריל", "מאי", "יוני",
    "יולי", "אוגוסט", "ספטמבר", "אוקטובר", "נובמבר", "דצמבר",
]


def hebrew_date(d: date) -> str:
    return f"{_WEEKDAYS[d.weekday()]}, {d.day} ב{_MONTHS[d.month - 1]}"


def income_phrase(d: SiteDay) -> str:
    if d.income_ils is None:
        return ""
    label = "הכנסה מהזרמה" if d.income_basis == "export" else "הכנסה"
    return f"{label} {fmt_ils(d.income_ils)}"


def _figures(d: SiteDay) -> str:
    phrase = income_phrase(d)
    kwh = fmt_kwh(d.production_wh)
    return f"{kwh}, {phrase}" if phrase else kwh


def daily_report(name: str, days: list[SiteDay], local_dt: datetime, show_total: bool) -> str:
    lines = [f"{greeting(local_dt)} {name},"]
    any_fault = False

    # Exceptions first: every open fault (site order), then non-ok states (site order).
    for d in days:
        for f in d.open_faults:
            any_fault = True
            lines.append(
                f"{d.label}: התראה פתוחה {f.code}{_in_serial(f.serial)}. "
                "מומלץ לפתוח קריאת שירות מול SolarEdge או להזמין טכנאי מוסמך."
            )
    for d in days:
        if d.state == "comm_loss":
            since = _hhmm(d.comm_since) if d.comm_since else ""
            lines.append(f"{d.label}: אין תקשורת עם המערכת מאז {since}. אני עוקבת ואעדכן.")
        elif d.state == "no_data":
            lines.append(f"{d.label}: {NO_DATA_YET}")
        elif d.state == "reauth_needed":
            lines.append(f"{d.label}: החיבור ל־SolarEdge פג, שלחתי קישור לחיבור מחדש.")

    for d in days:
        if d.state != "ok" or d.production_wh is None:
            continue
        ray = f"RAY {int(round(d.ray))}, " if d.ray is not None else ""
        prefix = "" if d.open_faults else "אין התראות פעילות. "
        lines.append(f"{d.label}: {prefix}{ray}{_figures(d)}")

    if show_total:
        shown = [d for d in days if d.state == "ok" and d.production_wh is not None]
        total_wh = sum(d.production_wh for d in shown)
        incomes = [d.income_ils for d in shown if d.income_ils is not None]
        total = fmt_kwh(total_wh)
        if incomes:
            total += f", {fmt_ils(sum(incomes, Decimal('0')))} לפני מע״מ"
        lines.append(f"סה״כ: {total}")

    lines.append(_FOLLOW_UP if any_fault else "לכל שאלה אני כאן. המשך ערב טוב.")
    return rtl_text("\n".join(lines))


def fault_alert(
    name: str,
    label: str | None,
    f: FaultView,
    local_dt: datetime,
    detected_local: datetime,
) -> str:
    paren = f" ({label})" if label else ""
    tail = f"{_PROFESSIONAL} {_ADVICE} {_FOLLOW_UP}"
    if f.code == "ZERO_PRODUCTION":
        body = f"היום המערכת{paren} לא ייצרה חשמל למרות שהנתונים מתעדכנים. {tail}"
    elif f.code == "INVERTER_NOT_REPORTING":
        body = (
            f"בשעה {_hhmm(detected_local)} זיהיתי שהממיר {f.serial} "
            f"במערכת{paren} לא מדווח נתונים. {tail}"
        )
    else:
        cause = CAUSES.get(f.code, "")
        body = (
            f"בשעה {_hhmm(detected_local)} זיהיתי התראה על תקלה במערכת הסולארית שלך{paren}: "
            f"{f.code}{_in_serial(f.serial)}. {cause}{tail}"
        )
    return rtl_text(f"{greeting(local_dt)} {name},\n{body}")


def fault_reminder(name: str, f: FaultView, local_dt: datetime) -> str:
    return rtl_text(
        f"{greeting(local_dt)} {name},\n"
        f"תזכורת, ההתראה {f.code}{_in_serial(f.serial)} עדיין פתוחה. {_ADVICE} {_FOLLOW_UP}"
    )


def fault_closed(name: str, f: FaultView, local_dt: datetime) -> str:
    return rtl_text(
        f"{greeting(local_dt)} {name},\n"
        f"לפי SolarEdge ההתראה {f.code}{_in_serial(f.serial)} נסגרה. "
        "סגירה של ההתראה עדיין לא מעידה שהתקלה תוקנה. "
        "האם הטיפול בוצע? מחכה לעדכון שלך."
    )


def inverter_recovered(name: str, serial: str, local_dt: datetime) -> str:
    """R24: closure of an internally detected fault (inverter mode, not reporting).
    Builder copy, flagged for Oren."""
    return rtl_text(
        f"{greeting(local_dt)} {name},\n"
        f"לפי הנתונים מ־SolarEdge הממיר {serial} חזר לפעול כרגיל. "
        "האם בוצע טיפול? מחכה לעדכון שלך."
    )


def comm_loss_alert(name: str, label: str, since_local: datetime, local_dt: datetime) -> str:
    return rtl_text(
        f"{greeting(local_dt)} {name},\n"
        f"{label}: אין תקשורת עם המערכת מאז {_hhmm(since_local)}. "
        "בשלב זה אין לי נתוני ייצור עדכניים. אני עוקבת ואעדכן כשהתקשורת תחזור."
    )


def comm_restored(name: str, label: str, local_dt: datetime) -> str:
    return rtl_text(f"{greeting(local_dt)} {name},\n{label}: התקשורת עם המערכת חזרה.")


def connect_link(url: str) -> str:
    return rtl_text(
        "כדי לחבר מערכת SolarEdge, פתחו את הקישור ואשרו גישה עם חשבון הבעלים של המערכת:\n"
        f"{url}\nהקישור בתוקף ל־15 דקות."
    )


CHAT_BOUND_ELSEWHERE = "הצ'אט הזה כבר מחובר לחשבון אחר"


def sites_monitored(n: int) -> str:
    return f"המערכות שלך במעקב: {n}"


def site_connected(name: str) -> str:
    return rtl_text(f"המערכת {name} חוברה בהצלחה. יש לך עוד מערכת? שלחו /connect.")


def site_already_connected(name: str) -> str:
    return rtl_text(f"המערכת {name} כבר מחוברת. בחלון של SolarEdge בחרו מערכת אחרת.")


def reauth_link(name: str, label: str, url: str, local_dt: datetime) -> str:
    return rtl_text(
        f"{greeting(local_dt)} {name},\n"
        f"החיבור ל־SolarEdge עבור {label} פג. כדי שאמשיך לעקוב, פתחו את הקישור ואשרו מחדש:\n{url}"
    )

AI_BUDGET_USED = "התקציב החודשי לתשובות פתוחות נוצל. אני עדיין שולחת את הסיכום וההתראות."
