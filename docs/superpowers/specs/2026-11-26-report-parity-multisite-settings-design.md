# Report parity, several systems, owner settings: design

Date: 26 Nov 2026. Status: for approval. A builder check was run; answers marked *(my call)* settle its questions. This is PR 1 of 2. PR 2 is RAY v1 (outline at the end).

## Why

Oren is live on the pilot. His farm (site 3033498) connected through the web sign-up and his Telegram is linked. He compares Solya with the evening report he gets today from Instinct, his own tool. Four things stand between us and that report:

1. **The report.** Instinct labels income ("הכנסה" vs "הכנסה מהזרמה"), says it is "לפני מע״מ", shows an alert-status line per site on the card, and sends the text as one message with the image. We show bare "₪", no VAT note, no status on the card, and two messages.
2. **Several systems.** Oren asked to choose more than one system. SolarEdge V2 grants one site per approval (one `site_id` on the callback). Our picker allows several, but nothing guides the owner to approve the second one, so it is easy to stop after one.
3. **No admin step per system.** Today labels, tariff and income basis are set by an admin running `solya sites set`. The admin will not do this per system: it must be automatic. The SolarEdge API has no tariff (checked in both V2 specs and in the live `/sites/3033498` response: name, size, dates, location, status, `note` only). So "automatic" means: the owner gives it once, during sign-up or later from a link Sol sends. No admin involvement.
4. **History.** Questions like "החודש" only cover days since joining. One SolarEdge call returns the month so far.

## Goals

- The evening report carries everything Instinct's report carries except RAY (PR 2).
- An owner with several systems is clearly guided to connect each one.
- A new system needs nothing from an admin. Owner input once is acceptable; admin input is not.
- "החודש" and "אתמול" work from the first day.

## Non-goals

- RAY (PR 2).
- Reading the tariff from an electricity bill, and the bill-vs-production check Oren mentioned. Later.
- A logo header on the card, or matching Instinct's look. The user said the look does not have to match.
- Real payments. The payment page stays a stub.
- Telegram chat flows for settings. Settings are edited on a web page.

## Anti-goals

- An admin having to touch every new site.
- Blocking sign-up on a tariff the owner does not know.
- Showing income figures that look wrong (82.6 kWh next to 35.98 ₪ with no "מהזרמה" label).

## Constraints

- Hebrew, RTL, Sol speaks in the feminine. Text copy stays short; the spec §5.6 "no extra sentences" rule is relaxed only for the lines listed here, which match Instinct's report.
- SolarEdge credits: free tier 2,000 a month, Starter 20,000 until 17 Dec. Spacing of 7.5 s between calls stays.
- Existing security model stays: signed session cookies, CSRF on every POST, single-use tokens with TTL, read-only API access, secrets never logged.
- All 458 tests keep passing. SQLite schema changes go through `src/solya/migrate.py`.

## Approaches considered

A. **Two PRs (picked by the user).** This PR: report, several systems, owner settings, month backfill. PR 2: RAY v1. Oren gets ₪ figures and both systems quickly; RAY follows once verified.
B. One big PR. Nothing ships until RAY is done and checked.
C. RAY first. Most visible, but leaves the card without ₪ and the second system clumsy.

For editing settings later: **a web settings page opened from a single-use link Sol sends** (picked, my call, user agreed). A Telegram chat flow was rejected: typing a tariff and choosing a payment basis is easier in a form, and the page reuses the sign-up form.

## Design

### 1. Owner settings per site

Fields per site, shown on the sign-up picker (`/sites`) and on the new settings page:

| Field | Stored in | Default | Validation |
| --- | --- | --- | --- |
| שם (name) | `label_he` and `card_name` | portal name, shortened: text before " - " if present, trimmed to 30 chars | 1–30 chars, no control characters |
| איך מקבלים תשלום | `income_basis` | `production` if `commercial`, else `export` | `production` ("על כל הייצור") or `export` ("רק על מה שמוזרם לרשת") |
| תעריף לפני מע״מ (₪ לקוט״ש) | `tariff_ils_per_kwh` | empty | empty, or a decimal 0.05–3.00 with up to 4 decimals |
| מעקב | `monitored` | ticked | at least one site ticked |

