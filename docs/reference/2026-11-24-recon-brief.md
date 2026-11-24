# Solya MVP recon brief: SolarEdge V2, Telegram, card, weather (2026-11-24)

**Labels.** VERIFIED(PS) means stated in SolarEdge's own Basic or Advanced Monitoring OpenAPI spec. We have it as a copy (v2.2.0 / v2.0.1) from https://github.com/EVWorth/solaredge `specs/`. VERIFIED-CODE means seen in a working open-source client. DOC-CITED means a repo quotes an official page that we could not open ourselves. UNVERIFIED means everything else.

None of the SolarEdge hosts were reachable in any research session (api-docs, developer, connect, monitoringapi). Neither were core.telegram.org and open-meteo.com.

**Scope rule.** V1 is dead. It returns 503 or 410, with a retirement date of 1 Nov or 3 Nov depending on the source. Build no V1 fallback. The V1 names `details`, `energyDetails`, `powerDetails`, `currentPowerFlow`, `inventory`, `meters`, `alerts?status=OPEN`, `timeUnit` and `startDate` do not exist in V2.

## 1. SolarEdge V2 auth (Site Access OAuth 2.0, authorization code)

**Setup**
- The owner or developer creates an account at https://developer.solaredge.com/. Choose "Set up my company's developer account"; the Free tier activates automatically.
- Register the app as "Site Access (OAuth)" with the `SITE_DATA` and `DEVICE_DATA` permissions and a Redirect URI. Source: openHAB README.
- Consent must be given with the **site owner's** account (HA PR #187939 testers).
- Fleet Access (`X-API-Key`) is for installer accounts only and is not for us.

**Authorize.** The host is VERIFIED(PS) as `https://connect.solaredge.com/authorize`. Send:
```
https://connect.solaredge.com/authorize?client_id=<id>&scope=SITE_DATA%20DEVICE_DATA&redirect_uri=<urlencoded exact registered URI>&access_duration=24&external_id=<nonce>&state=<nonce>
```

| Param | What we know |
|---|---|
| `client_id` | VERIFIED |
| `scope` | VERIFIED(PS) |
| `access_duration` | Grant length in months, max 24. Community clients and doc citations only, NOT in the PS: UNVERIFIED |
| `external_id` | Same status as `access_duration` |
| `state` | May not be echoed back on the callback |
| PKCE | Optional or ignored. openHAB works without it; HA PR #187939 sends S256. Do not depend on it. Enforcement is UNVERIFIED |
| `response_type=code` | UNVERIFIED |

**Callback.** It arrives at `redirect_uri` with `code` and `site_id`, plus `external_id`/`state` if they were sent, or `error`/`error_description`.
- Correlate on `external_id`, falling back to `site_id`.
- Exchange the code immediately. One user reports it expires in about 1 minute (UNVERIFIED). Codes are single-use.

**Token.** VERIFIED-CODE: openHAB `SolarEdgeOAuthClient.java`. The tokenUrl and refreshUrl are VERIFIED(PS).
```
POST https://monitoringapi.solaredge.com/v2/oauth2/token
Content-Type: application/json
{"grant_type":"authorization_code","code":"…","client_id":"…","client_secret":"…"}
{"grant_type":"refresh_token","refresh_token":"…","client_id":"…","client_secret":"…"}
```
- The body is JSON, not form-encoded. There is no Basic auth, and `redirect_uri` is not needed.
- Response: `access_token`, `refresh_token`, `token_type` ("Bearer"), `expires_in` (7200). There is no `site_id` and apparently no `scope`.
- Revoke: `POST /v2/oauth2/revoke-token` with `{"token":"…"}` (DOC-CITED).
- Errors follow RFC 6749: HTTP 400 `{error, error_description}`. `invalid_client` means bad credentials. `invalid_grant`/`invalid_request` means the code or token was already used or has expired.

**Lifecycle and rules**
- **Rotation.** Every refresh returns a NEW refresh token and invalidates the old one.
  - Persist the new pair atomically before using it.
  - Lock refreshes per site.
