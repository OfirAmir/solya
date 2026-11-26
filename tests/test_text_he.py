import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from solya.rules.daily import FaultView, SiteDay
from solya.text import he
from solya.text.rtl import RLM, rlm_tokens, rtl_text

TZ = timezone(timedelta(hours=2))
AT_1710 = datetime(2026, 11, 24, 17, 10, tzinfo=TZ)
AT_0700 = datetime(2026, 11, 25, 7, 0, tzinfo=TZ)


def ok_day(label, wh, income, **kw):
    return SiteDay(1, label, label, "ok", production_wh=wh, income_ils=Decimal(income), **kw)


def two_ok():
    return [ok_day("משק", 316990, "142.65"), ok_day("בית", 85039, "38.03")]


def fault(code="FAN_ALERT_1", serial="7E04E44D-B3"):
    return FaultView(code, serial, AT_0700, "alerts")


def test_report_regression_20261124():
    out = he.daily_report("אורן", two_ok(), AT_1710, True)
    assert out == (
        "ערב טוב אורן,\n"
        "משק: אין התראות פעילות. ‏317.0 קוט״ש, הכנסה ‏142.65 ₪\n"
        "בית: אין התראות פעילות. ‏85.0 קוט״ש, הכנסה ‏38.03 ₪\n"
        "סה״כ: ‏402.0 קוט״ש, ‏180.68 ₪ לפני מע״מ\n"
        "לכל שאלה אני כאן. המשך ערב טוב."
    )


def test_report_with_ray():
    days = [ok_day("משק", 316990, "142.65", ray=92.4)]
    out = he.daily_report("אורן", days, AT_1710, False)
    assert f"אין התראות פעילות. {RLM}RAY {RLM}92, {RLM}317.0 קוט״ש" in out
    assert "סה״כ" not in out


def test_report_without_income_shows_kwh_only():
    d = SiteDay(1, "משק", "משק", "ok", production_wh=1000, income_ils=None)
    out = he.daily_report("אורן", [d], AT_1710, False)
    assert "משק: אין התראות פעילות. ‏1.0 קוט״ש\n" in out
    assert "₪" not in out


def test_report_comm_loss_first_and_no_figures():
    since = datetime(2026, 11, 24, 9, 30, tzinfo=TZ)
    bad = SiteDay(2, "בית", "בית", "comm_loss", comm_since=since)
    out = he.daily_report("אורן", [ok_day("משק", 316990, "142.65"), bad], AT_1710, False)
    lines = out.split("\n")
    assert lines[1] == f"בית: אין תקשורת עם המערכת מאז {RLM}09:30. אני עוקבת ואעדכן."
    assert "קוט״ש" not in lines[1]
    assert lines[2].startswith("משק:")


def test_reauth_line():
    bad = SiteDay(2, "בית", "בית", "reauth_needed")
    out = he.daily_report("אורן", [ok_day("משק", 316990, "142.65"), bad], AT_1710, False)
    lines = out.split("\n")
    assert lines[1] == "בית: החיבור ל־SolarEdge פג, שלחתי קישור לחיבור מחדש."
    assert "סה״כ" not in out
    assert "משק: אין התראות פעילות." in out


def test_missing_overview_line():
    d = SiteDay(1, "משק", "משק", "no_data")
    out = he.daily_report("אורן", [d], AT_1710, False)
    lines = out.split("\n")
    assert lines[1] == "משק: המידע עדיין לא הגיע אלי, אני על זה."
    assert "קוט״ש" not in out and "סה״כ" not in out


def test_report_with_fault_line_and_closing():
    d = ok_day("משק", 316990, "142.65", open_faults=[fault()])
    out = he.daily_report("אורן", [d], AT_1710, False)
    lines = out.split("\n")
    assert lines[1] == (
        f"משק: התראה פתוחה {RLM}FAN_ALERT_1 בממיר {RLM}7E04E44D-B3. "
        f"מומלץ לפתוח קריאת שירות מול {RLM}SolarEdge או להזמין טכנאי מוסמך."
    )
    assert lines[2] == f"משק: {RLM}317.0 קוט״ש, הכנסה {RLM}142.65 ₪"
    assert lines[-1] == "עדכן אותי ואעקוב עד לסיום הטיפול."


def test_fault_line_without_serial():
    d = ok_day("משק", 1000, "1", open_faults=[fault(serial=None)])
    out = he.daily_report("אורן", [d], AT_1710, False)
    assert f"התראה פתוחה {RLM}FAN_ALERT_1. " in out


FORBIDDEN = ["רווח", "החווה", "ביחד", "סטטוס"]


