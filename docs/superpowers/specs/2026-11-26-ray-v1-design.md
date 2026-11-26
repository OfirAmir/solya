# RAY v1: design

Date: 26 Nov 2026. Status: for approval. A builder check was run; answers marked *(my call)* settle its questions. PR 2 of 2 (PR 1, report parity, is merged as #6).

## Why

RAY ("מדד RAY", Oren's RAY FACTOR) is the most visible part of the evening report Oren gets today from Instinct: a big gauge for the owner and a score per system. Solya has the slot for it (`rules/ray.py`, card circle, "RAY nn" in the text) but no calculator. Oren gave the frozen v1 formula as Instinct implements it. We implement it as written, verify it against Instinct's daily numbers while hidden, then turn it on.

## Goals

- Compute RAY v1 per site every evening after the pull, from stored data, deterministically.
- Show it on the card (gauge + per-site value) and in the text, exactly where the slot already puts it, once an admin switches it on.
- Systems without their own history still get a score, from a national monthly table, flagged low-confidence internally.
- Admins can print every component per site and day to compare with Instinct.

## Non-goals

- Smoothing so that one weak day doesn't lower the score (Oren's guide asks for it; v1 as Instinct runs it doesn't). Ask Oren after the comparison.
- Bill reconciliation ("התאמה לחשבון" component, off in v1), battery components, fault/soiling/shading penalties (off in v1).
- Irradiance sensors, a regional table, learning weights across systems.
- Showing confidence to owners.

## Anti-goals

- Inventing data: missing production, stale data or missing weather never become zeros. If an input is missing, that day's RAY is None (no gauge), not a guess.
- Customer copy that says "efficiency" or "Performance Ratio". kWh per kWp is "תפוקה סגולית" if it is ever shown.

## The formula (v1, frozen, from Oren; component examples 25.11: farm Perf 99.8, Health 89.8, Env 75.2 → 90.2; home 98.7 / 92.0 / 75.2 → 90.5)

```
RAY    = min(0.40·Perf + 0.35·Health + 0.25·Env, Perf + 25)        each component 0–100
Perf   = 0.7·PerfRatio + 0.3·HistSub
  expected  = mean daily production of the same calendar month in the prior 1–2 years (≥ 10 days)
  ratio     = actual ÷ expected × 100
  cf        = 1 − 0.3 × cloud%/100
  PerfRatio = min(ratio ÷ cf, 100)
  HistSub   = weighted mean of min(actual ÷ ref × 100, 100) against:
              last 30 days (weight 2/3), the same days of the month a year ago (1/3), two years ago (1/6)
Health = 100 − StabDeduction
  CV            = std ÷ mean of daily production over the last 30 days
  StabDeduction = min(20, CV ÷ 0.6 × 20)
Env    = 100 − Cloud − Temp − PM
  Cloud = 40 × cloud%/100
  Temp  = min(20, 0.4 × max(0, Tmax − 25))
  PM    = 15 × min(PM10/150, 1)
Overall (owner) = mean of site RAYs weighted by that day's kWh
```

Inputs: daily production from SolarEdge; cloud% = mean of hourly cloud cover 07:00–16:00 local; Tmax = daily max 2 m temperature; PM10 from Open-Meteo at the site's coordinates.

### Interpretations where the formula is silent (our calls; each is tunable during the hidden comparison)

1. **Expected baseline order:** (a) the same calendar month in the prior 1–2 years, using only days with real data, needing ≥ 10 such days; (b) otherwise the national table × kWp (below). The source used is stored.
2. **HistSub references:** "last 30 days" ref = mean of the last 30 days before today with real data (needs ≥ 10). "Same days a year ago" ref = mean of the days from day 1 to today's day-of-month in the same month a year ago (≥ 5 days); same for two years ago. A reference with too few days is dropped and the remaining weights are renormalised. With no reference at all, HistSub = PerfRatio.
2b. **HistSub weights** *(my call)*: the given weights (2/3, 1/3, 1/6) sum to 7/6. A weighted mean divides by the sum of the weights used, so the effective weights are 4/7, 2/7, 1/7 when all three references exist. Instinct's own note lists these weights among its interpretations, so they are tunable like the others: they live in one constant, and the comparison decides.
3. **CV:** population standard deviation of the last 30 days with real data, before today; needs ≥ 10 days, else Health = 100 and confidence is low.
4. **PM10:** mean of hourly PM10 07:00–16:00 local, same window as cloud.
5. **Real data** = a `daily_metrics` row with `production_wh` not null and `comm_state` in (`ok`, `backfill`). Stale or missing days are excluded everywhere (Oren's guide: missing data is never lost production).
6. **Rounding:** components and RAY stored as floats; shown rounded half-up to an integer, as today.
7. **Weather missing** (Open-Meteo unreachable) → that day's RAY is None. No fallback weather.

## National table (fallback baseline)

An editable setting `RAY_NATIONAL_KWH_PER_KWP_DAY`: 12 monthly values of expected kWh per kWp per day. Default values are taken from PVGIS (EU JRC, public) for a fixed-tilt c-Si system in central Israel (lat 32.1, lon 34.8, optimal tilt and azimuth, 14% losses), recorded in the code with source and date. The old software-house spec's table (3.5, 4.5, 6.0, 7.5, 8.0, 7.2, 6.5, 6.5, 7.0, 7.5, 5.0, 3.5) is kept in a comment for comparison; its summer values look wrong. Oren confirms or edits the values.

Low confidence: a site using the table, or with fewer than 30 days of real data, is marked `confidence="low"` in the stored components. Not shown to owners.

## Data

- **History backfill.** Extends the month-to-date backfill (PR 1). For RAY a site needs: the previous calendar month (for the 30-day window early in a month), and the same calendar month one and two years ago. Each is one `/sites/{id}/energy?resolution=DAY` call (1 credit), plus one meter telemetry call where export is needed for income (only for the previous month; history months need production only). Fetched once per site per month, same "tried" memory (a 200 RawResponse) as PR 1. Separate calls per month *(my call)*: worst case 6 credits per site per month including PR 1's 2 (current month energy + meter, previous month energy + meter, same month −1 and −2 years energy). The "tried" memory becomes per requested range: a 200 RawResponse for that endpoint whose `from` parameter starts the same month. Months before the site's installation return empty values and are simply absent.
- **Weather.** New table `weather_daily`: `site_id`, `local_date`, `cloud_pct`, `tmax_c`, `pm10`, `fetched_at`, `source`. Fetched during the **evening** pull only (not midday), after the SolarEdge calls, from:
  - `https://api.open-meteo.com/v1/forecast?latitude=…&longitude=…&hourly=cloud_cover&daily=temperature_2m_max&timezone=<site tz>&past_days=0&forecast_days=1`
  - `https://air-quality-api.open-meteo.com/v1/air-quality?latitude=…&longitude=…&hourly=pm10&timezone=<site tz>&forecast_days=1`
  No API key. No SolarEdge credits. Timeouts and errors are logged by type and leave the row absent.
- **RAY components.** New table `ray_daily`: `site_id`, `local_date`, `ray`, `perf`, `perf_ratio`, `hist_sub`, `health`, `env`, `expected_wh`, `expected_source` (`history`|`table`), `cv`, `cloud_pct`, `tmax_c`, `pm10`, `confidence` (`normal`|`low`), `computed_at`. `daily_metrics.ray` keeps the final score as today.
- Both tables are created by `create_all` on startup; no column changes.

## Computation

- New module `src/solya/rules/ray_v1.py` with class `RayV1` implementing the existing `RayCalculator` protocol (`compute(site, local_date, session) -> float | None`), reading `daily_metrics` and `weather_daily` and writing `ray_daily`. Pure arithmetic lives in small functions (`perf_ratio`, `hist_sub`, `health`, `env`, `combine`) tested directly.
- It runs where the slot already runs (`compute_site_day` after the evening pull). The pull fetches weather before `compute_site_day` is called, so `compute` stays synchronous. Today's production isn't persisted yet when `compute` runs *(my call)*: the protocol gains an optional `actual_wh: float | None = None` argument that `compute_site_day` passes; `NullRay` ignores it.
- `RAY_IMPLEMENTATION=solya.rules.ray_v1:RayV1` is the setting that turns computation on. Deploy sets it.

## Showing it

- New setting `RAY_VISIBLE` (default `false`). While false, RAY is computed and stored but `build_owner_report` passes `ray=None` to the card and text and omits per-site RAY. When true, the card shows the gauge and per-site RAY, and the text shows "RAY nn" per site and the overall.
- Card: the caption under the gauge becomes "מדד RAY" (was "RAY FACTOR"); a per-site "RAY" value appears in each row (the slot exists from PR 1's layout work; add the column if not). Band colours stay (green ≥ 90, orange 80–89, yellow 60–79, red < 60).
- Overall: `weighted_ray` weights by the day's `production_wh` instead of kWp (Oren's text). Sites with RAY None are left out of the overall; if none have RAY, no gauge.
- Text total line gains "RAY nn" when visible: "סה״כ: RAY 91, 396.7 קוט״ש, 177.50 ₪ לפני מע״מ" (as in Instinct's 26.11 report).
- The web landing page already mentions RAY only when it is enabled; it keys off `RAY_VISIBLE` instead.

## Admin comparison

- CLI `solya ray show --site ID [--date YYYY-MM-DD] [--days N]`: prints, per day, RAY and every component, inputs, expected source and confidence.
- CLI `solya ray recompute --site ID --date YYYY-MM-DD`: recomputes from stored data (no SolarEdge calls; fetches weather for that date from Open-Meteo's archive endpoint if missing, `https://archive-api.open-meteo.com/v1/archive` / `historical-forecast-api`), for tuning interpretations.
- `/admin` shows yesterday's RAY per site next to production.

## Rollout

1. Deploy with `RAY_IMPLEMENTATION` set and `RAY_VISIBLE=false`.
2. For a few evenings, compare `solya ray show` with Instinct's numbers (25.11: 90.2 / 90.5 components above; 26.11: 91 / 92). Tune only the interpretations listed above.
3. Set `RAY_VISIBLE=true` and re-run `setup.sh`.

## Tests

- Arithmetic: `combine(99.8, 89.8, 75.2) == 90.15` (→ shown 90); `combine(98.7, 92.0, 75.2) ≈ 90.5`; the cap `Perf + 25` binds when Health and Env are high and Perf low; each component clamps to 0–100.
- PerfRatio with cloud factor; HistSub renormalisation with missing references; CV and the 20-point cap; Env with Tmax ≤ 25 and PM10 above 150.
- Baseline: history used when ≥ 10 real days in the same month a year or two ago; table used otherwise; `expected_source` and `confidence` stored.
- Missing data: stale/None days excluded; weather missing → RAY None; today's production missing → RAY None.
- Weather client: parses both Open-Meteo responses (respx), averages 07:00–16:00 local, errors leave no row.
- Backfill: fetches previous month and same month −1/−2 years once per month; ≤ 6 credits per site per month including PR 1's calls; never overwrites existing rows; a monthly job (1st of the month, 01:00 local) runs it so early-month RAY has history without a restart.
- Visibility: with `RAY_VISIBLE=false` no RAY anywhere in card or text; with true, gauge + per-site + total line; overall weighted by kWh.
- CLI `ray show` output contains every component.

## Left to the builder

- Exact Open-Meteo parameter names and response parsing (verify against their docs; use `timezone=<site tz>` so hours are local).
- The PVGIS values: fetch once from PVGIS `PVcalc` (monthly `E_d` for 1 kWp) at build time if reachable, otherwise use published PVGIS figures for central Israel; record source and date in the code.
- Card geometry for the per-site RAY column.

## Open after the comparison (ask Oren, not blocking)

- Smoothing for single weak days.
- Whether PM10 should stay in Env (the handover notes its effect on production is unverified).
- The national table values.
