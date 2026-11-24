# Solya MVP 0: design

Date: 2026-11-24. Sources:
- the handover package (`solya-solar-handover-package`, sections referenced as §x.y);
- the recon brief at `docs/reference/2026-11-24-recon-brief.md`;
- SolarEdge's Basic Monitoring OpenAPI v2.2.0 at `docs/reference/solaredge/basic-monitoring-api.yaml`.

## 1. Intent

Solya is an AI agent ("Sol", feminine) that watches an owner's solar system and tells them, in short Hebrew messages, whether it is working. The messages are based only on real data, never guesses.

This MVP is **a codebase that can run on Oren's two real SolarEdge sites as soon as someone supplies three things**:
- a SolarEdge developer app client ID;
- that app's client secret;
- a Telegram bot token.

From there it grows to a pilot of about 20 owners (§5.1).

Why it matters:
- The clients want to own the code and leave the software house (§4.4).
- They need the V2 API working, because V1 is gone.
- They need cost under **$1 to $2 per customer per month** (§4.1).

Telegram comes first because it is free and needs no template approval. WhatsApp comes later, behind the same messaging interface.

## 2. Goals, non-goals, anti-goals

**Goals (MVP 0)**
1. Owner connects a SolarEdge site through V2 OAuth from a link the bot gives them. One approval covers one site, so an owner repeats it per site. Tokens are stored encrypted.
2. Ingest worker: pull data on a fixed, credit-budgeted schedule and **store it raw**. Everything downstream reads only the store (§4.1: pull once, analyse stored data).
3. A rules engine with no LLM. It computes:
   - daily production;
   - income in ₪;
   - communication loss;
   - zero production;
   - open alerts and inverter fault states.

   RAY v1 is a **pluggable slot**: an interface, the colour bands and kWp-weighting, wired into the card and text, but **disabled by default** until Oren's real formula or code is dropped in. See §5.5.
4. Evening report about 30 minutes after local sunset:
   - one combined **card image** from a fixed template (§3.4);
   - then the **text**.
5. Fault alert using the approved template (§3.4), and a communication-loss message (§2.6 rules).
6. A few **fixed** owner questions answered from stored data (today, yesterday, this month, open alerts). No LLM in MVP 0 (decided). An AI layer can be added later behind the same handler.
7. Credit ledger and budget guard, so we never exceed the SolarEdge quota. Log credits per site.
8. Acceptance tests from §5.4 as automated tests, with the 24 Nov figures as a regression test.

**Non-goals (explicitly out)**
- Payments and billing; the provider is open (§5.5).
- Landing page and marketing.
- Referral.
- Per-optimizer diagnosis: the API has no such data (recon §4).
- Quote review, upgrade advice, technician booking, opening service calls.
- Other inverter brands.
- The paid deep bill check.
- WhatsApp implementation: only the adapter seam now.
- Dashboard.
- Multi-tenant admin UI: CLI only.
- LLM or free-form Sol Q&A (decided: fixed questions only).
- Weather inputs (Open-Meteo). They are needed only for RAY, which is disabled until its formula arrives. This also avoids the paid commercial plan for now.

**Anti-goals (would be failure even if it "works")**
- Stating production or status when data is stale. A missing reading shown as 0. Guessing a cause (§2.6).
- Invented names. Text uses **משק / בית / סה״כ** or the owner-chosen label; the card uses the portal names (§2.7).
- Extra sentences in the daily summary, such as "system status", forecasts or signatures (§3.4).
- Any customer message triggering an account action (§4.2.4).
- Logging tokens or secrets, or storing owner passwords (rules out portal scraping, recon §4).
- Constant polling that multiplies cost (§4.1).
- Reopening the RAY formula without Oren (§2.4).

## 3. Constraints

- **SolarEdge V2 only.** These V1 paths do not exist in V2: `details`, `energyDetails`, `currentPowerFlow`, `inventory`, `alerts?status=`. The V2 endpoints used are listed in §5.3.
- **OAuth**
  - Authorize at `https://connect.solaredge.com/authorize`.
  - Token: `POST https://monitoringapi.solaredge.com/v2/oauth2/token` with a **JSON** body.
  - The access token lasts about 2 h.
  - The refresh token **rotates on every refresh** and expires after 14 days idle.
  - One grant = one site; the `site_id` arrives on the callback.
  - `/sites` (list) and `/alerts` (fleet) are API-key only, not OAuth.
- **Rate and credit limits**
  - Free tier: 2,000 credits a month and 10 calls per minute. Starter: 20,000 credits and 25 per minute.
  - Credits reset on the account's billing anniversary.
  - 1 credit per successful call. Token calls are counted too, conservatively.
