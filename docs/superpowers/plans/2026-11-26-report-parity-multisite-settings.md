# Report Parity, Several Systems, Owner Settings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring the evening report to parity with Instinct's (minus RAY), guide owners through connecting several systems, let owners set name, income basis and tariff themselves, and load the month so far when a system connects.

**Architecture:** Additive changes to the existing FastAPI + python-telegram-bot service. One new service module for owner settings, one for backfill, one new table for settings links. Report text and card gain fields; the Telegram channel sends a caption. No change to OAuth or token handling.

**Tech Stack:** Python 3.12, FastAPI/Jinja2, python-telegram-bot 22, SQLAlchemy 2 on SQLite, Pillow + Raqm, httpx, pytest + respx. Run tests with `.venv/bin/python -m pytest -q`.

**Spec:** `docs/superpowers/specs/2026-11-26-report-parity-multisite-settings-design.md`. Read it before any task; where this plan and the spec disagree, the spec wins.

## Global Constraints

- Hebrew copy is exact as written in the spec and below. Sol speaks in the feminine. No U+200E/U+200F or other direction marks inside or around a `/command` in any copy.
- Every POST checks CSRF. No page signs anyone in on a GET.
- Secrets and OAuth codes are never logged.
- Credits: backfill uses priority `"optional"`; at most 2 calls per site per calendar month.
- Tariff bounds: 0.05–3.00 ₪/kWh, up to 4 decimals. Name: 1–30 characters after trimming, no control characters.
- Settings link: 15 minutes, single use, stored hashed (SHA-256 hex). Session from it: 24 hours.
- All existing tests keep passing, except where a task names a test it changes.
- Commit after each task with the trailer lines from the session's attribution instructions.

## Review Focus

