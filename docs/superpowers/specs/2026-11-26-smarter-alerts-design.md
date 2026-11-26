# Smarter alerts: design

Date: 26 Nov 2026. Status: building (owner said "do the rest", option B). Calls marked *(my call)* can be changed.

## Why

Oren's SolarEdge guide asks for fewer false alarms and for Sol to catch slow losses (soiling, a failed string), not only zero production. Solya already treats stale data as lost communication and never turns API errors into faults. Two gaps remain: a single fault sample alerts the owner, and there is no "weak production" alert.

## Goals

1. **Persistence for inverter fault modes.** Alert only when the inverter reported a fault mode in at least `FAULT_MIN_SAMPLES` (default 2) hourly samples of the day's window *(my call)*. SolarEdge's own alerts stay immediate (SolarEdge already filters them). Whole-day "inverter not reporting" and zero production are day-level signals already and stay as they are.
2. **Weak production alert (new, off by default: `WEAK_ALERTS_ENABLED=false`).**
   - Input: `ray_daily.perf_ratio` (actual ÷ expected, weather-adjusted, 0–100), so it requires `RAY_IMPLEMENTATION`.
   - A day **counts** only if: real production data, weather present, `expected_source == "history"` (no alerts from the national table), and `cloud_pct <= WEAK_MAX_CLOUD_PCT` (default 70, the guide's "no alerts in low light") *(my call)*. Days that don't count are skipped, not treated as good or bad.
   - **Open** a `WEAK_PRODUCTION` fault when the last `WEAK_DAYS` (default 3) counted days, looking back at most 10 calendar days, all have `perf_ratio < WEAK_THRESHOLD` (default 70).
   - **Close** it when a counted day has `perf_ratio >= WEAK_RECOVER` (default 85).
   - Checked once per evening, after RAY is computed. Uses the existing fault lifecycle (one record, one alert, one reminder, the closed message).
3. **Copy (Sol, feminine; owner addressed in plural)** following the guide's confidence wording:
   - alert: "{greeting} {name},\nקיים חשד סביר לירידה בייצור{paren}: ב־{n} הימים האחרונים המערכת ייצרה כ־{pct}% מהצפוי ביחס למזג האוויר. סיבות אפשריות: לכלוך על הפאנלים, הצללה או תקלה ברכיב. אני ממשיכה לעקוב ואעדכן אתכם. איך תרצו להתקדם?"
   - closed: "{greeting} {name},\nהייצור{paren} חזר לרמה הצפויה. אמשיך לעקוב."
   - `pct` = rounded mean perf_ratio of the counted days.

## Non-goals

Bills (postponed), irradiance sensors, per-optimizer analysis, learning thresholds.

## Tests

Mode persistence (1 sample → no fault, 2 → fault); weak alert opens after 3 counted low days, skips cloudy/table/missing days, ignores days older than 10, closes on recovery, never fires when disabled or RAY off; copy has no singular forms and no direction marks around commands.
