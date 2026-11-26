# Solya

Solya watches SolarEdge solar sites through the SolarEdge Monitoring **V2** API and talks to the site owner in Hebrew on Telegram.

- Each owner connects a site from a link the bot sends them (V2 OAuth, one approval per site). Tokens are stored encrypted.
- A credit-budgeted schedule pulls data and stores the raw responses. Everything downstream reads the stored data.
- A rules engine with no LLM computes daily production, income in ₪, communication loss, zero production, open alerts and inverter faults.
- The evening report is a card image plus a short text, sent about 30 minutes after the owner's latest local sunset. Faults, communication loss and reminders arrive as separate messages.
- Owners can ask a few fixed questions (today, yesterday, this month, open alerts).
- Owners can also join through an invite-gated Hebrew web journey: sign up, connect the SolarEdge site, see the price, a payment stub, and a Telegram deep link that links the chat (see "Web journey" below).
- Admin work is done with the `solya` command line tool. A read-only page at `/admin` (password from `ADMIN_PASSWORD`) lists owners, sites and usage. Logout revokes the admin cookie in memory only: after a restart a copied cookie stays valid until it expires (12 h).

RAY is a pluggable slot. While `RAY_IMPLEMENTATION` is unset it is **disabled** and no RAY circle or "RAY" word appears anywhere. See "RAY" below.

## Setup for the real run

Do these steps in order.

1. **Create a new developer account** at developer.solaredge.com. Do not reuse Oren's personal "Oren Home Monitor" app: its daily job rotates the same sites' refresh tokens, and the two would race. Do not use the software house's Solya account either.
2. **Register an app of type "Site Access (OAuth)"** with the scopes `SITE_DATA` and `DEVICE_DATA`.
3. **Set the redirect URI** to `https://<your-host>/oauth/callback`, and make it the app's **default** redirect URI. SolarEdge ignores the other entries and sends the owner to the default one (observed live on 26 Nov: owners landed on an old URL). `<your-host>` is the public HTTPS host from step 8. Note the client id and client secret.
4. **Create a Telegram bot with BotFather** (`/newbot`). Note the bot token and the bot username (without the `@`).
5. **Fill in `.env`.** Copy the template and edit it:

   ```bash
   cp .env.example .env
   ```

   | Variable | Value |
   | --- | --- |
   | `SOLAREDGE_CLIENT_ID` | from step 2 |
   | `SOLAREDGE_CLIENT_SECRET` | from step 2 |
   | `TELEGRAM_BOT_TOKEN` | from step 4 |
   | `TELEGRAM_BOT_USERNAME` | from step 4, without the `@` |
   | `PUBLIC_BASE_URL` | `https://<your-host>`, no trailing path |
   | `TOKEN_ENCRYPTION_KEY` | from step 6 |
   | `ADMIN_CHAT_IDS` | JSON list of Telegram chat ids that get admin notices, for example `[123456789]` |
   | `ADMIN_PASSWORD` | optional. Password for the read-only `/admin` page. While unset, `/admin` returns 404. Example: `ADMIN_PASSWORD=pick-a-long-passphrase` |
   | `SESSION_SECRET` | optional. Signs the web session cookies. Defaults to a value derived from `TOKEN_ENCRYPTION_KEY`. Example: the output of `openssl rand -hex 32` |
   | `TRUSTED_PROXIES` | optional. Reverse-proxy IPs whose `X-Forwarded-For` is trusted, as a JSON list or comma-separated. Default `["127.0.0.1"]`. See step 8. |
   | `LLM_*` | optional AI answers, off by default. See "Optional AI answers". |

   `.env` holds secrets. It is git-ignored and excluded from the Docker image. Never commit it or paste it into chat.
6. **Generate the encryption key** with `solya genkey` and put it in `.env` as `TOKEN_ENCRYPTION_KEY`. It does not need a configured `.env`:

   ```bash
   docker compose build
   docker compose run --rm solya solya genkey
   ```

   Keep a safe copy of the key. If it is lost, every stored token is unreadable and each site must be connected again. If you have a local checkout with the package installed, plain `solya genkey` prints the same thing.