- On the sign-up picker the tariff is **optional**: "לא יודע/ת" leaves it empty. The owner can continue.
- Saving a tariff or basis **recomputes `income_ils`** for the site's stored `daily_metrics` rows of the current calendar month, so "החודש" stays consistent. Older rows are untouched.
- Name defaulting happens when the site is first registered (`onboarding.register_site`), only if `card_name` is empty. The default fills **`card_name` only**; `label_he` stays empty until the owner saves a name. *(My call.)* So a one-system owner still reads "המערכת" in the text, and existing tests keep their meaning. For multi-site owners, `effective_label` falls back to `card_name` before `portal_name`.
- The "שם" field shows `label_he`, else `card_name`, else the shortened portal name. Saving it writes **both** `label_he` and `card_name`. The owner's save wins over an earlier admin value. *(My call: the owner is the authority on their own system's name.)* Defaults never overwrite anything.
- With mixed tariffs (some sites priced, some not), the total shows kWh only, on the card and in the text, as today. The ₪ total and "לפני מע״מ" appear only when every site in the report has income. *(My call: a partial ₪ sum next to a full kWh sum looks wrong.)* Per-site ₪ still shows for the priced sites.
- `solya sites set` stays as an admin override.
- **Sites that already exist at deploy (Oren's farm):** *(my call)*
  - Name default: applied once at startup to sites whose `card_name` is empty.
  - Income basis: left as is. The current value can't be told apart from the column default, so we don't guess. The owner confirms it on the settings page when entering the tariff; the page shows the current value.
  - Month backfill: runs for any site with missing days in the current month, both after register and once at startup (see §5). So it self-heals for existing sites without an admin step.

### 2. Settings page after Telegram is linked

- New bot command `/settings` ("הגדרות"). Sol replies with a link `https://<host>/settings/<token>`, valid 15 minutes, single use. The token is stored hashed in a new table `settings_links` (`token_hash`, `owner_id`, `created_at`, `used_at`), following the `ConnectRequest` pattern.
- Opening the link (GET) shows a confirm page with one button "פתיחת ההגדרות". Only the CSRF-checked POST marks the token used and starts the web session, then redirects to `/settings`. *(My call: same pattern as `/join/<invite>`. No sign-in on GET, so Telegram's link preview can't use up the token and a shared link can't sign someone in silently.)* A session started this way lasts 24 hours, not the sign-up's 30 days. *(My call.)*
- `/settings` shows every site of the owner with the fields above, the price per site and the monthly total, plus "חיבור מערכת נוספת" (goes to `/connect-site`).
- Saving changes to `monitored` that change the total: admins get a notice in `ADMIN_CHAT_IDS` with the owner, the change and the new monthly total. No billing action (payments are a stub).
- `CHANGES_VIA_BOT` on the locked `/sites` page becomes "לשינויים שלחו לסול בטלגרם /settings", which is now true.
- `HELP` lists `/connect` and `/settings`.
- When an evening report has a site with no tariff, the text ends with "כדי לראות הכנסה, שלחו /settings והזינו תעריף." at most once every 7 days per owner (tracked by the outbox dedupe key `tariff_hint:<owner>:<iso week>`).

### 3. Several systems

- `connect.html`: fix the misleading footnote ("אחרי האישור בוחרים אילו מערכות לעקוב"). New copy: "כל מערכת מאשרים בנפרד ב-SolarEdge. אם יש לך כמה מערכות, אחרי הראשונה נחזור לכאן ותוכל/י לחבר את הבאה." List already-connected systems by name.
- After a successful callback, `/sites` shows a banner: "המערכת {name} חוברה. יש לך עוד מערכת?" with a primary button "חיבור מערכת נוספת" (POST `/connect-site` with CSRF, straight to SolarEdge) and a secondary button "סיימתי, המשך". The hint under the primary button: "בחלון של SolarEdge בחרו את המערכת שעוד לא חוברה."
- Re-approving a site the owner already has: the callback detects `previous_owner == requesting owner` and the banner says "המערכת {name} כבר מחוברת. בחלון של SolarEdge בחרו מערכת אחרת." In Telegram, Sol sends the same sentence.
- **Where the owner lands after a callback** *(my call)*:
  - Web sign-up not yet linked to Telegram: `/sites` with the banner, as above.
  - Linked owner with a live web session (came from `/settings`): `/settings` with the same banner.
  - Linked owner without a web session (came from the Telegram `/connect` link): the plain "אפשר לחזור לטלגרם" page, and the banner text arrives in Telegram from Sol instead.
- `/price` lists the price per site above the total.
- In Telegram, after each connect: "המערכת {name} חוברה בהצלחה. יש לך עוד מערכת? שלחו /connect."

### 3a. Bot robustness (found live on 26 Nov)

Oren sent "/connect" and "את יכול לחבר לי גם את המערכת השניה שלי?" and got the help text both times. Cause, almost certainly: the "/connect" he sent carried invisible direction marks (U+200E) copied from a Hebrew instruction message, so Telegram's command entity did not start at offset 0 and the message fell through to the free-text handler. Fixes:
- The free-text handler strips bidi and zero-width marks (U+200E, U+200F, U+202A–U+202E, U+2066–U+2069, U+200B–U+200D, U+FEFF) and, if what remains is `/<command>` (optionally `@<bot>`), dispatches to that command.
- Free-text routing gains a `connect` route: "חיבור", "לחבר", "תחבר", "מערכת נוספת", "עוד מערכת", "מערכת שניה/שנייה" → the connect link. And a `settings` route: "הגדרות", "תעריף" → the settings link.
- On startup the bot registers its command menu with Telegram (`set_my_commands`): connect, settings, today, yesterday, month, alerts, help, with Hebrew descriptions, so owners tap them from "Menu" instead of typing.
- Copy never puts direction marks inside a command. Hebrew sentences that mention a command place it at the end of the sentence without marks.

### 4. Evening report

Text (`text/he.py daily_report`):
- Per site: "{label}: אין התראות פעילות. {kWh}, הכנסה {₪}" or "…, הכנסה מהזרמה {₪}" by `income_basis`. With no tariff: kWh only, no income word.
- Total: "סה״כ: {kWh}, {₪} לפני מע״מ" when any income is shown.
- Faults and non-ok states keep their current wording.

Card (`render/card.py`):
- Per row: a status chip under the name. Green "אין התראות פעילות" for `ok` with no open faults; red "התראה פתוחה: {code}" when there are open faults (first code, "+N" if more). Non-ok states keep their existing notes and get no chip.
- Per row: the income label above the ₪ value: "הכנסה" or "הכנסה מהזרמה".
- Top line: Hebrew date "רביעי, 25 בנובמבר" and "נכון ל־HH:MM", where HH:MM is the earliest `last_update_time` among the report's sites in local time. Then "N מערכות" when N > 1.
- Footnote "הכנסה משוערת לפני מע״מ" when any income is shown.
- RAY drawing stays as is (hidden while `RAY_IMPLEMENTATION` is off).

Delivery (`messaging/telegram.py`): when there is an image and the text is at most 1024 characters, send one photo with the text as its caption. Otherwise photo then text, as today. One outbox entry either way.

### 5. Month-to-date backfill

- New client method `get_energy_daily(site_id, from, to)` for `GET /sites/{id}/energy?resolution=DAY` (1 credit), and `get_meter_export_daily(site_id, from, to)` for `GET /sites/{id}/meters/telemetry?resolution=DAY` (1 credit, export energy per day; 404 means no meter).
- After a site is registered, and once at every startup for every active site with missing days this month, a background job fetches the 1st of the current month to yesterday (local) and writes `daily_metrics` rows for days that have none: `production_wh`, `export_wh` (null if no meter), `income_ils` computed from the site's tariff if set, `comm_state="backfill"`, `as_of` = fetch time.
- On the 1st of a month the window is empty and the job does nothing. A site whose missing days were already tried this month is not retried at every restart (remember the attempt per site and month). So the cost is at most 2 credits per site per month.
- Uses priority `"background"` so it yields to essential calls and respects the budget.
- Answers treat `comm_state="backfill"` rows as normal data.

### Data and migrations

- New table `settings_links`.
- No new columns on `sites` or `daily_metrics`.

## Tests (new or changed)

- Settings: defaults on register (portal name shortened, basis by `commercial`); validation bounds; optional tariff; recompute of current-month income on save; admin values not overwritten.
- `/settings/<token>`: single use, 15-minute TTL, wrong token 404, starts a session, CSRF required on save; monitoring change notifies admins.
- Several systems: two callbacks in one web session give two sites and the banner; re-approving the same site shows "כבר מחוברת"; `/price` lists per-site prices.
- Report text: production basis says "הכנסה", export basis says "הכנסה מהזרמה", total ends "לפני מע״מ"; no tariff gives kWh only and the weekly hint once per ISO week.
- Card: renders chips, labels, Hebrew date, "נכון ל־", count and footnote (Raqm check passes); a golden-ish test on layout bounds, not pixels.
- Telegram: caption path for ≤1024 chars, two-message path above.
- Backfill: fills only missing days; skips on the 1st; no meter gives null export and null export-basis income; credits recorded.
- Acceptance: 25 Nov figures from Instinct (farm 305.9 kWh, 137.64 ₪ at 0.45 production; home 82.6 kWh, 35.98 ₪ at 0.48 export) reproduce in text and card.

## Left to the builder

- Exact card geometry and font sizes, as long as the Raqm check and layout tests pass.
- How the backfill job is scheduled (APScheduler one-off date job or an asyncio task after the callback), as long as it never blocks the callback response.
- The Hebrew weekday/month table (hand-written; no locale dependency in the image).

## PR 2 outline: RAY v1 (for reference, designed separately)

Oren's frozen v1 formula: RAY = min(0.40·Perf + 0.35·Health + 0.25·Env, Perf + 25). Perf from actual vs the same calendar month's average in the prior 1–2 years, cloud-adjusted, plus a history sub-score; Health from the 30-day coefficient of variation; Env from cloud (07:00–16:00 mean), Tmax and PM10 from Open-Meteo at the site's coordinates. Overall = kWh-weighted mean. Needs a 1–2 year daily backfill (about 25 credits per site, once) and an Open-Meteo client. Acceptance: 25.11 farm 90.2, home 90.5. Runs hidden for a few evenings beside Instinct's score before it shows. Open-Meteo's free tier is non-commercial; a paid plan is needed once Solya charges customers.