def all_variants():
    since = datetime(2026, 11, 24, 9, 30, tzinfo=TZ)
    f = fault()
    outs = [
        he.daily_report("אורן", two_ok(), AT_1710, True),
        he.daily_report("אורן", [ok_day("משק", 1000, "1", ray=90.0)], AT_1710, False),
        he.daily_report(
            "אורן",
            [
                ok_day("משק", 1000, "1", open_faults=[f]),
                SiteDay(2, "בית", "בית", "comm_loss", comm_since=since),
                SiteDay(3, "חצר", "חצר", "no_data"),
                SiteDay(4, "גג", "גג", "reauth_needed"),
            ],
            AT_1710,
            False,
        ),
        he.fault_alert("אורן", None, f, AT_0700, AT_0700),
        he.fault_alert("אורן", "משק", f, AT_0700, AT_0700),
        he.fault_alert("אורן", "משק", fault("ZERO_PRODUCTION", None), AT_0700, AT_0700),
        he.fault_alert("אורן", "משק", fault("INVERTER_NOT_REPORTING"), AT_0700, AT_0700),
        he.fault_alert("אורן", None, fault("WEIRD"), AT_0700, AT_0700),
        he.fault_reminder("אורן", f, AT_0700),
        he.fault_closed("אורן", f, AT_0700),
        he.inverter_recovered("אורן", "7E04E44D-B3", AT_0700),
        he.comm_loss_alert("אורן", "משק", since, AT_1710),
        he.comm_restored("אורן", "משק", AT_1710),
        he.connect_link("https://example.com/c?x=1"),
        he.site_connected("Portal 1"),
        he.site_already_connected("Portal 1"),
        he.reauth_link("אורן", "משק", "https://example.com/r", AT_1710),
        he.WELCOME_1, he.WELCOME_2, he.NOT_INVITED, he.INVITE_OK, he.HELP, he.NO_DATA_YET,
    ]
    return outs


def test_report_never_says_forbidden_words():
    for out in all_variants():
        for w in FORBIDDEN:
            assert w not in out


def test_every_line_starts_hebrew():
    for out in all_variants():
        for line in out.split("\n"):
            if re.fullmatch(r"https?://\S+", line):
                continue
            first = line.lstrip(RLM)[0]
            assert "֐" <= first <= "׿", line


def test_site_connected_portal_name_latin_gets_no_rlm_issue():
    # "Portal 1" follows Hebrew with a space, so it is RLM-prefixed per R2.
    assert he.site_connected("Portal 1") == (
        f"המערכת {RLM}Portal {RLM}1 חוברה בהצלחה. יש לך עוד מערכת? שלחו /connect.")


def test_site_already_connected_exact():
    assert he.site_already_connected("משק") == (
        f"המערכת משק כבר מחוברת. בחלון של {RLM}SolarEdge בחרו מערכת אחרת.")


def test_fault_alert_fan_exact():
    out = he.fault_alert("אורן", None, fault(), AT_0700, AT_0700)
    assert out == (
        "בוקר טוב אורן,\n"
        f"בשעה {RLM}07:00 זיהיתי התראה על תקלה במערכת הסולארית שלך: "
        f"{RLM}FAN_ALERT_1 בממיר {RLM}7E04E44D-B3. "
        "ככל הנראה מדובר בתקלה במאוורר הקירור של הממיר. "
        "נדרשת בדיקה של איש מקצוע מוסמך לאבחון ולטיפול, ולא טיפול עצמי. "
        f"אני ממליצה לפתוח קריאת שירות מול {RLM}SolarEdge או להזמין טכנאי מוסמך. "
        "עדכן אותי ואעקוב עד לסיום הטיפול."
    )


def test_fault_alert_label_shown_for_multi_site():
    out = he.fault_alert("אורן", "משק", fault(), AT_0700, AT_0700)
    assert "הסולארית שלך (משק): " in out


def test_fault_alert_unknown_code_no_cause():
    out = he.fault_alert("אורן", None, fault("WEIRD"), AT_0700, AT_0700)
    assert "ככל הנראה" not in out
    assert f"{RLM}WEIRD בממיר {RLM}7E04E44D-B3. נדרשת בדיקה" in out


def test_fault_alert_zero_production():
    out = he.fault_alert("אורן", "משק", fault("ZERO_PRODUCTION", None), AT_0700, AT_0700)
    assert out.split("\n")[1].startswith(
        "היום המערכת (משק) לא ייצרה חשמל למרות שהנתונים מתעדכנים. נדרשת בדיקה"
    )


def test_fault_alert_inverter_not_reporting_uses_detected_time():
    det = datetime(2026, 11, 25, 6, 45, tzinfo=TZ)
    out = he.fault_alert("אורן", None, fault("INVERTER_NOT_REPORTING"), AT_0700, det)
    assert out.split("\n")[1].startswith(
        f"בשעה {RLM}06:45 זיהיתי שהממיר {RLM}7E04E44D-B3 במערכת לא מדווח נתונים. נדרשת בדיקה"
    )


def test_reminder_and_closed_exact():
    assert he.fault_reminder("אורן", fault(), AT_0700) == (
        "בוקר טוב אורן,\n"
        f"תזכורת, ההתראה {RLM}FAN_ALERT_1 בממיר {RLM}7E04E44D-B3 עדיין פתוחה. "
        f"אני ממליצה לפתוח קריאת שירות מול {RLM}SolarEdge או להזמין טכנאי מוסמך. "
        "עדכן אותי ואעקוב עד לסיום הטיפול."
    )
    assert he.fault_closed("אורן", fault(), AT_0700) == (
        "בוקר טוב אורן,\n"
        f"לפי {RLM}SolarEdge ההתראה {RLM}FAN_ALERT_1 בממיר {RLM}7E04E44D-B3 נסגרה. "
        "סגירה של ההתראה עדיין לא מעידה שהתקלה תוקנה. "
        "האם הטיפול בוצע? מחכה לעדכון שלך."
    )


