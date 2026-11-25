# Web Journey, Admin, AI Answers and Low-Credit Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add PR #1's customer-facing pieces to the PR #2 Python service without weakening anything it already does: an invite-gated Hebrew web journey ending in a Telegram link, a read-only admin page, optional AI answers, and a per-site low-credit call plan.

**Architecture:**
- **Pages:** server-rendered Jinja2 pages, served by the existing FastAPI app.
- **Session:** a signed cookie with CSRF protection.
- **Pricing:** a small pure pricing module, plus a stub payment record.
- **Owners:** may exist without a Telegram chat until they open the deep link. Every send path skips them without changing any fault or outbox state.
- **AI layer:** an optional fallback behind the fixed Q&A router.
- **Low-credit plan:** a per-site switch read by the ingest pull and by the scheduler.

**Tech Stack:** what is already in the project, plus `jinja2` and `python-multipart` (form posts) and `itsdangerous` (signed cookies). AI calls use the existing `httpx`.

**Spec:** `docs/superpowers/specs/2026-11-25-web-onboarding-port-design.md`. That is the binding authority. Read §3, §5 and §5.6.

Reference for wording and layout, read-only: PR #1 on branch `origin/cursor/solya-mvp0-telegram-7243`. Read a file with `git show origin/cursor/solya-mvp0-telegram-7243:<path>`.

## Global Constraints

**Must not break**
- The existing 294 tests stay green.
- Acceptance tests a1–a10 are not weakened. a6 stays at ≤ 350 credits per site and ≤ 2000 in total.

**Settings and secrets**
- New settings are optional, and existing `.env` files keep working.
  - `SESSION_SECRET` defaults to `hmac(token_encryption_key, b"solya-session")`.
  - `ADMIN_PASSWORD` is unset by default, which makes `/admin` return 404.
  - `LLM_ENABLED=false`.
  - `TRUSTED_PROXIES="127.0.0.1"`.
- Never log or render tokens or secrets.
- Email and phone appear only on `/admin`.

**Pages**
- Every page is `<html dir="rtl" lang="he">`, mobile-first, with no JavaScript build step and no external assets.
- Every form POST carries a CSRF token bound to the session.
- Journey pages without a valid session redirect with 303 to `/`.

**Pricing**
- 4997 agorot per site up to 50 kW, plus 100 agorot per kW above 50, rounded half-up.
- The kW is `billing_kwp_override`, else `peak_power_kwp_reported` with the price marked as an estimate, else unknown.

