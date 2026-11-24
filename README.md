# Solya

Evening monitoring for SolarEdge owners. The owner approves each site with OAuth, Solya stores the tokens encrypted, pulls once a day into Postgres, and sends a fixed card plus a short Hebrew summary on Telegram: RAY FACTOR, kWh, and income in shekels.

This is MVP 0, phase 1. WhatsApp is not connected. The messaging interface is there for it. Payments are a stub because no provider was chosen.

Without SolarEdge or Telegram credentials the app runs on the recorded 24 Nov 2026 figures:

| Site | Production | Income | RAY |
| --- | --- | --- | --- |
| Farm 3033498, אורן סופר | 316,990 Wh | 142.65 ₪ | 92 |
| Home 4552265, IL 360 | 85,039 Wh, export 79,225 Wh | 38.03 ₪ export | 93 |
| Total | 402.0 kWh | 180.68 ₪ | 92 weighted |

```bash
npm install
npm test
npm run sample
```

`npm run sample` prints the Hebrew summary and writes `docs/sample-card-2026-11-24.png`.

## Local demo

```bash
cp .env.example .env.local
# fill APP_SECRET, TOKEN_ENCRYPTION_KEY, ADMIN_PASSWORD, ADMIN_SESSION_SECRET, CRON_SECRET
# leave SOLAREDGE_CLIENT=fixture
npm run dev
```

Sign up with name, email, and phone. The next step loads the two recorded sites instead of calling SolarEdge. Pick them, and the ready page shows the card and the text. Admin is at `/admin`.

`npm run dev` keeps data in memory unless `DATABASE_URL` is set. A restart clears it.

## Deploy for free

Same shape as a small Next.js app: Vercel Hobby, Neon Postgres, and a scheduled hit on the app.

1. Create a Neon database and copy its connection string into `DATABASE_URL`.
2. Import this repo on Vercel Hobby. Set every variable from `.env.example`. Set `SOLAREDGE_CLIENT=live` only after the SolarEdge app exists. Set `APP_URL` to the Vercel origin.
3. Apply the schema once: `DATABASE_URL=... npm run migrate`.
4. `vercel.json` calls `/api/cron/daily` at 17:30 UTC. Vercel sends `Authorization: Bearer $CRON_SECRET`. The route does nothing until 30 minutes after local sunset, and the send window stays open for `SOLYA_SEND_WINDOW_HOURS` (default 6) so one daily cron still lands in the evening in both winter and summer clock time. It no-ops if that site was already pulled today.
5. For a send closer to sunset, add the GitHub Action in `.github/workflows/ingest.yml`. It needs repository secrets `APP_URL` and `CRON_SECRET` and runs hourly. The route still waits for sunset.

The early route `/api/cron/early` is alerts only. It does nothing while `SOLYA_EARLY_PULL=false`.

## SolarEdge developer app

Use a developer account that is not a customer's monitoring login. The free tier is 2,000 credits a month, 10 calls a minute, and one application. One successful data call is 1 credit.