def test_inverter_recovered_copy_r24():
    """R24: an internal (non-alert) fault closes without claiming SolarEdge closed it."""
    out = he.inverter_recovered("אורן", "7E04E44D-B3", AT_0700)
    assert out == (
        "בוקר טוב אורן,\n"
        f"לפי הנתונים מ־SolarEdge הממיר {RLM}7E04E44D-B3 חזר לפעול כרגיל. "
        "האם בוצע טיפול? מחכה לעדכון שלך."
    )
    assert "ההתראה" not in out and "נסגרה" not in out


def test_connect_link_url_line_untouched():
    out = he.connect_link("https://example.com/c?x=1")
    lines = out.split("\n")
    assert lines[1] == "https://example.com/c?x=1"
    assert lines[2] == "הקישור בתוקף ל־15 דקות."


def test_comm_alerts():
    since = datetime(2026, 11, 24, 9, 30, tzinfo=TZ)
    assert he.comm_loss_alert("אורן", "משק", since, AT_1710).split("\n")[1] == (
        f"משק: אין תקשורת עם המערכת מאז {RLM}09:30. בשלב זה אין לי נתוני ייצור עדכניים. "
        "אני עוקבת ואעדכן כשהתקשורת תחזור."
    )
    assert he.comm_restored("אורן", "משק", AT_1710).endswith("משק: התקשורת עם המערכת חזרה.")


@pytest.mark.parametrize(
    "hour,minute,word",
    [
        (4, 59, "לילה טוב"), (5, 0, "בוקר טוב"), (11, 59, "בוקר טוב"),
        (12, 0, "צהריים טובים"), (16, 59, "צהריים טובים"),
        (17, 0, "ערב טוב"), (21, 59, "ערב טוב"), (22, 0, "לילה טוב"), (0, 0, "לילה טוב"),
    ],
)
def test_greeting_bands(hour, minute, word):
    assert he.greeting(datetime(2026, 11, 25, hour, minute, tzinfo=TZ)) == word


def test_formatters():
    assert he.fmt_kwh(316990) == "317.0 קוט״ש"
    assert he.fmt_ils(Decimal("38.3")) == "38.30 ₪"


def test_rlm_idempotent():
    once = rtl_text("שלום SolarEdge 24/7 ו־RAY 92\nhello world abc")
    assert once == f"שלום {RLM}SolarEdge {RLM}24/7 ו־RAY {RLM}92\nhello world abc"
    assert rtl_text(once) == once
    assert rlm_tokens(once.split("\n")[0]) == once.split("\n")[0]


def test_rlm_not_before_leading_latin_or_without_hebrew():
    assert rlm_tokens("abc def") == "abc def"
    assert rlm_tokens("https://x.y/z?a=1") == "https://x.y/z?a=1"


def _plain(s):
    return s.replace(RLM, "")


def test_daily_report_income_labels():
    farm = ok_day("משק", 305900, "137.64")
    home = ok_day("בית", 82600, "35.98", income_basis="export")
    out = _plain(he.daily_report("אורן", [farm, home], AT_1710, True))
    assert "משק: אין התראות פעילות. 305.9 קוט״ש, הכנסה 137.64 ₪" in out
    assert "בית: אין התראות פעילות. 82.6 קוט״ש, הכנסה מהזרמה 35.98 ₪" in out


def test_total_line_before_vat():
    farm = ok_day("משק", 305900, "137.64")
    home = ok_day("בית", 82600, "35.98", income_basis="export")
    out = _plain(he.daily_report("אורן", [farm, home], AT_1710, True))
    assert "סה״כ: 388.5 קוט״ש, 173.62 ₪ לפני מע״מ" in out


def test_no_tariff_kwh_only():
    d = SiteDay(1, "משק", "משק", "ok", production_wh=1000, income_ils=None)
    out = he.daily_report("אורן", [d], AT_1710, False)
    assert "הכנסה" not in out


def test_hebrew_date():
    from datetime import date
    assert he.hebrew_date(date(2026, 11, 25)) == "רביעי, 25 בנובמבר"
    assert he.hebrew_date(date(2026, 1, 4)) == "ראשון, 4 בינואר"


def test_total_line_mixed_tariffs_kwh_only():
    """Final fix 4 (spec section 1): with a site lacking income, the total shows kWh only."""
    farm = ok_day("משק", 305900, "137.64")
    home = SiteDay(2, "בית", "בית", "ok", production_wh=82600, income_ils=None)
    out = _plain(he.daily_report("אורן", [farm, home], AT_1710, True))
    total = [line for line in out.split("\n") if line.startswith("סה״כ")]
    assert total == ["סה״כ: 388.5 קוט״ש"]
    assert "משק: אין התראות פעילות. 305.9 קוט״ש, הכנסה 137.64 ₪" in out