**Exact Hebrew copy** (verbatim from the spec and PR #1):
- estimate label: `הערכה לפי נתוני הפורטל, המחיר הסופי ייקבע לפי ההספק המותקן`
- unknown price: `המחיר יחושב כשקילוואט מותקן שמור אצלנו. נתון ההספק מהפורטל לא משמש לחיוב.`
- VAT: `אופן הצגת המע״מ טרם נקבע.`
- payment stub: `טרם שולם. ספק הסליקה טרם נבחר, ולא בוצע חיוב ולא הופעל מנוי.`
- bad invite: `הקוד אינו בתוקף`
- chat already bound: `הצ'אט הזה כבר מחובר לחשבון אחר`
- re-signup after linking: `כבר נרשמת, המשך בטלגרם`
- AI budget used: `התקציב החודשי לתשובות פתוחות נוצל. אני עדיין שולחת את הסיכום וההתראות.`

**RAY and telemetry**
- Don't mention RAY on the landing page unless `settings.ray_implementation` is set.
- The low plan's evening pull calls only `/sites/{id}`, `/overview`, `/alerts`, and `/devices` weekly.

## Review Focus

1. **Double submit or a reused invite.**
   - Expected: the second `POST /signup` with the same code re-renders the form with `הקוד אינו בתוקף`, and there is exactly one Owner.
   - Test: Task 5, `test_signup_invite_single_use`.
2. **Cross-owner access.**
   - Expected: `/pay/{id}` for another owner's payment returns 404, and `/sites` only ever lists the session owner's sites.
   - Test: Task 6, `test_pay_other_owner_404` and `test_sites_lists_only_own`.
3. **An owner with no chat at report time.**
   - Expected: nothing is sent and no fault is stamped, while other owners still get their reports.
   - Test: Task 2, `test_unlinked_owner_skipped_others_sent`.
4. **The AI provider times out or returns a 5xx.**
   - Expected: the owner gets `המידע עדיין לא הגיע אלי, אני על זה.`, the handler doesn't crash, and no tokens are logged.
   - Test: Task 9, `test_ai_provider_error_fallback`.
5. **Admin login behind the proxy.**
   - Expected: the lockout counts per `X-Forwarded-For` client when the request comes from a trusted proxy, so one attacker can't lock out the real admin. An untrusted peer's `X-Forwarded-For` is ignored.
   - Test: Task 4, `test_client_ip_trusted_proxy`.
   - Test: Task 8, `test_admin_lockout_per_client`.
6. **Phone input with dashes or spaces.**
   - Expected: `050-123 4567` is accepted (10 digits), and leading or trailing spaces are trimmed.
   - Test: Task 5, `test_signup_phone_formats`.

---

## File Structure

```
src/solya/
  models.py                 modify: owner/site columns; Payment, LlmUsage tables
  migrate.py                new: idempotent SQLite column adds, run from init_db
  db.py                     modify: init_db calls migrate
  config.py                 modify: new optional settings
  pricing.py                new: pure pricing
  services/payments.py      new: stub checkout
  services/onboarding.py    modify: web owner create, link token, monitored, invite links
  services/reporting.py     modify: skip unlinked owners and unmonitored sites; low plan; link-time messages
  services/ai_answers.py    new: optional AI layer
  scheduler.py              modify: plan filters monitored and plan
  ingest.py                 modify: low-plan endpoint set
  bot.py                    modify: /start link token; AI fallback
  cli.py                    modify: --monitored, --call-plan, invite links
  web/__init__.py           new: router assembly (keeps the existing callback routes)
  web/security.py           new: session cookie, CSRF, rate limiter, client IP
  web/journey.py            new: /, /join, /signup, /connect-site, /sites, /price, /pay, /ready
  web/admin.py              new: /admin, login, logout
  web/templates/*.html      new: base, landing, connect, sites, price, pay, ready, admin, admin_login, error
  web/static/style.css      new
  web.py → web/oauth.py     move: the existing connect/callback routes, unchanged except for the web redirect
tests/
  test_migrate.py, test_pricing.py, test_web_security.py, test_journey.py, test_admin.py,
  test_ai_answers.py, test_low_plan.py, plus additions to existing tests
```

---

### Task 1: Data model, migration and settings

**Files:**
- Modify: `src/solya/models.py`, `src/solya/db.py`, `src/solya/config.py`, `.env.example`
- Create: `src/solya/migrate.py`, `tests/test_migrate.py`

**Interfaces:**
- Produces:
  - **Owner** columns:
    - `telegram_chat_id: int | None` (nullable, unique)
    - `email: str | None`
    - `phone: str | None`
    - `signup_source: str = "bot"` (`bot` or `web`)
    - `link_token: str | None` (unique)
    - `subscription_status: str = "active"`, where `"lead"` is set explicitly for web sign-ups (`lead|connected|pending_payment|active`)
  - **Site** columns:
    - `monitored: bool = True`
    - `call_plan: str = "standard"` (`standard` or `low`)
  - `Payment(id: str pk (uuid4 hex), owner_id, amount_agorot: int | None, amount_is_estimate: bool, provider: str = "stub", status: str = "stub_pending", description: str, created_at)`.
  - `LlmUsage(id pk, owner_id, ts, input_tokens: int, output_tokens: int)`.
  - `migrate.apply(engine) -> list[str]`: for an existing SQLite DB, it adds any missing columns above with `ALTER TABLE … ADD COLUMN`, plus a `CREATE UNIQUE INDEX IF NOT EXISTS` on the link token. It returns the statements it ran, and is idempotent.
    - Making `telegram_chat_id` nullable on old DBs: SQLite can't alter that constraint, so `apply` rebuilds `owners` (create new, copy, drop, rename) only if that column is `NOT NULL`. This runs inside one transaction.
  - `init_db(engine)` calls `create_all`, then `migrate.apply`.
  - **Settings** (all optional):
    - `session_secret: SecretStr | None = None`
    - `admin_password: SecretStr | None = None`
    - `trusted_proxies: list[str] = ["127.0.0.1"]`
    - `llm_enabled: bool = False`
    - `llm_base_url: str = "https://openrouter.ai/api/v1"`
    - `llm_api_key: SecretStr | None = None`
    - `llm_model: str | None = None`
    - `llm_monthly_tokens_per_owner: int = 50000`
  - `Settings.effective_session_secret() -> bytes`
- [ ] **Step 1: Write `tests/test_migrate.py`.**
  - `test_fresh_db_has_new_columns`: Owner can be created with `telegram_chat_id=None`. Two such owners coexist (NULLs are not unique-conflicting).
  - `test_migrate_old_schema`: build a DB with the pre-change `owners` and `sites` DDL (copy it from `git show HEAD:src/solya/models.py` into a raw-SQL fixture) and insert one owner and one site. After `migrate.apply`, the row is intact, `monitored` is 1, `call_plan` is `'standard'`, and `telegram_chat_id` accepts NULL.
  - `test_migrate_idempotent`: a second `apply` returns `[]`.
  - `test_session_secret_fallback`: unset gives 32 bytes, deterministic for the same key, and different when the key differs.
- [ ] **Step 2:** Run `pytest tests/test_migrate.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement.** Add `jinja2`, `python-multipart` and `itsdangerous` to `pyproject.toml` dependencies now (later tasks need them). Add the new settings, commented, to `.env.example`.
- [ ] **Step 4:** Run `pytest -q`. Expected: all pass, including the existing 294.
- [ ] **Step 5:** Commit `feat: owner/site fields, payments and AI usage tables, idempotent SQLite migration`.

### Task 2: Unlinked owners and unmonitored sites across sending, scheduling and answers

**Files:**
- Modify: `src/solya/services/reporting.py`, `src/solya/scheduler.py`, `src/solya/services/answers.py`, `src/solya/messaging/outbox.py`, `src/solya/services/onboarding.py`
- Test: `tests/test_unlinked.py`

**Interfaces:**
- Consumes: Task 1 columns.
- Produces:
  - `reporting.owner_chat(owner) -> str | None` returns `None` when `telegram_chat_id is None` or `not owner.active`. Every send site in `Reporter` uses it. When it returns `None`: log `skip_unlinked`, do not call `Outbox.deliver`, and do not set `notified_at`, `reminded_at` or `closed_notified_at`.
  - `on_site_connected` sends nothing when the chat is `None`.
  - `plan_day_jobs` creates pull and mid-day jobs only for sites that are `monitored and status == "active"`. It creates a report job only for owners with a chat and at least one monitored active site.
  - `keepalive` still covers unmonitored sites (rule R17 unchanged).
  - `build_owner_report` and the `answers.*` functions include only monitored sites.
  - When a pull reaches an unmonitored site (manual `solya pull` only), it stores raw data but skips `sync_faults`.
  - `onboarding.register_site`: on reassignment to another owner, it also sets `monitored = True` (spec §5.6.6).
- [ ] **Step 1: Write the tests.**
  - `test_unlinked_owner_skipped_others_sent`: two owners, one with `telegram_chat_id=None`, both with a fresh fan alert. Run `evening_pull` for both sites, then `evening_report` for both. FakeChannel gets messages only for the linked owner. The unlinked owner's FaultRecord keeps `notified_at is None`, and no OutboxEntry exists for it.
  - `test_unlinked_no_stale_reminder`: then link the chat and run `send_reminders` at +4 h. Nothing is sent, because the fault was never notified. The next `evening_pull` sends exactly one fault alert.
  - `test_unmonitored_not_planned_but_kept_alive`: `plan_day_jobs` has no job for the unmonitored site, and `keepalive` refreshes it after 7 days.
  - `test_unmonitored_excluded_from_report_and_answers`.
  - `test_reassign_resets_monitored`.
- [ ] **Step 2:** Run the tests. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run `pytest -q`. Expected: all pass.
- [ ] **Step 5:** Commit `feat: skip unlinked owners without consuming fault state; monitored flag drives planning, reports and answers`.

### Task 3: Low-credit call plan, plus the CLI flags

**Files:**
- Modify: `src/solya/ingest.py`, `src/solya/services/reporting.py`, `src/solya/scheduler.py`, `src/solya/cli.py`, `README.md`
- Test: `tests/test_low_plan.py`, plus `tests/test_acceptance.py` (add `test_a6b_low_plan_budget`)

**Interfaces:**
- Consumes: Task 1 `Site.call_plan`.
- Produces:
  - `pull_site(..., call_plan: str = "standard")`. For `low`: no inverter status, no lifetime, devices only when they are more than 7 days old, and the bundle's `inverters` and `lifetime` are `None`, not errors. `Reporter` passes `site.call_plan`.
  - `plan_day_jobs`: no mid-day job for low-plan sites.
  - `cli sites set` gains `--call-plan {standard,low}` and `--monitored/--no-monitored`.
    - Switching to `low` closes this site's open faults whose base key starts with `mode:` or `missing:`. It sets `closed_at = now` without notifying, then prints and logs `closed N inverter faults (low plan)`.
  - `DailyMetric.lifetime_wh` is `None` on low days.
- [ ] **Step 1: Write the tests.**
  - `test_low_plan_calls_only_three_endpoints`: respx records the paths `/sites/S`, `/overview`, `/alerts` and nothing else (devices are fresh).
  - `test_low_plan_no_midday_job`.
  - `test_switch_to_low_closes_inverter_faults_silently`: no message is sent, and an `alert:` fault stays open.
  - `test_a6b_low_plan_budget`: the a6 harness with one home site on `low` for 30 days. Credited rows are ≤ 120. Print the number.
  - `test_cli_monitored_flag`.
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3: Implement.** Add a README note: the low plan detects only open SolarEdge alerts and zero production, not inverter-mode or missing-inverter faults.
- [ ] **Step 4:** Run `pytest -q`. Expected: all pass, and a6 is unchanged.
- [ ] **Step 5:** Commit `feat: per-site low-credit call plan; CLI call-plan and monitored flags`.

### Task 4: Pricing, stub payments and web security primitives

**Files:**
- Create: `src/solya/pricing.py`, `src/solya/services/payments.py`, `src/solya/web/__init__.py`, `src/solya/web/security.py`
- Move: `src/solya/web.py` → `src/solya/web/oauth.py`, with the routes unchanged. `create_app` includes the router from `solya.web`.
- Test: `tests/test_pricing.py`, `tests/test_web_security.py`

**Interfaces:**
- Produces:
  - `pricing.SitePrice(site_id: int, agorot: int | None, is_estimate: bool, kw: float | None)`.
  - `pricing.price_site(site: Site) -> SitePrice`: uses the override, else the reported kWp (with `is_estimate=True`), else `None`.
  - `pricing.price_total(prices: list[SitePrice]) -> tuple[int | None, bool]`: the total is `None` if any site has no amount. The flag is true if any site is an estimate.
  - `pricing.describe_price(agorot: int | None, is_estimate: bool) -> str`, which assembles the Global-Constraints copy:
    - `f"{agorot/100:.2f} ₪ לחודש."`
    - followed by `" " + ESTIMATE_LABEL + "."` when `is_estimate`
    - followed by `" אופן הצגת המע״מ טרם נקבע."`
    - Unknown gives `UNKNOWN_PRICE`.
  - `payments.create_stub_checkout(session, owner_id: int, amount_agorot: int | None, is_estimate: bool, description: str, now) -> Payment`: sets `owner.subscription_status = "pending_payment"`.
  - `security.Sessions(secret: bytes)`:
    - `issue(owner_id: int) -> str`
    - `read(cookie: str | None) -> int | None` (max age 30 days)
    - `issue_admin() -> str`
    - `read_admin(cookie) -> bool` (12 h)
    - `csrf_token(cookie_value: str) -> str`
    - `check_csrf(cookie_value, token) -> bool`
  - Cookie names: `solya_s` and `solya_admin`. Both are HttpOnly, SameSite=Lax, and Secure when `public_base_url` is https.
  - `security.client_ip(request, trusted: list[str]) -> str`: returns the left-most `X-Forwarded-For` entry only if `request.client.host` is in `trusted`, else `request.client.host`.
  - `security.RateLimiter(limit: int, window_s: int, clock)`, with `hit(key) -> bool` returning `False` when over the limit.
- [ ] **Step 1: Write the tests.**
  - `test_price_under_50kw`: 26.55 kW gives 4997 and is not an estimate when the override is set.
  - `test_price_above_50kw`: 90.72 gives 4997 + round(40.72 × 100) = 9069.
  - `test_estimate_from_portal`: override None and reported 65.52 gives 4997 + 1552 = 6549, with `is_estimate=True`.
  - `test_unknown_price`.
  - `test_total_none_if_any_unknown`.
  - `test_describe_price_estimate_copy`: exact string.
  - `test_stub_checkout_sets_pending`.
  - `test_session_roundtrip_and_tamper`.
  - `test_session_expiry`, using FixedClock with itsdangerous `max_age`. Inject time if needed.
  - `test_csrf_bound_to_session`.
  - `test_client_ip_trusted_proxy`: trusted peer plus XFF `1.2.3.4, 10.0.0.1` gives `1.2.3.4`. An untrusted peer with XFF gives the peer.
  - `test_rate_limiter_window`.
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3: Implement.** Also pass `proxy_headers=True, forwarded_allow_ips=",".join(settings.trusted_proxies)` in `app.serve`.
- [ ] **Step 4:** Run `pytest -q`. Expected: all pass. The existing `tests/test_web.py` must still pass after the move.
- [ ] **Step 5:** Commit `feat: pricing, stub payments, signed sessions, CSRF and proxy-aware rate limiting`.

### Task 5: Landing, join and sign-up

**Files:**
- Create: `src/solya/web/journey.py`, `src/solya/web/templates/{base,landing,error}.html`, `src/solya/web/static/style.css`
- Modify: `src/solya/services/onboarding.py`
- Test: `tests/test_journey.py`

**Interfaces:**
- Consumes: Task 4 `Sessions`, `RateLimiter`, `client_ip`.
- Produces:
  - `onboarding.signup_web(session, invite_token: str, name: str, email: str, phone: str, now) -> Owner | None`, in one transaction:
    - validate: name ≥ 2 chars after strip, `"@" in email`, phone ≥ 9 digits;
    - redeem the invite: unused, or used by a web owner without a linked chat (that is the "continue" case, which returns the existing owner);
    - create `Owner(signup_source="web", subscription_status="lead", link_token=secrets.token_urlsafe(24), telegram_chat_id=None)`.

    It returns `None` on an invalid invite. A `ValueError("form")` means bad fields.
  - Routes:
    - `GET /` renders the landing page. Copy is adapted from PR #1's `src/app/page.tsx`, without RAY unless it is enabled. The form has name, email, phone, a code field, and a hidden CSRF field.
    - `GET /join/{invite}` renders the same page with the code pre-filled. If that invite belongs to a web owner who is already linked, it renders `כבר נרשמת, המשך בטלגרם` instead.
    - `POST /signup`: rate limit 10 per hour per client IP; then CSRF check; then `signup_web`. On success: set the cookie and 303 to `/connect-site`. Otherwise re-render with an error: `הקוד אינו בתוקף` for an invite problem, `נא למלא שם, אימייל וטלפון תקינים` for field problems.
  - For CSRF before the session exists, the landing page sets a pre-session cookie `solya_pre` (random) and binds the token to it.
- [ ] **Step 1: Write the tests** (TestClient, `start_background=False`).
  - `test_landing_rtl_no_ray_when_disabled`: the page has `dir="rtl"` and no `RAY`.
  - `test_signup_creates_web_owner_and_redirects`.
  - `test_signup_invite_single_use`: Review Focus 1.
  - `test_signup_bad_fields`.
  - `test_signup_phone_formats`: Review Focus 6.
  - `test_signup_csrf_required`.
  - `test_signup_rate_limited`: the 11th request gets 429 with a Hebrew page.
  - `test_join_resume_before_link`: the same invite and the same owner give a new cookie.
  - `test_join_after_link_shows_continue_in_telegram`.
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3: Implement.** Look at the rendered landing page once (Playwright screenshot at phone width, 390 px) and fix any RTL layout problems.
- [ ] **Step 4:** Run `pytest -q`. Expected: all pass.
- [ ] **Step 5:** Commit `feat: invite-gated Hebrew landing and web sign-up`.

### Task 6: Connect, site picker, price, payment stub and ready pages

**Files:**
- Modify: `src/solya/web/journey.py`, `src/solya/web/oauth.py` (callback redirect)
- Create: `src/solya/web/templates/{connect,sites,price,pay,ready}.html`
- Test: `tests/test_journey.py` (extend)

**Interfaces:**
- Consumes: Task 4 pricing and payments, Task 5 session, `onboarding.create_connect_request(session, owner_id, now, settings) -> url`.
- Produces:
  - `GET /connect-site`: copy from PR #1's `src/app/connect/page.tsx`. `POST /connect-site` (with CSRF) creates the request and returns 303 to `/connect/{token}`.
  - **Callback:** after success, if `owner.signup_source == "web"` and the chat is unlinked, set `subscription_status = "connected"` (when it was `lead`) and return 303 to `/sites`. Otherwise keep the current success page.
  - `GET /sites` lists the session owner's sites: checkbox (`monitored`), card name or portal name, site id, and per-site price via `describe_price`. Links go to `/connect-site` (add another) and continue.
    - `POST /sites` saves the flags. If none is ticked, re-render with `יש לבחור לפחות מערכת אחת`. Then 303 to `/price`.
    - After the chat is linked, `POST /sites` returns 409 with `השינויים נעשים עכשיו דרך סול בטלגרם`.
  - `GET /price` shows the total via `describe_price(price_total(...))`. `POST /pay` creates the stub checkout and returns 303 to `/pay/{id}`.
  - `GET /pay/{id}` returns 404 unless the payment belongs to the session owner. It shows the amount and the stub copy, plus a link to `/ready`.
  - `GET /ready` shows `המערכות נשמרו.` and a button to `https://t.me/{bot}?start={link_token}`. If the owner is already linked, it shows `כבר נרשמת, המשך בטלגרם`.
- [ ] **Step 1: Write the tests.**
  - `test_full_journey_to_ready`. Steps: signup, then POST connect-site, then follow to `/connect/{token}`, then call the callback with respx stubs (token, and `/sites/3033498` with peakPower 65.52). Then `/sites` shows the site, POST it, `/price` shows `65.49 ₪` and the estimate label, POST `/pay` gives a Payment with `amount_is_estimate`, and `/ready` contains the `t.me` link with the `link_token`.
  - `test_pay_other_owner_404`: Review Focus 2.
  - `test_sites_lists_only_own`: Review Focus 2.
  - `test_sites_requires_one`.
  - `test_journey_pages_redirect_without_session`.
  - `test_bot_owner_callback_unchanged`: the existing success page still appears for bot owners.
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run `pytest -q`. Expected: all pass.
- [ ] **Step 5:** Commit `feat: web connect, site picker, price estimate, payment stub and ready pages`.

### Task 7: Telegram link via `/start`, link-time messages, invite links

**Files:**
- Modify: `src/solya/bot.py`, `src/solya/services/onboarding.py`, `src/solya/services/reporting.py`, `src/solya/text/he.py`, `src/solya/cli.py`
- Test: `tests/test_bot.py`, `tests/test_onboarding.py` (extend)

**Interfaces:**
- Produces:
  - `onboarding.link_chat(session, link_token: str, chat_id: int, now) -> Literal["linked","bound_elsewhere","unknown"]`.
    - `linked` sets `telegram_chat_id`, clears `link_token`, and sets `last_inbound_at`.
    - `bound_elsewhere` means the chat already belongs to a different owner; nothing changes.
  - `/start <payload>` tries `link_chat` first, then the existing invite redeem.
    - On `linked`, it calls `Reporter.on_owner_linked(owner_id)`. That sends `WELCOME_1`, `WELCOME_2` (through the existing `delay_send` 60 s) and `he.sites_monitored(n)` (`"המערכות שלך במעקב: {n}"`), then calls `scheduler.plan_owner(owner_id)`.
    - On `bound_elsewhere`, it replies `הצ'אט הזה כבר מחובר לחשבון אחר`.
  - `solya invite create` prints two lines: `telegram: https://t.me/<bot>?start=<t>` and `web: <public_base_url>/join/<t>`.
- [ ] **Step 1: Write the tests.**
  - `test_start_link_token_links_and_welcomes`: FakeChannel gets the welcome messages and the sites line, and `plan_owner` is called.
  - `test_start_link_token_single_use`.
  - `test_start_bound_elsewhere_changes_nothing`.
  - `test_start_invite_still_works`.
  - `test_invite_create_prints_both_links`.
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run `pytest -q`. Expected: all pass.
- [ ] **Step 5:** Commit `feat: link web sign-ups to Telegram via deep link; dual invite links`.

### Task 8: Admin page

**Files:**
- Create: `src/solya/web/admin.py`, `src/solya/web/templates/{admin,admin_login}.html`
- Test: `tests/test_admin.py`

**Interfaces:**
- Consumes: Task 4 `Sessions.issue_admin/read_admin`, `RateLimiter`, `client_ip`. Also `CreditBudget.used_this_cycle()` and `used_by_site(site_id, since)`, and `LlmUsage`.
- Produces:
  - Routes: `GET /admin/login`, `POST /admin/login` (CSRF, rate limit of 5 failures per 15 min per client IP, `hmac.compare_digest`), `POST /admin/logout`, `GET /admin`.
  - Without `admin_password`, every `/admin*` route returns 404.
  - `GET /admin` renders the table from spec §5.3. It uses PR #1's status labels: `{"lead":"נרשם","connected":"מחובר","pending_payment":"ממתין לתשלום","active":"פעיל"}`. A header line shows `קרדיטים במחזור: {used}/{cap}`.
- [ ] **Step 1: Write the tests.**
  - `test_admin_404_without_password`.
  - `test_admin_login_and_table`: shows the owner's email and phone, `monitored`, `call_plan`, status and credits. The response body contains no ciphertext or token plaintext.
  - `test_admin_requires_login`.
  - `test_admin_lockout_per_client`: Review Focus 5. Five bad attempts from XFF `1.1.1.1` via the trusted proxy lock out `1.1.1.1`, while `2.2.2.2` can still log in.
  - `test_admin_csrf`.
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run `pytest -q`. Expected: all pass.
- [ ] **Step 5:** Commit `feat: read-only admin page with password login`.

### Task 9: Optional AI answers

**Files:**
- Create: `src/solya/services/ai_answers.py`
- Modify: `src/solya/bot.py`, `README.md`
- Test: `tests/test_ai_answers.py`

**Interfaces:**
- Consumes: Task 1 `LlmUsage` and settings; `answers.route_text`; `DailyMetric`; `open_fault_views`.
- Produces:
  - `ai_answers.build_context(session, owner, now) -> dict`. It contains up to 31 days of monitored sites' `{date, label, production_kwh, income_ils, comm_state}` plus open faults `{code, serial, first_seen}`. No contact details, tokens or raw series.
  - `async ai_answers.answer(http, settings, session_factory, owner_id, question, now) -> str`.
    - Disabled, or no key or model: returns `HELP`.
    - Budget used up: returns the budget copy.
    - Otherwise it makes a POST to `{llm_base_url}/chat/completions` with `max_tokens` 300 and a 20 s timeout.
    - The system prompt adapts PR #1's `src/lib/llm/openrouter.ts` `SYSTEM` (the same five rules).
    - It logs usage, applies the numeric guard, and returns `rtl_text(reply)`.
  - **Numeric guard:** every number in the reply (regex `\d+(?:[.,]\d+)?`) must match a number in the context JSON, rounded to 0, 1 or 2 decimals. Otherwise return `HELP`.
  - **Errors:** a timeout or HTTP error returns `המידע עדיין לא הגיע אלי, אני על זה.` and logs only the exception type.
  - The bot text handler calls it when the route is `help` and `settings.llm_enabled` is set.
- [ ] **Step 1: Write the tests** (respx).
  - `test_ai_disabled_returns_help`: no HTTP call.
  - `test_ai_answer_uses_context_only`: the request body contains no email, phone or token.
  - `test_ai_numeric_guard_blocks_invented_number`.
  - `test_ai_numeric_guard_allows_rounded`: context 316.99 and reply "317" pass.
  - `test_ai_budget_exhausted`.
  - `test_ai_provider_error_fallback`: Review Focus 4.
  - `test_ai_usage_logged`.
  - `test_bot_text_uses_ai_only_on_help_route`.
  - `test_ai_cannot_change_state`: a snapshot of all tables is identical except `llm_usage`, `last_inbound_at` and `acked_at`.
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3: Implement.** Add a README section saying that enabling AI sends stored site aggregates and the owner's message to the configured provider, and that this is off by default.
- [ ] **Step 4:** Run `pytest -q`. Expected: all pass.
- [ ] **Step 5:** Commit `feat: optional AI answers from stored data with budget and numeric guard`.

### Task 10: README and end-to-end check

**Files:**
- Modify: `README.md`, `.env.example`
- Test: `tests/test_acceptance.py` (add `test_a11_web_journey_end_to_end`)

**Interfaces:**
- Consumes: everything above.
- [ ] **Step 1: Write the test.** `test_a11_web_journey_end_to_end`:
  - Run the web journey (Task 6 flow) for the home site, then `/start <link_token>` through the real bot handler.
  - Then `evening_pull` and `evening_report` at the regression clock.
  - The owner receives the welcome messages and then a report whose text contains `85.0 קוט״ש`.
  - The admin page shows the owner as linked, with status `ממתין לתשלום`.
- [ ] **Step 2:** Run it. Expected: PASS. A failure means fixing the owning task's module, not weakening the test.
- [ ] **Step 3: Update the README.**
  - The web journey and the invite links.
  - The admin page (`ADMIN_PASSWORD`).
  - `TRUSTED_PROXIES` with the Caddy example.
  - The AI settings and their data note.
  - The low plan.
  - That the price is an estimate until `sites set --kwp` is used.
- [ ] **Step 4:** Run `pytest -q`. Expected: all pass. Print a6, a6b and the totals.
- [ ] **Step 5:** Commit `docs: web journey, admin, AI and low-plan setup; end-to-end acceptance`, then push.