- **Refresh timing.** Refresh when the token is within 60 s of expiry, or at about 100 min. On a data-call 401, refresh once and retry.
- **Refresh failure.** A refresh that returns 400 or 401 is terminal (openHAB behaviour). Mark the site re-auth-needed and send the owner a new consent link via Telegram.
- **Refresh-token idle expiry is 14 days or 30 days** (sources conflict). Design for 14; a daily refresh makes it moot.
- **Grant lifetime.** The grant lasts `access_duration` months, max 24. Store `authorized_at` and warn about 2 weeks before it runs out.
- **One grant = one site** (single `site_id` on the callback). Store one token pair per site.
- **Data calls** use `Authorization: Bearer <access_token>`.
- **Endpoints closed to OAuth.** `GET /sites` (list) and `GET /alerts` (fleet) are API-key only, VERIFIED(PS). Site discovery is the callback `site_id`.
- **Scopes.** The PS gives no per-endpoint scope mapping (every scope list is empty). Request both.
- **Logging.** Never log tokens or secrets, including in URLs (HA issue #188395).

## 2. V2 data endpoints

Base URL: `https://monitoringapi.solaredge.com/v2`. VERIFIED(PS) unless marked otherwise.

**Conventions**
- **Request times.** `from`/`to` are ISO-8601 with `Z`. A timestamp without an offset returns 400 `Failed to convert … [Instant]`; URL-encode `+` if you send an offset.
  - Omitting them gives local midnight to now. That breaks QUARTER_HOUR after 12:00, so always send explicit windows.
- **Response times.** Timestamps use the site-local offset and mark the **start** of the bucket.
- **Day boundaries.** Compute them from `location.timezone` (Asia/Jerusalem).
- **Units.** Upper case: `WH|KWH|MWH|GWH` and `W|KW|MW`. Advanced power-flow uses "kW", so compare case-insensitively. Conversion factors: WH 1, KWH 1e3, MWH 1e6, GWH 1e9.
- **Measurements envelope:** `{"period":{"from","to"},"unit","resolution","values":[{"timestamp","value":float|null}]}`.
- **Max window per resolution** (VERIFIED(PS); over the limit returns 400):

  | Resolution | Max window |
  |---|---|
  | QUARTER_HOUR | 12 h |
  | HOUR | 24 h |
  | DAY | 1 month |
  | WEEK | 1 year |
  | MONTH | 3 years |
  | YEAR | unlimited |
  | TOTAL | a single value |

- **History limit.** From 1 Dec 2026, QUARTER_HOUR and HOUR data is limited to the last 1 year. DAY and coarser keep full history.
- **Errors.**
  - 400, 403 and 404 return `application/problem+json` (`type, title, status, detail, errors, traceId`).
  - 401 and 429 return a gateway body: `{message, request_id}` or `{error, error_description}`.
  - 403 means no site permission, missing scope or a tier gate. Re-auth will not fix it, so alert the operator.
  - 404 can also mean no access to the site.
- **Billing.** 1 credit per call; error calls are not charged (advanced spec). There are no webhooks, so poll.

| Path | Params | Returns / notes |
|---|---|---|
| `GET /sites/{id}` | — | `SiteMetadataExtended`, **unwrapped**: `siteId, name, accountId, peakPower (kWp), installationDate, lastUpdateTime, location{…, latitude, longitude, timezone}, activationStatus (ACTIVE\|PENDING), note, ptoDate`. No `type` or `primaryModule` (those are V1; PR #187939 is wrong there). Allowlist fields before storing because the response may include owner contact details. `lastUpdateTime` staleness is a comms-loss signal |
| `GET /sites/{id}/overview` | `from`, `to` | `{siteId, production{total, unit, toSelfConsumption, toStorage, toGrid}, consumption{total, unit, fromPv, fromStorage, fromGrid}}`. Each block has its own unit, sub-fields are nullable, and consumption needs a meter. **Single call for the daily summary.** The window cap is undocumented (UNVERIFIED live) |
| `GET /sites/{id}/overview/performance` | `from`, `to` | `performance{specificYield (kWh/kWp), performanceRatio}` |
| `GET /sites/{id}/energy` | `from`, `to`, `resolution` (default QUARTER_HOUR), `unit` | Envelope. Net PV (battery-adjusted). DAY with a window of 1 month or less gives month-to-date |
| `GET /sites/{id}/lifetime-energy` | none | `{energy, unit, lastUpdateTime}` from the onboard counter. May exclude replaced devices |
| `GET /sites/{id}/power` | `resolution` QUARTER_HOUR\|HOUR, `unit` | Envelope. The latest 15-min average, not instantaneous power |
| `GET /sites/{id}/meters/telemetry` | `from`, `to`, `resolution` | `meters{<SN>{productionPower/Energy, consumption…, import…, export…{unit, values}}}`. DAY and coarser return energy only. 404 means no meter. Meter type comes from `devices.meterType` |
| `GET /sites/{id}/inverters/status` | `resolution` QUARTER_HOUR\|HOUR, max 12 h / 24 h | `inverters{<SN>{powerLimit %, operationMode, inverterMode (string: MPPT, STARTING, FAULT, STANDBY, LOCKED_*), temperature °C}}`. **A missing SN is itself a fault signal.** (Correction: mode and temperature were NOT dropped in V2) |
| `GET /sites/{id}/storage/telemetry` | `from`, `to`, `resolution` | Returns `"storage":{}` when there is no battery |
| `GET /sites/{id}/devices` | `types` repeated (`types=INVERTER&types=METER&types=BATTERY`); default INVERTER only | Bare array of `DeviceData`: `type, serialNumber, model, firmware, active, meterType, form (virtual meters have no serial), connectedOptimizers…`. Filter on `active`. Repeated-param encoding is UNVERIFIED live |
| `GET /sites/{id}/alerts` | `page`, `alerts-in-page` (1–100), `only-open` (default true). No status or severity filter | Bare array: `alertId, siteId, category (may be absent), type, impact 1–9, status OPEN\|CLOSED, firstTrigger, lastTrigger, closeDate, component{name, serialNumber}, errorCodes[]`. Timestamps may be `"2026-09-28 23:07:18"` (no T, no offset) or ISO with an offset, so parse both and treat the bare form as site-local. Dedupe on `alertId` |
| `/sites/{id}/power-flow`, `/energy/per-destination` | — | **Pro/Enterprise only; 403 on our tier** |

Mapping for our model: code → `errorCodes[]`, severity → `impact`, openedAt → `firstTrigger`, serial → `component.serialNumber`.

**Rate limits**
- Limits apply per developer account: a monthly credit quota plus calls per minute (CPM).
- Free tier: 2,000 credits/month, 10 CPM, one application. This is DOC-CITED and not in the PS ("contact your account manager"). Starter at 25 CPM is UNVERIFIED.
- Burst limit: about 5 calls in a few seconds got 429 (observed). Space calls ≥6 s apart and stay at ≤8 per minute.
- 429 headers are documented as `x-ratelimit-{limit,remaining}-{minute,month,second}` plus `retry-after`. Only the minute pair has been observed in practice.
- A 429 with `remaining-minute > 0` and no `retry-after` means the month is exhausted.
- **Monthly credits reset on the account's billing-cycle anniversary, not on the 1st** (PS). Keep a persistent local ledger: a rolling 32-day window plus 10 per 60 s, counting token calls too (as PR #188074 does).

**Budget for 2 sites (INFERRED)**
- Hourly alerts: 1,440 calls/month.
- Daily overview, status and details: about 180 calls/month.
- Total about 1,620, which fits under 2,000.
- Alerts every 15 min would be 5,760 and does not fit.
- Cache details and devices for 1–7 days, and persist next-due times so restarts do not refetch.

## 3. Lessons from Home Assistant

- **Merged HA code is V1-only and broken.** Do not copy its endpoints or its 15-minute cadence, which is about 9,000 calls/month. On every restart it also refetches everything (about 6 calls). Persist cache state instead.
- **V2 references:**
  - PR #187939 (open): https://github.com/home-assistant/core/pull/187939. Overview 2 h, details and devices 24 h, storage 4 h. Its "block until 1st of month" logic and its `type`/`primaryModule` fields are wrong.
  - PR #188074: shared budget ledger, 1,600 calls per 32 days, pause ≥1 h on 429.
  - aiosolaredge PR #151: computes period boundaries in the site TZ and maps errors (401 → auth, 403 → permission).
- **Parsing rules to keep:**
  - Null means unknown, never 0.
  - Ignore a lifetime value of 0 when the day, month or year value is > 0 (HA #59285).
  - A missing meter means "no meter", not an error.
  - A non-active `activationStatus` means the site is inactive.
  - Check that the response `siteId` matches the request.
  - Read each block's own unit.
  - Sign convention: negate grid on export and storage on charge.
- **Synthetic power flow:** net grid = meter import − export (PR #187939).

## 4. Per-optimizer data

- **The V2 API has no optimizer telemetry or energy endpoint.** `/devices?types=OPTIMIZER` gives inventory only, and `connectedOptimizers` gives a count. The per-optimizer gap is VERIFIED(PS) by absence.
- HA's module statistics use `solaredge-web` 0.5.1 (https://github.com/Solarlibs/solaredge-web). It scrapes the portal login (`login.solaredge.com` PKCE under the website's own client_id, HTML form scraping), stores the owner's **plaintext password**, re-logs in every hour, and calls undocumented `monitoring.solaredge.com/services/layout/...` endpoints (e.g. `playback/.../optimizers-compact`, max 8 days).
  - **Not viable for Solya.** It violates the no-passwords rule, has a likely ToS problem, and is fragile: no MFA handling, payload and ID schemes have already changed.
  - There is no `solaredge_modules` component; it is part of `solaredge`.
- Modbus or local access gives no optimizer data either (INFERRED).
- **MVP decision:** leave optimizer analysis out. Use site-level, inverter-status and alerts data for the rules engine.

## 5. Telegram, and notes for a future WhatsApp adapter

- **Library.** Use `python-telegram-bot[job-queue,rate-limiter]~=22.8` (Python ≥3.10, Bot API 10.0; the current Bot API is 10.5, and we need nothing newer). aiogram 3.34 is an alternative but has no job queue.
  - Keep the SolarEdge scheduler (one APScheduler for the whole app) separate from the bot, and treat Telegram as an output adapter.
- **Polling.** Long polling (`run_polling`) is fine. The OAuth callback still needs a public HTTPS endpoint. A webhook needs a `secret_token` (header `X-Telegram-Bot-Api-Secret-Token`), and getUpdates does not work while a webhook is set.
- **Deep link.** `https://t.me/<bot>?start=<payload>`, payload `[A-Za-z0-9_-]`, max 64 characters. Use an opaque token such as `secrets.token_urlsafe(32)` mapped server-side to site and owner. In PTB the payload is in `ctx.args[0]`. VERIFIED-SRC.
- **Limits (VERIFIED-SRC):**
  - sendMessage text: 4,096 characters.
  - sendPhoto caption: 1,024 characters after entity parsing.
  - Photo: ≤10 MB, width + height ≤ 10,000, aspect ratio ≤ 20.
  - Bytes are accepted directly.
  - About 1 msg/s per chat and 30 msg/s globally.
  - If the text exceeds the caption limit, send the photo with a short caption, then a follow-up text.
- **HTML parse mode.** Escape with `html.escape(s, quote=False)`. An unsupported tag returns 400.
- **Errors (UNVERIFIED).** A 429 carries `retry_after` (`RetryAfter`). A 403 "bot was blocked" (`Forbidden`) means mark the recipient inactive.
- **RTL (UNVERIFIED).** Start each line with a Hebrew letter, or prefix it with U+200F RLM. Isolate numbers and units with FSI/PDI. Test on iOS, Android and Desktop.
- **WhatsApp (VERIFIED-SEARCH):**
  - Billing is per message.
  - Business-initiated messages outside the 24 h window need approved Utility templates (Meta may reclassify a template as Marketing).
  - Service messages are paid since 1 Oct 2026, with 1,000/month free.
  - Images: JPEG or PNG ≤5 MB, caption ≤1,024. Cloud API uses a media upload and then the id. Twilio needs a public MediaUrl and charges $0.005 plus Meta's fee.
- **Adapter interface.** A channel-neutral `OutboundMessage{recipient, kind, text_html, image_png, template, dedupe_key}`. The adapter exposes `caption_limit`, `send() -> ok|retry_after|blocked|invalid`, `can_send_freeform()` (always true on Telegram; inside the 24 h window on WhatsApp) and `parse_inbound()`. Store `last_inbound_at` per recipient.

## 6. Card rendering

**Use Pillow 13.0.0 with Raqm.**
- The wheels include HarfBuzz but dlopen FriBiDi at runtime, so add `apt-get install -y --no-install-recommends libfribidi0` to the Dockerfile.
- Load fonts with `ImageFont.truetype(path, size, layout_engine=ImageFont.Layout.RAQM)` and draw with `direction="rtl", language="he", anchor="ra"` (or `"rm"`) at the right margin.
- Mixed Hebrew, digits and % rendered correctly in a local test.
- **Assert `PIL.features.check("raqm")` at startup and in a test.** If Raqm is missing, Pillow silently falls back to BASIC layout and renders Hebrew reversed.

Fallback and rejected options:
- **Fallback:** BASIC layout plus `bidi.algorithm.get_display` (the pure-Python version). The default `bidi.get_display` in 0.6.12 does not mirror brackets.
- **Rejected:**
  - CairoSVG has no bidi.
  - resvg_py works, but needs `text-anchor="end"` and `font_dirs`.
  - Playwright/Chromium adds about 300–500 MB, which is overkill.

**Fonts.** Vendor Heebo, Rubik or Noto Sans Hebrew static TTFs (OFL 1.1) with `OFL.txt`. Re-check the licence when vendoring (UNVERIFIED here). DejaVu Sans is the last resort and is verified to work.

## 7. Weather and sunset

**Sunset: use astral 3.2** (VERIFIED, run locally; DST correct).
```python
sunset(Observer(lat, lon), date=d, tzinfo=ZoneInfo("Asia/Jerusalem"))
```
Tel Aviv on 2026-11-24 gives 16:37 +02:00. The image needs tzdata (or `pip install tzdata`).

**Open-Meteo** (from search snippets only; re-check live):
- Forecast: `GET https://api.open-meteo.com/v1/forecast?latitude&longitude&hourly=cloud_cover,shortwave_radiation&timezone=Asia/Jerusalem`. The response has parallel arrays under `hourly`. `shortwave_radiation` is W/m², averaged over the preceding hour. `daily=sunset` and `shortwave_radiation_sum` are UNVERIFIED.
- Air quality (dust): `GET https://air-quality-api.open-meteo.com/v1/air-quality?hourly=pm10,pm2_5` (CAMS; the global domain, about 45 km, covers Israel).
- **The free tier is non-commercial only.** Solya is commercial, so plan for the Standard plan (about $29/month, UNVERIFIED). Make the base URL and `apikey` configurable (customer-api host, INFERRED).
- Data is CC BY 4.0 and attribution is required where it is displayed.

## 8. Still UNVERIFIED: confirm against the live API

1. Whether `access_duration`, `external_id`, `state` and `response_type` are accepted on authorize, and whether `state`/`external_id` are echoed back.
2. Whether PKCE is enforced or ignored, and the exact `redirect_uri` matching rules.
3. Authorization-code lifetime (reported as about 1 minute).
4. Refresh-token idle expiry: 14 days or 30 days. Whether the token response includes `scope`.
5. Whether token calls consume credits. Free-tier figures (2,000 credits, 10 CPM), burst behaviour, and whether the month and second headers are actually sent.
6. Whether one consent can cover several sites (expected: no).
7. Whether overview accepts a full previous-day `from`/`to` window, and its cap.
8. Repeated `types=` encoding on `/devices`.
9. Per-endpoint scope needs (`SITE_DATA` vs `DEVICE_DATA`).
10. Alert timestamp format in live responses, and whether `category` is present.
11. `inverters/status` field presence on our sites' inverter models.
12. Which "migrating V1 to V2" doc page is current:
    - https://api-docs.solaredge.com/docs/basic-monitoring-api/m32bx376ka8mb-migrating-from-v1-to-v2
    - https://api-docs.solaredge.com/docs/developer-platform/mxj41549n0nxk-migrating-from-v1-to-v2
13. Open-Meteo terms, pricing, commercial host and `daily=sunset`; Telegram HTML tag list and webhook ports.

**Key official URLs to open once the hosts are allowed:**
- Auth: https://api-docs.solaredge.com/docs/developer-platform/g09jmvp5t2cok-authentication
- Homeowner guide: https://api-docs.solaredge.com/docs/developer-platform/df8kx5j4z9vi0-homeowner-and-self-access-guide
- Tiers: https://api-docs.solaredge.com/docs/developer-platform/pwh8p4k2wtajh-tiers-and-rate-limits
- Polling: https://api-docs.solaredge.com/docs/basic-monitoring-api/2eb2zn9wi9y2p-aggregation-and-polling-strategy

**Local spec copies** (scratchpad, can be cleared; copy them into the repo):
- /tmp/claude-0/-home-user-solya/1cf2dc84-a0a7-5949-88b7-00c74bdd583b/scratchpad/gh/ev/specs/basic-monitoring-api.yaml
- /tmp/claude-0/-home-user-solya/1cf2dc84-a0a7-5949-88b7-00c74bdd583b/scratchpad/gh/ev/specs/advanced-monitoring-api.yaml