1. **Tariff typed as "0,48", " 0.48 ", "₪0.48" or "0.48 ₪".** Owners type it like on their bill. Accept a comma as the decimal point and strip ₪ and spaces; reject anything else with the field error, keeping what they typed. Test in Task 1.
2. **A caption that fits before RTL marks but not after.** Telegram counts the text it receives. Measure the length after `rtl_text`, and fall back to photo-then-text above 1024. Test in Task 4.
3. **A settings link used twice, or after 15 minutes.** The second POST gets the "expired" page with status 410, never a 500, and never a session. Test in Task 6.
4. **Backfill must not touch today or any existing row.** The evening pull owns today's row. Backfill writes only days strictly before today (site-local) that have no row. Test in Task 8.
5. **A forged site id in a settings or picker POST** (another owner's site). It is ignored silently, as `/sites` does today. Test in Task 6.

---

### Task 1: Owner settings service, name defaults, label fallback

**Files:**
- Create: `src/solya/services/site_settings.py`
- Modify: `src/solya/services/onboarding.py` (`register_site`), `src/solya/rules/daily.py` (`effective_label`)
- Test: `tests/test_site_settings.py`; update `tests/test_rules_daily.py` if a label test changes

**Interfaces:**
- Produces:
  - `short_name(portal_name: str) -> str`: text before the first `" - "` if present, stripped, cut to 30 characters.
  - `default_basis(commercial: bool) -> str`: `"production"` if commercial, else `"export"`.
  - `parse_tariff(raw: str) -> Decimal | None`: `""` → `None`; strips spaces and `₪`, a comma becomes a dot; raises `ValueError("tariff")` outside 0.05–3.00 or with more than 4 decimals or non-numeric.
  - `validate_name(raw: str) -> str`: trimmed; raises `ValueError("name")` if empty, longer than 30, or containing a control character.
  - `SiteEdit` dataclass: `site_id: int, name: str, basis: str, tariff: Decimal | None, monitored: bool`.
  - `apply_edits(session, owner_id: int, edits: list[SiteEdit], today_by_site: Callable[[Site], date]) -> list[int]`: applies edits only to this owner's sites (others ignored). Writes `label_he` and `card_name` = name, `income_basis`, `tariff_ils_per_kwh`, `monitored`. For sites whose tariff or basis changed, recomputes `income_ils` for `daily_metrics` rows from the 1st of the site-local month to today, with `rules.daily.income`. Returns the ids whose `monitored` flag changed.
  - `apply_name_defaults(session) -> int`: sets `card_name = short_name(portal_name)` where `card_name` is empty; returns the count.
  - Error copy constants: `NAME_ERROR = "שם המערכת צריך להיות באורך 1 עד 30 תווים"`, `TARIFF_ERROR = "התעריף צריך להיות מספר בין 0.05 ל-3.00"`.
- `register_site`, for **new** sites only, sets `card_name = short_name(info.name)` and `income_basis = default_basis(commercial)`. Existing sites keep both.
- `effective_label(site, count)`: `label_he` → for count ≤ 1 `"המערכת"` → `card_name` → `portal_name`.

- [ ] **Step 1: Write failing tests** in `tests/test_site_settings.py`:
  - `test_short_name`: `"IL 360 - אורן סופר"` → `"IL 360"`; `"אורן סופר"` → `"אורן סופר"`; a 40-character name → its first 30 characters.
  - `test_parse_tariff_accepts_bill_formats`: `"0,48"`, `" 0.48 "`, `"₪0.48"`, `"0.48 ₪"` → `Decimal("0.48")`; `""` → `None`.
  - `test_parse_tariff_rejects`: `"0.04"`, `"3.01"`, `"0.12345"`, `"abc"` raise `ValueError("tariff")`.
  - `test_validate_name_rejects`: `"   "`, 31 characters, `"a\x07b"` raise `ValueError("name")`.
  - `test_apply_edits_recomputes_month_income`: a site with rows on the 1st and 24th of November and one on 31 October, tariff None. Apply tariff 0.45 with basis production: the November rows get income = production × 0.45 (rounded half-up to 0.01); the October row keeps `None`.
  - `test_apply_edits_export_basis_uses_export`: with basis export, income comes from `export_wh`; a row with `export_wh=None` gets `None`.
  - `test_apply_edits_ignores_other_owner`: an edit naming another owner's site changes nothing on it.
  - `test_apply_edits_returns_monitoring_changes`.
  - `test_register_site_sets_short_card_name_and_basis`: new 26.55 kWp site → `card_name` `"IL 360"` and basis `"export"`; new 65.52 kWp site → basis `"production"`. `test_register_site_keeps_existing_card_name_and_basis` covers a re-registered site.
  - `test_apply_name_defaults_only_empty`.
  - `test_effective_label_falls_back_to_card_name` (two sites, no `label_he`, `card_name="IL 360"` → `"IL 360"`); a single site with no `label_he` → `"המערכת"`.
- [ ] **Step 2: Run them and confirm they fail.** `.venv/bin/python -m pytest -q tests/test_site_settings.py`, expecting import errors.
- [ ] **Step 3: Implement** the functions above.
- [ ] **Step 4: Run the full suite** and confirm everything passes.
- [ ] **Step 5: Commit** with `feat(settings): owner site settings service and name defaults`.

### Task 2: Report text

**Files:**
- Modify: `src/solya/rules/daily.py` (`SiteDay` gains `income_basis: str = "production"`; `compute_site_day` sets it), `src/solya/services/reporting.py` (`_stored_day` sets it), `src/solya/text/he.py`
- Test: `tests/test_text_he.py`

**Interfaces:**
- Produces, in `text/he.py`:
  - `income_phrase(d: SiteDay) -> str`: `"הכנסה {fmt_ils}"` or `"הכנסה מהזרמה {fmt_ils}"` by `d.income_basis`; `""` when `d.income_ils is None`.
  - `daily_report(...)` keeps its signature. An ok site's line is `"{label}: אין התראות פעילות. {RAY…}{kWh}, {income_phrase}"`, or kWh only with no income. The total line, shown only when `show_total` is true, is `"סה״כ: {kWh}, {₪} לפני מע״מ"`.
  - `TARIFF_HINT = "כדי לראות הכנסה, שלחו /settings והזינו תעריף."`
  - `hebrew_date(d: date) -> str`, for example `"רביעי, 25 בנובמבר"`, from hand-written weekday and month tables.
- The weekly hint is appended by the reporter, not by `daily_report`: Task 3 owns sending it.

- [ ] **Step 1: Write failing tests:**
  - `test_daily_report_income_labels`: farm (production, 305.9 kWh, 137.64) and home (export, 82.6 kWh, 35.98). The text contains `"משק: אין התראות פעילות. 305.9 קוט״ש, הכנסה 137.64 ₪"` and `"בית: אין התראות פעילות. 82.6 קוט״ש, הכנסה מהזרמה 35.98 ₪"`, after removing RLM characters for the comparison.
  - `test_total_line_before_vat`: the total is `"סה״כ: 388.5 קוט״ש, 173.62 ₪ לפני מע״מ"`.
  - `test_no_tariff_kwh_only`: no "הכנסה" in the line.
  - `test_hebrew_date`: `date(2026,11,25)` → `"רביעי, 25 בנובמבר"`; `date(2026,1,4)` → `"ראשון, 4 בינואר"`.
- [ ] **Step 2: Run them and confirm they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the full suite.** Update any existing `test_text_he` assertion that expected the old line format, and only those.
- [ ] **Step 5: Commit** with `feat(report): label income type and pre-VAT total in the evening text`.

### Task 3: Card and report assembly

**Files:**
- Modify: `src/solya/render/card.py`, `src/solya/services/reporting.py` (`_card_row`, `build_owner_report`, `evening_report`)
- Test: `tests/test_card.py`, `tests/test_reporting.py`

**Interfaces:**
- `CardRow` gains `income_label: str | None = None` and `status: tuple[str, str] | None = None`, where the tuple is `("ok", "אין התראות פעילות")` or `("fault", "התראה פתוחה: {code}")`. With more than one open fault, the second form gets `" +{n-1}"`.
- `CardData` gains `header: str = ""` (the Hebrew date plus `" · נכון ל־HH:MM"` when known), `site_count: int = 1` (drawn as `"{n} מערכות"` when greater than 1), and `footnote: str | None = None` (`"הכנסה משוערת לפני מע״מ"` when any row has income). The date at the bottom moves into `header`; drop `date_label` and update the `_sample` helper.
- "נכון ל־" uses the earliest `Site.last_update_time` among the report's sites, in local time. Leave it out when any of them is None.
- `build_owner_report` return type is unchanged. `evening_report` appends `he.TARIFF_HINT` on its own line when any shown ok site has `tariff_ils_per_kwh is None`, and no `OutboxEntry` with key `tariff_hint:{owner_id}:{iso_year}-W{iso_week:02d}` exists. After the report is delivered successfully, it inserts that `OutboxEntry` (status `"sent"`, kind `"report"`, channel name, recipient, `sent_at=now`) as the once-a-week marker.

- [ ] **Step 1: Write failing tests:**
  - `test_card_renders_status_labels_header_footnote`: `render_card` runs on a 2-row `CardData` with every new field set and returns PNG bytes. Its height grows by the header and footnote heights compared with the same data without them.
  - `test_card_row_fault_status`: `_card_row` for a SiteDay with two open faults gives `status == ("fault", "התראה פתוחה: FAN_ALERT_1 +1")`.
  - `test_report_header_as_of`: two sites with `last_update_time` at 15:01Z and 15:06Z on 25 Nov give a header of `"רביעי, 25 בנובמבר · נכון ל־17:01"`.
  - `test_tariff_hint_weekly`: two reports in the same ISO week with a tariff-less site; only the first text contains `TARIFF_HINT`.
- [ ] **Step 2: Run them and confirm they fail.**
- [ ] **Step 3: Implement.** Geometry is the builder's call. Keep `assert_raqm` and the RTL drawing helpers.
- [ ] **Step 4: Run the full suite.** Regenerate `out/card_sample*.png` with `.venv/bin/python -m solya.render.card` and look at them once.
- [ ] **Step 5: Commit** with `feat(card): status chips, income labels, Hebrew date, as-of time, VAT footnote`.

### Task 4: One Telegram message with a caption

**Files:**
- Modify: `src/solya/messaging/telegram.py`
- Test: `tests/test_messaging.py`

**Interfaces:**
- `TelegramChannel.send`: if `image_png` is set and `len(msg.text) <= caption_limit`, call `send_photo(chat_id, photo, caption=msg.text)` only. Otherwise keep today's photo-then-split-text path. `msg.text` already went through `rtl_text` in `Reporter._send`, so the length check is on the final text.

- [ ] **Step 1: Write failing tests:**
  - `test_short_text_goes_as_caption`: a mocked bot gets one `send_photo` call with `caption=` equal to the text, and no `send_message` call.
  - `test_long_text_photo_then_message`: text of 1025 characters gives `send_photo` with no caption, then `send_message`.
- [ ] **Step 2: Run them and confirm they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the full suite.**
- [ ] **Step 5: Commit** with `feat(telegram): send the evening text as the card's caption`.

### Task 5: Several systems on the web

**Files:**
- Modify: `src/solya/web/oauth.py` (callback redirect), `src/solya/web/journey.py` (`/connect-site`, `/sites`, `/price`), templates `connect.html`, `sites.html`, `price.html`, `src/solya/services/reporting.py` (`on_site_connected`), `src/solya/text/he.py`
- Test: `tests/test_journey.py`, `tests/test_oauth.py`

**Interfaces:**
- Callback redirect, after a successful connect:
  - an unlinked web owner goes to `/sites?connected={sid}`, or `/sites?already={sid}` when `previous_owner == new_owner`;
  - a linked owner whose request carries a valid session cookie for the same owner goes to `/settings?connected=…` or `/settings?already=…` (Task 6 renders the banner);
  - otherwise the plain `CONNECTED` page, as today.
- `Reporter.on_site_connected(site, is_first, already: bool = False)`:
  - with `already`, it sends `he.site_already_connected(name)` = `"המערכת {name} כבר מחוברת. בחלון של SolarEdge בחרו מערכת אחרת."`;
  - otherwise it sends `he.site_connected(name)` = `"המערכת {name} חוברה בהצלחה. יש לך עוד מערכת? שלחו /connect."`.
- `/sites` and `/settings` banner: the query parameter's site id must belong to the session owner, otherwise no banner. `connected` shows `"המערכת {name} חוברה. יש לך עוד מערכת?"` with:
  - a primary button POSTing to `/connect-site`, labelled `"חיבור מערכת נוספת"`, with the hint `"בחלון של SolarEdge בחרו את המערכת שעוד לא חוברה."`;
  - a secondary link `"סיימתי, המשך"` (submits the picker form).

  `already` shows the "כבר מחוברת" sentence.
- `/sites` for an unlinked owner adds per-site inputs (see Task 6): `name_{id}`, `basis_{id}` and `tariff_{id}`, pre-filled with `label_he or card_name or short_name(portal_name)`, the stored `income_basis` and the tariff. The tariff field shows `"לא יודע/ת? אפשר להשאיר ריק."` beside it. POST `/sites` validates through `site_settings`. On error it re-renders with the submitted values and the field error, status 400.
- `connect.html`:
  - replace the footnote with `"כל מערכת מאשרים בנפרד ב-SolarEdge. אם יש לך כמה מערכות, אחרי הראשונה נחזור לכאן ותוכל/י לחבר את הבאה."`;
  - list connected systems by name under `"מחוברות:"`.
- `price.html` lists each monitored site with its `describe_price` above the total.
- `CHANGES_VIA_BOT = "לשינויים שלחו לסול בטלגרם /settings"`.

- [ ] **Step 1: Write failing tests:**
  - `test_two_connects_one_session_show_banner_each_time`: the second callback redirects to `/sites?connected=4552265`, and the page shows `"IL 360"` in the banner and both sites.
  - `test_reapprove_same_site_shows_already`: redirects to `?already=`, and the page contains `"כבר מחוברת"`.
  - `test_banner_ignores_foreign_site_id`.
  - `test_sites_post_saves_settings`: posting a name, basis export and tariff `"0,48"` stores Decimal 0.48 and the name in `label_he` and `card_name`.
  - `test_sites_post_bad_tariff_rerenders`: status 400, the typed value is kept, `TARIFF_ERROR` is shown and nothing is stored.
  - `test_price_lists_per_site`.
  - `test_connect_page_lists_connected_and_new_footnote`.
  - `test_linked_owner_with_session_lands_on_settings` (stub `/settings` if Task 6 is not done yet: assert only the redirect location).
  - `test_on_site_connected_already_message`.
- [ ] **Step 2: Run them and confirm they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the full suite.**
- [ ] **Step 5: Commit** with `feat(web): guided multi-system connect and settings on the picker`.

### Task 6: Settings links and the settings page

**Files:**
- Modify: `src/solya/models.py` (new `SettingsLink`), `src/solya/web/security.py`, `src/solya/web/journey.py` (or a new `src/solya/web/settings.py` router included in `web/__init__.py`), new templates `settings.html` and `settings_confirm.html`, `src/solya/services/onboarding.py`, `src/solya/services/reporting.py`
- Test: `tests/test_settings_web.py`

**Interfaces:**
- `SettingsLink` table `settings_links`: `token_hash` (str, primary key), `owner_id` (foreign key), `created_at`, `used_at` (nullable). `create_all` creates it; no `migrate.py` change is needed for a new table.
- `onboarding.create_settings_link(session, owner_id, now, settings) -> str`: returns `{public_base_url}/settings/{token}`, using `secrets.token_urlsafe(24)` and storing `sha256(token).hexdigest()`.
- `onboarding.resolve_settings_link(session, token, now) -> SettingsLink | None`: returns the link only if it is unused and less than 15 minutes old.
- `Sessions.issue(owner_id, max_age: int | None = None)` puts `"m": max_age` in the payload when given. `Sessions.read` rejects a cookie older than `"m"`, and otherwise keeps `SESSION_MAX_AGE`. `SETTINGS_SESSION_MAX_AGE = 24 * 3600`.
- Routes:
  - `GET /settings/{token}`: if the link is usable, render a confirm page with the button `"פתיחת ההגדרות"` and a CSRF tied to the pre-cookie, as `/join` does. Otherwise show `"הקישור פג. שלחו /settings לסול לקישור חדש."` with status 410.
  - `POST /settings/{token}`: check CSRF, resolve the link again, mark it used, issue a 24-hour session cookie and redirect 303 to `/settings`.
  - `GET /settings`: requires a session. It shows each site's fields, the price per site and the total, the banner from Task 5, and a `"חיבור מערכת נוספת"` POST to `/connect-site`.
  - `POST /settings`: check CSRF, validate, call `apply_edits`, require at least one monitored site (`PICK_ONE`), then show the page again with `"נשמר."`. When `apply_edits` returns monitoring changes, call `Reporter.notify_admins(text, key)`.
- `Reporter.notify_admins(text: str, key: str) -> None` sends to each `ADMIN_CHAT_IDS` entry with dedupe key `{key}:{chat_id}`. Use it for: `"בעלים {owner_id} שינה מעקב: {names}. מחיר חודשי חדש: {describe_price}"`. Refactor `on_site_reassigned` to use it.
- The callback "has session" check from Task 5 uses `Sessions.read` on the request's session cookie.

- [ ] **Step 1: Write failing tests:**
  - `test_settings_link_confirm_then_session`: GET shows the confirm page and no `Set-Cookie` for the session. POST with CSRF redirects to `/settings` and sets the session cookie.
  - `test_settings_link_single_use`: a second POST gives 410 and no cookie.
  - `test_settings_link_expires_after_15_minutes`: advance the clock 16 minutes; GET gives 410.
  - `test_settings_session_lasts_24h`: advance the clock 25 hours; `/settings` redirects to `/`.
  - `test_settings_save_and_admin_notice`: unticking one of two sites stores the change, and the fake channel gets one admin message containing the new price.
  - `test_settings_forged_site_ignored`.
  - `test_settings_requires_one_monitored`.
  - `test_token_stored_hashed`: the raw token is not in the table.
- [ ] **Step 2: Run them and confirm they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the full suite.**
- [ ] **Step 5: Commit** with `feat(web): owner settings page opened from a single-use Telegram link`.

### Task 7: Bot commands, free-text routing, invisible marks, command menu

**Files:**
- Modify: `src/solya/bot.py`, `src/solya/services/answers.py`, `src/solya/text/he.py`, `src/solya/app.py` (lifespan)
- Test: `tests/test_bot.py`, `tests/test_answers.py`

**Interfaces:**
- `answers.Route` gains `"connect"` and `"settings"`. `route_text` checks them before the others:
  - `connect`: `לחבר|תחבר|חיבור|מערכת נוספת|עוד מערכת|מערכת שני(?:ה|יה)`;
  - `settings`: `הגדרות|תעריף`.
- `answers.strip_marks(text: str) -> str` removes U+200B–U+200F, U+202A–U+202E, U+2066–U+2069 and U+FEFF, then trims.
- In the bot, the free-text handler strips the marks first. If the result matches `^/(\w+)(?:@\w+)?(?:\s.*)?$` and names a registered command, it calls that command's handler. Otherwise it routes the text. The `connect` and `settings` routes call the same code as `/connect` and `/settings`.
- `/settings` command: known owner → `he.settings_link(url)` = `"כדי לשנות שם, תעריף או מעקב, פתחו את הקישור (בתוקף ל־15 דקות):\n{url}"`; otherwise `NOT_INVITED`.
- `HELP = "אני יכולה לענות על: היום, אתמול, החודש, התראות.\nלחיבור מערכת נוספת שלחו /connect\nלשינוי שם, תעריף או מעקב שלחו /settings"`.
- `BOT_COMMANDS: list[tuple[str, str]]`, for example `[("connect","חיבור מערכת"), ("settings","הגדרות ותעריף"), ("today","היום"), ("yesterday","אתמול"), ("month","החודש"), ("alerts","התראות"), ("help","עזרה")]`. In the lifespan, after `bot_app.initialize()`, call `await bot_app.bot.set_my_commands(BOT_COMMANDS)` inside try/except, logging only the exception type.

- [ ] **Step 1: Write failing tests:**
  - `test_marked_command_dispatches`: text `"‎/connect‎"` replies with a connect link, not HELP.
  - `test_free_text_connect_request`: `"את יכול לחבר לי גם את המערכת השניה שלי?"` replies with a connect link.
  - `test_settings_command_known_owner` and `test_settings_command_unknown`.
  - `test_free_text_tariff_routes_to_settings`.
  - `test_help_lists_commands`.
  - `test_strip_marks`.
  - `test_existing_routes_unchanged`: "אתמול", "החודש" and "התראות" still route as before.
- [ ] **Step 2: Run them and confirm they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the full suite.**
- [ ] **Step 5: Commit** with `feat(bot): /settings, connect by request, tolerate copied commands, command menu`.

### Task 8: Month-to-date backfill

**Files:**
- Create: `src/solya/services/backfill.py`
- Modify: `src/solya/solaredge/client.py`, `src/solya/solaredge/parse.py`, `src/solya/services/reporting.py`, `src/solya/scheduler.py` (`start`), `src/solya/web/oauth.py` (callback)
- Test: `tests/test_backfill.py`, `tests/test_client.py`

**Interfaces:**
- `SolarEdgeClient.get_energy_daily(site_id, start: date, end: date, tz: str, *, priority="optional") -> dict[date, float]` calls `GET /sites/{id}/energy`. Its params are `from` and `to` built from the site-local window, using `day_window_utc` for the start and end days, plus `resolution=DAY`. It returns Wh by site-local date via `to_wh`, skipping null values.
- `SolarEdgeClient.get_meter_export_daily(site_id, start, end, tz, *, priority="optional") -> dict[date, float] | None` calls `GET /sites/{id}/meters/telemetry` with `resolution=DAY`. It sums `exportEnergy` across meters by date and returns None on `NotFound`, or when no meter reports `exportEnergy`.
- `backfill.backfill_month(client, session_factory, site_id: int, now: datetime) -> int`, with these rules:
  - The window runs from the 1st of the site-local month to yesterday.
  - It returns 0 without calling anything when the window is empty, or when a `RawResponse` exists for path `/sites/{id}/energy` with `fetched_at` on or after the 1st of this month (site-local). That is the "tried this month" memory.
  - It writes rows only for dates in the window that have no `DailyMetric`, with `comm_state="backfill"`, `as_of=now`, `computed_at=now`, and `income_ils` from `rules.daily.income` using the site's tariff and basis.
  - It returns the number of rows written.
- `Reporter.backfill_soon(site_id: int) -> None` schedules `backfill_month` as a background task through the existing `_background` set. It never raises into the caller and logs exception types only.
- Callers:
  - the callback calls `backfill_soon(sid)` after `on_site_connected` for a new site (`is_first` or not, but not on `already`);
  - `SchedulerService.start()` adds one `DateTrigger` job one minute after start that calls `backfill_soon` for every active, monitored site;
  - the same startup job calls `site_settings.apply_name_defaults` once.
- `answers` and `build_owner_report` need no change: `"backfill"` rows have `production_wh` set, so they count as data. Confirm this with a test.

- [ ] **Step 1: Write failing tests:**
  - `test_get_energy_daily_parses`: respx returns the spec's `Measurements` shape with 3 DAY values in WH and KWH units, and the method gives the expected Wh map.
  - `test_get_meter_export_daily_no_meter`: a 404 gives None.
  - `test_backfill_fills_only_missing_past_days`: clock on 26 Nov 15:00Z, existing rows on 10 Nov and 26 Nov. It writes 1–9 and 11–25 Nov, leaves 10 and 26 Nov unchanged and never writes the 26th.
  - `test_backfill_once_per_month`: a second call makes no HTTP calls.
  - `test_backfill_first_of_month_noop`.
  - `test_backfill_export_income`: an export-basis site with tariff 0.48 and meter export values gets income on backfilled rows; with no meter, its income is None.
  - `test_month_answer_counts_backfill_rows`.
  - `test_callback_schedules_backfill`.
- [ ] **Step 2: Run them and confirm they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the full suite.**
- [ ] **Step 5: Commit** with `feat(backfill): load the month so far when a system connects and at startup`.

### Task 9: Acceptance on 25 Nov figures, docs

**Files:**
- Modify: `tests/test_acceptance.py`, `README.md`, `deploy/ec2/README.md`, `deploy/azure/README.md`
- Test: `tests/test_acceptance.py`

**Interfaces:**
- Consumes everything above.

- [ ] **Step 1: Write the failing test** `test_a12_instinct_parity_25_nov`, with two sites for one owner:
  - farm: production basis, tariff 0.45, 305 870 Wh;
  - home: export basis, tariff 0.48, 82 600 Wh production and 74 960 Wh export;
  - stored rows for 25 Nov.

  `build_owner_report` gives text containing `"305.9 קוט״ש, הכנסה 137.64 ₪"`, `"82.6 קוט״ש, הכנסה מהזרמה 35.98 ₪"` and `"סה״כ: 388.5 קוט״ש, 173.62 ₪ לפני מע״מ"`, after removing RLM. It also gives a card with 2 rows, the status `("ok", "אין התראות פעילות")` on both, `site_count == 2` and the footnote set.

  Income is computed from unrounded Wh: 305.87 × 0.45 = 137.64 and 74.96 × 0.48 = 35.98, which are Instinct's numbers.
- [ ] **Step 2: Run it.** It should pass if Tasks 1–8 are complete; fix any gap in the owning task's code.
- [ ] **Step 3: Update the docs:**
  - README: owner settings and `/settings`, the commands menu, backfill credits (at most 2 per site per month).
  - `deploy/ec2/README.md` and `deploy/azure/README.md`: the Solya callback must be the app's **default** redirect URI, because SolarEdge ignores other entries. Remove the advice that several redirect URIs are fine.
  - Admin step 10/11 in the README becomes optional ("override only").
- [ ] **Step 4: Run the full suite and ruff.** Run `.venv/bin/python -m pytest -q` and `.venv/bin/ruff check src tests`; both must pass.
- [ ] **Step 5: Commit** with `test(acceptance): Instinct parity on 25 Nov; docs: owner settings, default redirect URI`.