- **History windows (per call)**

  | Resolution | Max window |
  |---|---|
  | QUARTER_HOUR | 12 h |
  | HOUR | 24 h |
  | DAY | 1 month |

  From 1 Dec 2026, fine-grained history only covers about 1 year.
- **No webhooks.** Fault detection is by polling.
- **Public HTTPS URL** is needed for the OAuth redirect. Telegram can use long polling, so no inbound port is needed for it.
- **Open-Meteo** (not used in MVP 0): the free tier is non-commercial. When RAY is enabled, the base URL and API key must be configurable.
- **Hebrew RTL** must render in Telegram and on the card (§3.4 RTL rules).

## 4. Approaches considered

| | Approach | Trade-off |
|---|---|---|
| **A (chosen)** | One Python service: FastAPI web (OAuth connect and callback, health), python-telegram-bot, APScheduler, SQLite. One container. | Simplest to run, own and debug. A few dollars a month to host. Enough for about 20 to 100 sites. Can be split later because the modules are already separated. |
| B | Separate web, worker and bot services with Postgres and a queue | Cleaner at scale, but more moving parts than a 20-user pilot justifies |
| C | Serverless functions with cron and a Telegram webhook | Nearly free hosting, but rotating refresh tokens plus stateless invocations make token loss more likely. Losing one means asking the owner to re-approve. |

The user picked **A**.

## 5. Design

### 5.1 Stack (builder's calls, stated)

- Python 3.12.
- `httpx` (async) for HTTP.
- FastAPI and uvicorn for the web.
- `python-telegram-bot` v22 for Telegram.
- APScheduler 3.x (AsyncIO) for scheduling.
- SQLAlchemy 2 on SQLite (Postgres-ready).
- `cryptography` Fernet for token encryption at rest.
- Pillow with Raqm for the card (needs `libfribidi0` in the image), plus a vendored OFL Hebrew font (Heebo or Noto Sans Hebrew).
- `astral` for sunset.
- pytest and respx for tests.
- Packaging: one Dockerfile and `docker-compose.yml`, configured with a `.env` file.

All of these run in one asyncio process: uvicorn serves FastAPI, and FastAPI's lifespan starts the PTB application (polling) and the scheduler.

### 5.2 Modules

```
src/solya/
  config.py         settings from env (pydantic-settings)
  db.py, models.py  SQLAlchemy models + session
  crypto.py         Fernet encrypt/decrypt for tokens
  solaredge/
    oauth.py        authorize URL, code exchange, refresh (rotation-safe)
    client.py       typed V2 calls; unit normalisation to Wh/W; null ≠ 0
    budget.py       credit ledger + per-minute limiter + monthly cap
  ingest.py         scheduled pulls → raw_responses + normalised rows
  rules/
    daily.py        per-site day figures, income, totals
    comms.py        staleness / communication-loss detection
    faults.py       alerts + inverterMode FAULT/LOCKED_* + missing inverter
    ray.py          RAY v1 (see §7)
  text/he.py        Hebrew templates (summary, fault, comm-loss, welcome, Q&A) + RTL helpers
  render/card.py    fixed-template PNG card
  messaging/
    base.py         OutboundMessage, Channel protocol
    telegram.py     Telegram implementation
  bot.py            Telegram handlers: /start, /connect, /today, /yesterday, /month, /alerts, /help
  web.py            FastAPI: GET /connect/{token} → redirect to SolarEdge; GET /oauth/callback; GET /healthz
  scheduler.py      job wiring (per-site sunset times, polls)
  cli.py            admin: status, credits, sites, send-report --dry-run, replay
  main.py           entrypoint
```

### 5.3 SolarEdge usage and schedule

| Purpose | Call | When | Credits per site per day |
|---|---|---|---|
| Metadata, `lastUpdateTime` | `GET /sites/{id}` | each pull | 1 to 2 |
| Day production, export, self-consumption | `GET /sites/{id}/overview?from&to` (local midnights as UTC `Z`) | each pull | 1 to 2 |
| Open alerts | `GET /sites/{id}/alerts?only-open=true&alerts-in-page=100` | each pull | 1 to 2 |
| Inverter fault state | `GET /sites/{id}/inverters/status?resolution=HOUR&from&to` | each pull | 1 to 2 |
| Devices (inverter serials) | `GET /sites/{id}/devices?types=INVERTER&types=METER` | at connect, then weekly | about 0.15 |
| Lifetime counter (bill check data) | `GET /sites/{id}/lifetime-energy` | evening | 1 |
| Token refresh | `POST /v2/oauth2/token` | once per pull session (the access token lives 2 h) | 1 to 2 |