1. Open [developer.solaredge.com](https://developer.solaredge.com/) and create an application with **Site Access** (OAuth), not a fleet API key.
2. Register the redirect URI exactly: `https://<your-domain>/api/solaredge/callback`.
3. Enable scopes `SITE_DATA` and `DEVICE_DATA`.
4. Put the client id and secret in `SOLAREDGE_CLIENT_ID` and `SOLAREDGE_CLIENT_SECRET`. Put the same redirect URI in `SOLAREDGE_REDIRECT_URI`.
5. The owner opens **חיבור ל-SolarEdge**, signs in, and approves one site. The callback receives `code` and `site_id` (and usually `external_id`; `state` is not always echoed). Repeat for each site. Tokens are per site.
6. The token endpoint is `POST https://monitoringapi.solaredge.com/v2/oauth2/token` with a JSON body, not a form. Access tokens last about two hours. The refresh token rotates on every refresh and must be stored before the next call. A failed refresh keeps the previous pair and does not delete stored production.

V1 (`/site/{id}` plus `api_key`) is not used. SolarEdge set it to close on 3 Nov 2026.

Daily pull, three credits per site:

- `GET /v2/sites/{id}/energy?from&to&resolution=DAY&unit=WH`
- `GET /v2/sites/{id}/meters/telemetry?from&to&resolution=DAY&unit=WH`
- `GET /v2/sites/{id}/alerts?only-open=true&alerts-in-page=100`

`timeUnit`, `startDate`, and `startTime` are not sent. Those names ignored the requested day during the original testing. Parameter names were checked against the Monitoring API V2 notes published from api-docs.solaredge.com (OpenAPI 2.0.1) and against owner-authorization traces from Nov 2026. The docs site itself is a client-rendered SPA, so a live click-through of every field still belongs with the first real app.

A null bucket is a missing reading. It is never stored or shown as 0 Wh. A numeric 0 is a fault.

Portal peak power is stored and labeled untrusted. The farm registry says 65.52 kWp; the invoice is 90.72. Billing and the RAY weight use `installed_kw_dc` only, and only after it is saved on the site. A new live site has no tariff and no installed kW, so the summary omits income and the price stays unset.

## Telegram bot

1. In BotFather, `/newbot`, and copy the token into `TELEGRAM_BOT_TOKEN`. The username, without `@`, goes in `TELEGRAM_BOT_USERNAME`.
2. Set a webhook secret and the same value in `TELEGRAM_WEBHOOK_SECRET`:

```bash
curl -X POST "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/setWebhook" \
  -d "url=$APP_URL/api/telegram/webhook" \
  -d "secret_token=$TELEGRAM_WEBHOOK_SECRET"
```

3. The ready page links to `https://t.me/<bot>?start=<token>`. `/start` binds that chat and sends the two welcome messages. Later messages can ask for production, income, RAY, alerts, or the last pull. Answers come from the stored summary. Anything else is refused unless `SOLYA_LLM_ENABLED=true` and both `OPENROUTER_API_KEY` and `OPENROUTER_MODEL` are set, and the customer is still inside `SOLYA_LLM_MONTHLY_TOKEN_BUDGET`.

Chat cannot cancel, refund, delete, change tokens, or open a service call. That stays true when `SOLYA_MAY_OPEN_SERVICE_CALLS=true`. The flag does not give the model a tool.

## Decisions left open

These are env vars, not choices baked into the code. See handover section 5.5.

| Question | Variable | Default |
| --- | --- | --- |
| Polling rate, and who pays for faster faults | `SOLYA_EARLY_PULL`, `SOLYA_MAX_CREDITS_PER_CUSTOMER_MONTH` | off, 400 credits |
| Payment provider and VAT | `SOLYA_PAYMENT_PROVIDER`, `SOLYA_VAT_MODE`, `SOLYA_VAT_RATE` | `stub`, `unset`, empty |
| Canonical domain | `SOLYA_CANONICAL_DOMAIN` | empty |
| May Sol open service calls | `SOLYA_MAY_OPEN_SERVICE_CALLS` | `false` |
| Does the RAY weather term stay | `SOLYA_RAY_INCLUDE_WEATHER` | `true`, matching frozen v1 |

Tariffs are per site. The fixture uses 45 agorot on farm production and 48 agorot on home export because that is the arithmetic of the 24 Nov shekels. The handover still marks that mapping unconfirmed, so new sites do not inherit it.

## RAY v1

Weights are performance 40, health 35, environment 25. Performance is actual ÷ expected, capped at 100. An open alert costs 25 health points. A measured zero scores 20 and is described as a fault. Communication loss produces no score and no production figure. Environment drops 0.4 points per percent of cloud cover, which is about 5.2 RAY points at 52.3% cloud. Unknown cloud cover is not treated as clear and is not invented; the term is skipped.

The 24 Nov composite is 92 and 93, and the farm hit the performance cap. The handover did not publish the component split. The fixture uses shared 70% cloud and a 4-point farm soiling gap so the frozen weights land on those two numbers. That split is a reconstruction.

## Tests

`npm test` covers the rules engine and the section 5.4 checks that can run without a portal login: the 24 Nov figures, the communication-loss wording, null versus zero, the fault-alert template, refresh rotation and a failed refresh that keeps stored rows, and inbound messages that cannot reach account actions.

`npm run build` is the production build.

## Out of scope

Per-optimizer data, quote review, referrals, other inverter brands, WhatsApp/Twilio sending, and taking payment. The adapter and the payment interface are the extension points.