7. **Start the service:**

   ```bash
   docker compose up -d
   ```

   The database lives in `./data` (a SQLite file, mounted at `/app/data`). Back that folder up. Logs: `docker compose logs -f solya`. Health check: `GET /healthz` (on the host, `curl http://127.0.0.1:8000/healthz`: the port is published on loopback only, see step 8).
8. **Put HTTPS in front of port 8000.** The OAuth callback must be reachable on a public HTTPS host. Any of these works: Caddy, Cloudflare Tunnel, or a host's managed TLS. A minimal Caddy setup:

   ```
   solya.example.com {
       reverse_proxy localhost:8000
   }
   ```

   Caddy sets `X-Forwarded-For` itself. The service resolves the client IP from that header only when the connecting peer is in `TRUSTED_PROXIES`, and the admin login lockout and the sign-up rate limits count per client IP.

   **Under Docker Compose the default `["127.0.0.1"]` is wrong.** Caddy's connection to `localhost:8000` reaches the container through Docker's port proxy, so inside the container the peer is the compose network's gateway (for example `172.18.0.1`), not `127.0.0.1`. That peer is not trusted, `X-Forwarded-For` is ignored, and every visitor gets the gateway's address as their client IP. They then share one rate-limit bucket: five wrong admin passwords from anyone lock the real admin out for 15 minutes, and ten sign-up or `/join` posts an hour from anyone block every sign-up. Pick one of these setups:

   - **Caddy on the host.** The shipped `docker-compose.yml` already publishes the port on loopback only, so clients cannot bypass Caddy and reach the service directly. Keep it that way:

     ```yaml
         ports:
           - "127.0.0.1:8000:8000"
     ```

     After `docker compose up -d`, find the network's gateway (the network is `<project>_default`, where the project is the folder name, for example `solya_default`):

     ```bash
     docker network inspect solya_default --format '{{(index .IPAM.Config 0).Gateway}}'
     ```

     Put that address in `.env`, for example `TRUSTED_PROXIES=["172.18.0.1"]`, then run `docker compose up -d` again. The gateway can change if the network is removed and recreated. To keep it fixed, pin the subnet at the top level of `docker-compose.yml` and trust that gateway (`TRUSTED_PROXIES=["172.30.0.1"]`):

     ```yaml
     networks:
       default:
         ipam:
           config:
             - subnet: 172.30.0.0/24
               gateway: 172.30.0.1
     ```

   - **Caddy in the same compose file.** Add Caddy as a service on the same network with `reverse_proxy solya:8000`, give it a fixed address (pin the subnet as above and set `ipv4_address` under the Caddy service's `networks: default:`), list that address in `TRUSTED_PROXIES`, and remove the `ports:` entry from the `solya` service so only Caddy can reach it.

   For any other proxy (a tunnel, another host), list the address it connects from. A proxy on another host cannot reach the loopback-only port, so change the `ports:` entry to an address that proxy can reach (and firewall it from everyone else). Never list an address that clients can reach directly: anyone connecting from it could set their own `X-Forwarded-For`.

   The web session cookie is `Secure` and bound to the host in `PUBLIC_BASE_URL`. Open the site only through that exact host: if the browser uses another host name (an alias, a bare IP, `www.`), the cookie is not sent and every journey page redirects back to `/`.
9. **Create an invite and send it to Oren:**

   ```bash
   docker compose exec solya solya invite create
   ```

   It prints two links for the same one-time token:

   ```
   telegram: https://t.me/<bot_username>?start=<token>
   web: https://<your-host>/join/<token>
   ```

   Send the one that suits the person. The Telegram link registers them in the bot directly. The web link opens the sign-up form with the code filled in. The token works once, whichever link is used.
10. **Oren connects his sites, one approval per site**, so he does this twice. Through Telegram he sends `/connect` to the bot once per site and approves each one at SolarEdge; after each approval the bot confirms in Telegram. An owner who signed up on the web connects through the web journey below instead, and the same approval applies.
11. **Optional: override labels, tariffs and the rest.** Owners set their own system name, tariff and income basis on `/sites` and `/settings` (see Owner settings below), so this step is an override only. Check the site ids first with `docker compose exec solya solya sites list`, then run the commands below (the ids are Oren's two sites):

    ```bash
    solya sites set --site 3033498 --label משק --tariff 0.45 --income-basis production --kwp 90.72 --commercial
    solya sites set --site 4552265 --label בית --card-name "IL 360" --tariff 0.48 --income-basis export
    ```

    Run them as `docker compose exec solya solya sites set ...`. Quote any value with spaces.

## RAY

RAY is the daily score (0 to 100) shown on the card as "מדד RAY". It combines how the system performed against its expected yield and its own history (Perf), how steady it has been over the last 30 days (Health) and the day's weather (Env: cloud cover, maximum temperature, PM10). Every component is stored per site and day in the `ray_daily` table so it can be compared with the existing Instinct score.

Settings (all in `.env`, see `.env.example`):

- `RAY_IMPLEMENTATION=solya.rules.ray_v1:RayV1` turns RAY v1 on. Unset, nothing is computed or fetched.
- `RAY_VISIBLE=true` shows RAY to owners (card gauge and per-site score, per-site RAY and the total line in the text). The default `false` computes and stores RAY but shows it only to admins (`/admin` and `ray show`).
- `RAY_NATIONAL_KWH_PER_KWP_DAY` is the 12-value monthly yield table (kWh per kWp per day, January first) used until a site has enough history. **The defaults are approximate PVGIS figures recalled from knowledge: PVGIS was unreachable when this was built. Oren should confirm them**, or supply his own table.

Weather comes from Open-Meteo, which costs no SolarEdge credits. Its free tier is for **non-commercial use only**: a paid plan or the commercial host is needed before charging customers. Backfill of RAY history (the previous month and the same month one and two years back) costs at most **6 credits per site per month**, including the 2 credits of the month-to-date backfill, all at the lowest ("optional") priority.

Rollout:

1. Set `RAY_IMPLEMENTATION` and keep `RAY_VISIBLE=false`. Restart. The next evening pulls store weather and RAY.
2. Compare for a few weeks with `solya ray show --site ID` and the `/admin` page (yesterday's RAY and its confidence per site). Use `ray recompute` after fixing a site's kWp or data.
3. Once Oren is happy with the numbers, set `RAY_VISIBLE=true` and restart.

## Owner settings

Owners manage their own systems; the admin does not have to run `sites set`.

- **`/sites`** lists the connected systems. Editing there applies only before the owner links Telegram; once the chat is linked the page is locked and the owner uses `/settings` instead. For each system the owner picks whether to monitor it, and sets the name, the tariff (0.05 to 3.00 ₪/kWh, up to 4 decimals; "0,48", "₪0.48" and "0.48 ₪" are accepted) and the income basis (production or export). Name is 1 to 30 characters.
- **`/settings`**: the owner sends `/settings` to the bot. The bot replies with a single-use link that is valid for 15 minutes. The link opens a confirm page (nothing happens on the GET); after the owner presses the button, a web session starts and lasts 24 hours. The link is stored hashed. A second use or an old link shows the "expired" page. Free text works too: "הגדרות" or "תעריף" gets the settings link, and a connect phrase such as "תחבר לי עוד מערכת" gets the SolarEdge connect link.
- When the owner changes the set of monitored systems on the settings page, the admin gets a notice that says, per system, whether monitoring started or stopped, with the new monthly price. Today's jobs are re-planned at once, so a newly ticked system gets tonight's report. A system connected after the page was opened (for example in another tab) is not on the submitted form and keeps its monitoring.
- **Commands menu.** At startup the bot registers its commands with Telegram (`set_my_commands`), so they show in the menu next to the message box. A command copied out of a Hebrew message carries invisible direction marks from that text (Telegram then does not see it as a command); the marks are stripped before the command is read, and the words after it are kept, so a copied `/start <code>` still works.

### Backfill

When an owner connects a system, and once at startup, Solya fills the month so far from SolarEdge, so the month figures are not empty. It uses at most 2 API credits per site per calendar month (energy and meter telemetry), at the lowest ("optional") priority. A failed call is not credited and is retried at the next start; for an export-basis system a failed meter call writes nothing (the retry reuses the stored energy answer), while a 404 means no meter. It writes only days before today (site-local) that have no row yet. Today's row belongs to the evening pull and existing rows are never changed.

## Web journey

A person with an invite can onboard on the web, in Hebrew, with no Telegram step until the end:

1. `/join/<token>` (or `/` with the code typed in): name, email, phone and the invite code. Sign-up uses the invite once and creates a web owner (status `lead`).
2. `/connect-site`: sends them to SolarEdge to approve one site (the same V2 OAuth as `/connect`). They can add more sites afterwards.
3. `/sites` then `/price`: they pick which connected sites to monitor and see the price. Status becomes `pending_payment` when they confirm.
4. `/pay/<id>` is a **stub**. It records the intended amount and shows that nothing was charged and no subscription started. No payment provider is chosen and VAT display is undecided; both are stated on the page.
5. `/ready` shows a button to `https://t.me/<bot_username>?start=<link_token>`. Sending that `/start` in Telegram links the chat to the owner, sends the welcome messages and plans their reports. Evening reports start the same day if the site is connected in time.

The link token is single use and cleared once the chat is linked. An owner with no linked chat at report time is skipped (nothing is sent, nothing is marked failed) and the others still get theirs. A chat that is already bound to another account is refused.

### Price

The price is 49.97 ₪ per site up to 50 kW plus 1.00 ₪ per kW above 50, rounded half-up to the agora. The power used is `--kwp` (the installed kW kept on the site, `billing_kwp_override`) when set. Otherwise the portal's reported peak power is used and the price is shown as an **estimate** ("הערכה לפי נתוני הפורטל, המחיר הסופי ייקבע לפי ההספק המותקן"), because the portal figure is not used for billing. With neither, the price is shown as unknown. Run `sites set --site ID --kwp N` for each site to make its price final (an admin override).

## Admin commands

Run inside the running container (`docker compose exec solya solya <command>`). The CLI uses the same database and `.env` as the service.

| Command | What it does |
| --- | --- |
| `serve [--host 0.0.0.0 --port 8000]` | Runs the service (the container's default command). |
| `genkey` | Prints a new Fernet key. |
| `invite create` | Prints a one-time invite as a Telegram link and a web `/join` link. |
| `owners list` | Lists owners. |
| `owners set-name --owner ID --name NAME` | Changes the name used in greetings. |
| `sites list` | Id, owner, portal name, label, card name, commercial, tariff, basis, status, comm state, last update. Never shows tokens. |
| `sites set --site ID [options]` | Options: `--label`, `--card-name`, `--tariff`, `--income-basis production\|export`, `--kwp`, `--commercial` / `--no-commercial`, `--check-times 12:30,15:00`, `--status active\|paused`, `--call-plan standard\|low`, `--monitored` / `--no-monitored`. |
| `credits` | Credits used this cycle against the cap, usage per site over the last 30 days, and an estimated cost at $0.005 per credit. |
| `report --owner ID [--date YYYY-MM-DD] --dry-run --out out/` | Builds the evening report from stored data only. Writes the PNG and prints the text. Sends nothing and makes no API calls. |
| `pull --site ID [--kind evening\|midday] [--notify]` | Pulls one site from the live API now and processes it exactly like the scheduled pull: stores the raw responses, saves the day's figures (evening kind) and updates fault records. Uses credits. Prints the parsed result. Sends no Telegram messages unless `--notify` is given; faults it records are alerted by the service's next pull. Without `--notify` it opens new fault records but closes none, so the service's next pull closes them and sends the follow-up. |
| `ray show --site ID [--date YYYY-MM-DD] [--days 7]` | Prints the stored RAY components per day, newest first, next to the day's actual production (actual_kwh, ray, perf, perf_ratio, hist_sub, health, env, expected_wh, expected_source, cv, cloud_pct, tmax_c, pm10, confidence). |
| `ray recompute --site ID --date YYYY-MM-DD` | Recomputes that day's RAY from stored production. Fetches the weather from Open-Meteo's historical endpoint when it is missing. Never calls SolarEdge. Also refetches weather when any of cloud, Tmax or PM10 is missing, and sets `daily_metrics.ray` to the new value. When RAY cannot be computed (no production, no weather, no kWp) it prints why, deletes any stale `ray_daily` row for the day and clears `daily_metrics.ray`. Works whether or not `RAY_IMPLEMENTATION` is set (it always uses RAY v1). |
| `replay --site ID --date YYYY-MM-DD [--dry-run]` | Re-parses the stored raw responses for that local day and recomputes the daily figures and prints the detected faults. Makes no API calls. See the note below. |

### Low call plan

`sites set --site ID --call-plan low` puts a site on the low-credit plan: the evening pull calls only `/sites/{id}`, `/overview` and `/alerts` (plus `/devices` when the stored list is more than 7 days old), and there is no mid-day check. It does not call inverter status or lifetime energy, so lifetime energy is not stored for those days. The low plan detects only open SolarEdge alerts and zero production, not inverter-mode or missing-inverter faults. Switching a site to `low` closes its open inverter-mode and missing-inverter faults without sending a message, and prints `closed N inverter faults (low plan)`. `--no-monitored` stops scheduled pulls, reports and alerts for a site; `solya pull` still works on it.

### When changes take effect

The service plans each day's jobs (evening pulls, evening reports, and mid-day checks if configured) at 00:05 Israel time, and again whenever it starts. By default there is one SolarEdge pull per site per day, about 25 minutes after that site's sunset (computed from its coordinates). Mid-day checks for commercial sites are off by default; turn them on for all commercial sites with `DEFAULT_CHECK_TIMES='["12:30"]'`, or for one site with `sites set --site ID --check-times 12:30`. SolarEdge has no webhook for alerts, so faults are found at the evening pull (or a mid-day check, if enabled).

- **A newly connected site** is planned straight away: a site connected in the afternoon gets that evening's pull and report. Connecting soon after sunset still runs that evening's pull, and the report follows 5 minutes later.
- **CLI changes** (`sites set` with `--commercial`, `--check-times` or `--status`, and the other options) run in a separate process, so the running service sees them at the next 00:05 planning or after a restart (`docker compose restart solya`). Labels, card names, owner names, tariffs and the income basis are read from the database on every pull and message, so they apply from the next one without a restart. A restart after sunset re-runs that evening's pulls and, 5 minutes later, the report; a report already sent is not sent again.

`replay` writes the recomputed daily row only. It prints the faults it detects but never creates, closes or alerts on fault records. Use `--dry-run` to save nothing at all.

## First-run verification

Do this after both sites are connected, before trusting the numbers.

1. Run a manual pull for each site:

   ```bash
   docker compose exec solya solya pull --site 3033498 --kind evening
   docker compose exec solya solya pull --site 4552265 --kind evening
   ```

   Run it after sunset, so the day is complete. A pull makes up to 6 API calls (site, overview, alerts, inverter status, lifetime energy, and devices when the device list is old), which count against the 2,000 monthly credits. The calls are spaced 7.5 seconds apart, so one pull takes under a minute.

   The pull is processed like the evening job: the day's figures are saved and fault records updated, so the report in step 3 shows them. It sends nothing to Oren (pass `--notify` to send the alerts it finds). It prints the parsed data, and the raw responses are saved.
2. Compare with the SolarEdge portal for the same site and day: production total, export, last update time, open alerts and the inverter list. Production and income must match the portal to rounding.
3. Build the report without sending it and look at the card:

   ```bash
   docker compose exec solya solya report --owner 1 --dry-run --out out/
   ```
4. If a number looks wrong, fix the parser or settings and re-run `solya replay` on the stored data instead of pulling again.
5. Check `solya credits`.

### Still UNVERIFIED: confirm on first run

These come from the API recon (section 8) and have not been checked against the live service. Each one is configurable or fails safe, so none blocks the build. Check them on the first real run and note the result.

1. Whether `access_duration`, `external_id`, `state` and `response_type` are accepted on authorize, and whether `state` and `external_id` are echoed back.
2. Whether PKCE is enforced or ignored, and the exact `redirect_uri` matching rules.
3. Authorization-code lifetime (reported as about 1 minute).
4. Refresh-token idle expiry: 14 days or 30 days. Whether the token response includes `scope`.
5. Whether token calls consume credits. The free-tier figures (2,000 credits, 10 calls per minute), burst behaviour, and whether the month and second rate-limit headers are actually sent.
6. Whether one consent can cover several sites (expected: no).
7. Whether overview accepts a full previous-day `from`/`to` window, and its cap.
8. How repeated `types=` parameters are encoded on `/devices`.
9. Which scope each endpoint needs (`SITE_DATA` or `DEVICE_DATA`).
10. The alert timestamp format in live responses, and whether `category` is present.
11. Which `inverters/status` fields are present on our sites' inverter models.
12. Which "migrating V1 to V2" documentation page is current (two candidates exist on api-docs.solaredge.com).
13. Open-Meteo terms, pricing, commercial host and `daily=sunset`. Telegram HTML tag list and webhook ports. Weather is only needed for RAY; the free tier is non-commercial.

### Other open items

- Tariffs 0.45 and 0.48 are inferred from the 24 Nov numbers. They match exactly but should be confirmed with Oren.
- The hosting target and domain for `PUBLIC_BASE_URL` are not decided.
- The approved welcome text: the current text is the 1 Nov draft.

## What is out of scope

- Payments and billing.
- A landing page, marketing and referral.
- Per-optimizer diagnosis. The API has no such data.
- Quote review, upgrade advice, technician booking and opening service calls.
- Other inverter brands.
- The paid deep bill check.
- WhatsApp. Only the adapter seam exists.
- Any editing from the web. The `/admin` page is read-only; changes go through the CLI.
- Real payments: the pay page is a stub and charges nothing.
- Free-form Q&A by default. Only the fixed questions are answered unless the optional AI answers below are switched on.
- Anything that lets a customer message change an account. Inbound messages can only request a connect link, register with an invite, link a web sign-up's Telegram chat (the single-use link token from `/ready`), and acknowledge a fault. With AI answers on, a free-form message that matches no fixed question is also sent to the external AI provider (see "Optional AI answers"); the reply cannot change anything.

## Optional AI answers

Off by default (`LLM_ENABLED=false`). When you enable it (`LLM_ENABLED=true`, plus `LLM_API_KEY`, `LLM_MODEL`, optionally `LLM_BASE_URL` and `LLM_MONTHLY_TOKENS_PER_OWNER`), a free-form owner message that matches none of the fixed questions is sent, together with up to 31 days of stored site aggregates (date, site label, kWh, income, communication state, open faults), to the configured provider. No email, phone or token is sent. The answer is dropped in favour of the help text if it contains a number that is not in that data. It cannot change anything, and a reply that claims an action (cancel, refund, charge, service call, technician) is also replaced by the help text. Usage is capped per owner by `LLM_MONTHLY_TOKENS_PER_OWNER` (default 50000 tokens a month); after that the owner is told the monthly budget for open answers is used, and the summary and alerts keep arriving. Example:

```
LLM_ENABLED=true
LLM_BASE_URL=https://openrouter.ai/api/v1
LLM_API_KEY=sk-...
LLM_MODEL=<provider model id>
LLM_MONTHLY_TOKENS_PER_OWNER=50000
```

Data note: with this on, owners' questions and their stored aggregates leave the system for that provider, so check the provider's terms and tell owners. The number guard does not catch numbers written as words, and small integers are always treated as grounded because dates appear in the data, so treat AI answers as best-effort.

## Development

```bash
uv venv && source .venv/bin/activate
uv pip install -e '.[dev]'
pytest
```

The report card needs Pillow with the Raqm layout engine. On Debian and Ubuntu that means `libfribidi0` (the Docker image installs it). The service checks this at start-up and refuses to run without it.

## CI/CD (GitHub Actions)

- **CI** (`.github/workflows/ci.yml`) runs on every pull request and can be started by hand. It does two things:
  - runs the full test suite on Python 3.12;
  - builds the Docker image and checks inside it that the Hebrew card renders (Raqm) and that the CLI starts.
- **CD** (`.github/workflows/cd.yml`) runs on pushes to `main`, on `v*` tags, and by hand. It runs CI first. If CI passes, it builds the image for `linux/amd64` and `linux/arm64` and pushes it to the GitHub Container Registry as `ghcr.io/ofiramir/solya`, with these tags:
  - `latest` on `main`
  - `sha-<commit>`
  - `X.Y.Z` and `X.Y` for a `vX.Y.Z` tag

The push uses the workflow's own `GITHUB_TOKEN`, so no secrets are needed. After the first push, the repository owner should check the package's visibility in GitHub, under Packages → `solya` → Package settings. The visibility decides who can pull the image:

| Visibility | Pulling the image |
| --- | --- |
| Public | Works without logging in |
| Private | Needs `docker login ghcr.io` with a personal access token that has `read:packages` |

To pull it on a server:

```bash
docker pull ghcr.io/ofiramir/solya:latest
```