**Schedule (follows handover §5.2).**
- **Evening pull:** one per site, at sunset + 25 min, feeding the report at sunset + 30 min.
- **Mid-day check:** one per site, for sites marked `commercial`. The default time is 12:30 local. It uses the same four calls and only sends a message for a new fault or communication loss.
- The mid-day check can be turned on or off per site, and its times are configurable (`check_times`), because the polling rate is Oren's decision (§5.5.1).

**Budget**

| | Per site per month |
|---|---|
| Home site | about 6 credits a day, about 185 |
| Commercial site | about 11 credits a day, about 330 |

- Oren's two sites come to about 515 credits a month, well under the free 2,000.
- At Starter's $0.005 per credit this is about $0.9 to $1.7 per site per month, inside the ceiling. More frequent polling should be a paid option (§4.1).
- The budget guard has a configurable monthly cap and billing anniversary day, and a per-minute limit (default 8, under the free tier's 10).
- It refuses mid-day checks once 90% of the cap is used, and keeps evening-report calls until 100%.
- Acceptance test 6 replays 30 days for one home and one commercial site and asserts the credit total against `max_credits_per_site_month` (default 350).

**Robustness**
- Every raw response is stored with its timestamp, endpoint and status, for replay and debugging.
- Analyses and Q&A never call the API.
- Timestamps are parsed in both bare (`YYYY-MM-DD HH:MM:SS`, site-local) and ISO-with-offset forms.
- Unit fields are normalised case-insensitively.

### 5.4 OAuth and token safety

0. **Invite-only pilot.** An admin creates an invite with `cli invite create`. That produces a link of the form `https://t.me/<bot>?start=<invite>`. `/start <invite>` registers the owner. `/connect` works only for registered owners; anyone else gets a polite "the pilot is by invitation" reply.
1. The owner sends `/connect` to the bot. The bot creates an opaque `connect_token` (`secrets.token_urlsafe`, 15 min TTL) bound to the chat, and replies with `https://<PUBLIC_BASE_URL>/connect/<token>`.
2. `/connect/<token>` redirects to `connect.solaredge.com/authorize`. It sends:
   - `client_id`;
   - `scope=SITE_DATA DEVICE_DATA`;
   - `redirect_uri`;
   - `state` and `external_id` set to the same nonce;
   - `access_duration` (configurable, default 24).
3. The callback correlates on `external_id`, then `state`, then the single pending token for that browser cookie. It **exchanges the code immediately**: the code is single-use and reported short-lived.
4. It then fetches `/sites/{site_id}` and stores the site with an **allowlist of fields**: name, peakPower, timezone, lat, lon, lastUpdateTime, activationStatus. It links the site to the chat and confirms in the bot with the portal name.
5. **Rotation safety**
   - A per-site asyncio lock serialises refreshes.
   - Refresh happens only within 5 minutes of access-token expiry.
   - The new pair is written in a DB transaction **before** the access token is used.
   - The old pair is kept in `previous_*` columns until the next successful data call. This allows recovery if the write succeeded but the response was lost.
   - A refresh 400 or 401 `invalid_grant` marks the site `reauth_needed`, sends the owner a reconnect link, and alerts the admin.
   - Tokens never appear in logs, and `repr` of the token objects is redacted.
6. **Keep-alive.** If a site has had no successful refresh in 7 days (for example, because polling was paused), the scheduler refreshes it anyway. This prevents the 14-day idle expiry.

### 5.5 Rules (deterministic)

- **Staleness (§2.6).** If `now - lastUpdateTime > stale_after` (default 60 min in daylight), the site is in COMM_LOSS:
  - The report for that site says communication was lost since `<time>`.
  - It states **no** production, status, RAY or income for that site.
  - After reconnection, the next pull checks whether history back-filled.
- **Missing vs zero.** A `null` value is unknown and never coerced to 0. Zero production in daylight with fresh data is a fault ("Zero output is a fault").
- **Income.** Per site `tariff_ils_per_kwh` and `income_basis ∈ {production, export}`. With the 24 Nov figures:
  - משק: production 316.990 kWh × 0.45 = 142.65 ₪.
  - בית: export 79.225 kWh × 0.48 = 38.03 ₪.
  - These match the handover figures exactly (§1.4), which is the regression test. Both tariffs are config, because they are unconfirmed (§2.2).
- **Faults**, from three sources:
  - open alerts from `/alerts`, deduplicated on `alertId`;
  - `inverterMode ∈ {FAULT, LOCKED_*}`;
  - an inverter serial expected from `/devices` but absent from `/inverters/status`.

  Each new fault gets exactly one alert message, then one reminder after `fault_reminder_hours` (default 3) if not acknowledged (§3.4).
  - **Acknowledgement:** any owner message in that chat after the alert marks all of that owner's notified faults as acknowledged, which only suppresses the reminder. This is message bookkeeping, not an account action, so it is the one allowed exception in acceptance test 10.
  - **Closed alert:** when SolarEdge closes an alert we notified about, Sol sends one short follow-up. It says SolarEdge reports the alert closed and asks the owner to confirm the fix was done. It does not claim the fault is repaired, because the handover notes closure is not proof of repair.
- **RAY slot.**
  - The interface is `RayCalculator.compute(site, day, stored_data) -> float | None`.
  - The default implementation returns `None`.
  - When it returns `None`, the card and text omit RAY entirely: no circle, no "RAY" word, and the layout reflows.
  - A real implementation plugs in via config (`RAY_IMPLEMENTATION=module:Class`). Band colours and kWp weighting are already implemented and tested.
- **Hedged cause.** A small table maps known codes to hedged Hebrew causes, for example `FAN_ALERT_1` → "ככל הנראה מדובר בתקלה במאוורר הקירור של הממיר". Unknown codes get no cause sentence: never guess.

### 5.6 Messages (Hebrew, templates, no LLM)

**Evening report: card first, then text.**

Card (PNG, fixed template, §3.4):
- green RAY circle at top centre, **only when RAY is enabled**;
- small grey labels, bold numbers;
- production on the right, ₪ on the left;
- one row per site with portal names;
- bottom row with total production and total income;
- one card for all of an owner's sites;
- RAY colours by band: green 90+, orange 80 to 89, yellow 60 to 79, red under 60;
- a COMM_LOSS site shows "אין תקשורת" instead of numbers.

Text, per §3.4 "personal report":

```
ערב טוב אורן,
משק: אין התראות פעילות. ‏RAY 92, ‏317.0 קוט״ש, ‏142.65 ₪
בית: אין התראות פעילות. ‏RAY 93, ‏85.0 קוט״ש, ‏38.03 ₪
סה״כ: ‏402.0 קוט״ש, ‏180.68 ₪
לכל שאלה אני כאן. המשך ערב טוב.
```

- Shown with RAY enabled. With RAY disabled, which is the MVP default, the "RAY nn," part is dropped: `משק: אין התראות פעילות. ‏317.0 קוט״ש, ‏142.65 ₪`.
- If any site has an open fault or communication loss, that line comes **first**, with the site name and the action.
- The closing becomes "להמשיך? מחכה לאישור שלך" only where a next step exists.
- RTL rule: lines start with a Hebrew word, and English (RAY) sits mid-sentence. Numbers are isolated with an RLM or FSI/PDI. A helper in `text/he.py` applies this and is unit-tested.

**Fault alert.** Exactly the §3.4 template:
- greeting by send time;
- detection time;
- code and serial;
- hedged cause, if known;
- "a certified professional is needed, not self-treatment";
- recommend opening a SolarEdge service call or a certified technician;
- "update me and I'll follow until resolved".

**Welcome.** The two §3.4 messages after the first site connects (draft text, which Oren has not yet approved; kept in one template file).

**Q&A.** These commands, and the matching free-text Hebrew keywords, are answered from the store only:
- `/today` (היום)
- `/yesterday` (אתמול)
- `/month` (החודש)
- `/alerts` (התראות)

Any other message gets a fixed reply: Sol can answer these questions now, and more is coming. It names no unbuilt feature. See §7, item 1.

**No action commands exist.** There is nothing for a message to trigger (§4.2.4). Disconnect is admin-only through the CLI.

### 5.7 Messaging seam

```python
@dataclass
class OutboundMessage:
    recipient: str
    kind: Literal["report", "fault", "comm_loss", "welcome", "reply"]
    text: str
    image_png: bytes | None
    dedupe_key: str
    template: str | None = None
    template_vars: dict | None = None
```

`Channel` protocol members:
- `caption_limit`
- `send(msg) -> Ok | RetryAfter(s) | Blocked | Invalid`
- `can_send_freeform(recipient)`, which is always True on Telegram
- `parse_inbound(...)`

Sends are recorded in `outbox` with a unique `dedupe_key`, so a restart never double-sends. WhatsApp later implements the same protocol with templates.

### 5.8 Data model (SQLite)

| Table | Columns |
|---|---|
| `owners` | id, display_name (default: Telegram first name; admin can override), telegram_chat_id, locale, created_at |
| `invites` | token, created_by, created_at, used_at, owner_id |
| `sites` | se_site_id, owner_id, portal_name, card_name (default portal_name; admin can shorten, e.g. "IL 360"), label_he (משק/בית; default for a single-site owner: "המערכת"), commercial (bool), peak_power_kwp_reported, billing_kwp_override, tz, lat, lon, tariff, income_basis, check_times, status (active/reauth_needed/paused), last_update_time |
| `site_tokens` | site_id, access_enc, refresh_enc, access_expires_at, previous_refresh_enc, refreshed_at, authorized_at, grant_expires_at |
| `connect_requests` | token, owner/chat, nonce, created_at, used_at |
| `raw_responses` | id, site_id, endpoint, params, status, body (json), fetched_at |
| `daily_metrics` | site_id, date, production_wh, export_wh, self_consumption_wh, income_ils, ray, comm_state, computed_at |
| `faults` | id, site_id, source, key, code, serial, first_seen, notified_at, reminded_at, acked_at, closed_at |
| `outbox` | dedupe_key, channel, recipient, kind, status, sent_at, error |
| `api_ledger` | ts, site_id, endpoint, http_status, credited (bool) |

The panel-power override (`billing_kwp_override`) exists because the portal kWp is known to be wrong for the farm: 65.52 vs a real 90.72 (§1.1). Expected-production maths uses the override when it is set.

### 5.9 Testing

- Unit tests run on fixture JSON shaped exactly like the spec examples. respx mocks SolarEdge.
- **Regression:** the 24 Nov fixtures reproduce 402.0 kWh and 180.68 ₪ total, with per-site figures as in §1.4.
- **Acceptance tests (§5.4) automated:**
  - 2: COMM_LOSS output contains no figures.
  - 3: a null reading is not rendered as 0.
  - 4: names.
  - 5: there are no extra lines.
  - 6: a 30-day replay of the schedule stays under the credit budget, and the cost per site is reported.
  - 7: the fault template has all required parts.
  - 8: RTL markers are present, and the card renders with Raqm (assert `features.check("raqm")`).
  - 9: rotation survives a simulated crash after the token response and before the DB commit, plus 14 days idle (keep-alive).
  - 10: no message triggers a state change except `/connect` link issuance.
- A `cli replay` command re-runs rules on stored raw data, with no API calls.

### 5.10 Setup flow for the real run (README)

1. Create a **new** developer account at developer.solaredge.com (decided). Do not reuse Oren's personal "Oren Home Monitor" app: its daily job rotates the same sites' refresh tokens, and the two would race. Do not use the software house's Solya account either. Register a "Site Access (OAuth)" app with SITE_DATA and DEVICE_DATA. Set the redirect `https://<host>/oauth/callback`.
2. Create a Telegram bot with BotFather.
2b. Run `cli invite create` and send Oren the link.
3. Fill in `.env`:
   - `SOLAREDGE_CLIENT_ID`
   - `SOLAREDGE_CLIENT_SECRET`
   - `TELEGRAM_BOT_TOKEN`
   - `PUBLIC_BASE_URL`
   - `TOKEN_ENCRYPTION_KEY`
   - `ADMIN_CHAT_IDS`
4. Run `docker compose up`. Put HTTPS in front, for example with Caddy, Cloudflare Tunnel, or a host's managed TLS.
5. Oren sends `/connect` to the bot twice, once per site, and approves each at SolarEdge.
6. `cli sites set-label`, `set-tariff` and `set-kwp` set משק, בית, 0.45 or 0.48, and 90.72.

## 6. Left to the builder

- Exact SQLAlchemy and migration tooling: `create_all` is fine for MVP.
- Logging format.
- Card pixel layout within §3.4.
- Font choice among OFL Hebrew fonts.
- CLI framework.
- Retry and backoff details, within the budget rules.

## 7. Decisions and open items

**Decided (24 Nov)**
- Approach A (single service).
- RAY is pluggable and disabled until Oren's formula or code is supplied.
- A new SolarEdge developer account is used for the MVP.
- Q&A is fixed questions only, with no LLM.

**Builder's calls, flagged for review**
- The schedule (evening pull plus commercial mid-day check).
- The fault acknowledgement semantics and the closed-alert follow-up.
- An invite-only pilot.
- Name defaults.

**Still open (do not block the build; all configurable)**
1. Tariffs 0.45 and 0.48 are inferred from the 24 Nov numbers. They match exactly, but should be confirmed.
2. The hosting target and domain for `PUBLIC_BASE_URL`.
3. Live verification of the UNVERIFIED items in recon brief §8, on first run.
4. The approved welcome text: the current text is the 1 Nov draft.
