# Solya

Solya watches SolarEdge solar sites through the SolarEdge Monitoring **V2** API and talks to the site owner in Hebrew on Telegram.

- Each owner connects a site from a link the bot sends them (V2 OAuth, one approval per site). Tokens are stored encrypted.
- A credit-budgeted schedule pulls data and stores the raw responses. Everything downstream reads the stored data.
- A rules engine with no LLM computes daily production, income in ₪, communication loss, zero production, open alerts and inverter faults.
- The evening report is a card image plus a short text, sent about 30 minutes after the owner's latest local sunset. Faults, communication loss and reminders arrive as separate messages.
- Owners can ask a few fixed questions (today, yesterday, this month, open alerts).
- Admin work is done with the `solya` command line tool. There is no admin UI.

RAY is a pluggable slot and is **disabled** until the real formula is supplied. While `RAY_IMPLEMENTATION` is unset, no RAY circle and no "RAY" word appear anywhere.

## Setup for the real run

Do these steps in order.

1. **Create a new developer account** at developer.solaredge.com. Do not reuse Oren's personal "Oren Home Monitor" app: its daily job rotates the same sites' refresh tokens, and the two would race. Do not use the software house's Solya account either.
2. **Register an app of type "Site Access (OAuth)"** with the scopes `SITE_DATA` and `DEVICE_DATA`.
3. **Set the redirect URI** to `https://<your-host>/oauth/callback`. `<your-host>` is the public HTTPS host from step 8. Note the client id and client secret.
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

   The database lives in `./data` (a SQLite file, mounted at `/app/data`). Back that folder up. Logs: `docker compose logs -f solya`. Health check: `GET /healthz`.
8. **Put HTTPS in front of port 8000.** The OAuth callback must be reachable on a public HTTPS host. Any of these works: Caddy, Cloudflare Tunnel, or a host's managed TLS. A minimal Caddy setup:

   ```
   solya.example.com {
       reverse_proxy localhost:8000
   }
   ```
9. **Create an invite and send it to Oren:**

   ```bash
   docker compose exec solya solya invite create
   ```

   It prints `https://t.me/<bot_username>?start=<token>`. The token works once.
10. **Oren sends `/connect` to the bot once per site** and approves each one at SolarEdge. One approval covers one site, so he does this twice. After each approval the bot confirms in Telegram.
11. **Set labels, tariffs and the rest.** Check the site ids first with `docker compose exec solya solya sites list`, then run the commands below (the ids are Oren's two sites):

    ```bash
    solya sites set --site 3033498 --label משק --tariff 0.45 --income-basis production --kwp 90.72 --commercial
    solya sites set --site 4552265 --label בית --card-name "IL 360" --tariff 0.48 --income-basis export
    ```

    Run them as `docker compose exec solya solya sites set ...`. Quote any value with spaces.

## Admin commands

Run inside the running container (`docker compose exec solya solya <command>`). The CLI uses the same database and `.env` as the service.

| Command | What it does |
| --- | --- |
| `serve [--host 0.0.0.0 --port 8000]` | Runs the service (the container's default command). |
| `genkey` | Prints a new Fernet key. |
| `invite create` | Prints a one-time Telegram invite link. |
| `owners list` | Lists owners. |
| `owners set-name --owner ID --name NAME` | Changes the name used in greetings. |
| `sites list` | Id, owner, portal name, label, card name, commercial, tariff, basis, status, comm state, last update. Never shows tokens. |
| `sites set --site ID [options]` | Options: `--label`, `--card-name`, `--tariff`, `--income-basis production\|export`, `--kwp`, `--commercial` / `--no-commercial`, `--check-times 12:30,15:00`, `--status active\|paused`. |
| `credits` | Credits used this cycle against the cap, usage per site over the last 30 days, and an estimated cost at $0.005 per credit. |
| `report --owner ID [--date YYYY-MM-DD] --dry-run --out out/` | Builds the evening report from stored data only. Writes the PNG and prints the text. Sends nothing and makes no API calls. |
| `pull --site ID [--kind evening\|midday] [--notify]` | Pulls one site from the live API now and processes it exactly like the scheduled pull: stores the raw responses, saves the day's figures (evening kind) and updates fault records. Uses credits. Prints the parsed result. Sends no Telegram messages unless `--notify` is given; faults it records are alerted by the service's next pull. |
| `replay --site ID --date YYYY-MM-DD [--dry-run]` | Re-parses the stored raw responses for that local day and recomputes the daily figures and prints the detected faults. Makes no API calls. See the note below. |

### When changes take effect

The service plans each day's jobs (evening pulls, mid-day checks, evening reports) at 00:05 Israel time, and again whenever it starts.

- **A newly connected site** is planned straight away: a site connected in the afternoon gets that evening's pull and report. Connecting soon after sunset still runs that evening's pull, and the report follows 5 minutes later.
- **CLI changes** (`sites set` with `--commercial`, `--check-times` or `--status`, and the other options) run in a separate process, so the running service sees them at the next 00:05 planning or after a restart (`docker compose restart solya`). Labels, tariffs and names are read when each message is built, so those apply to the next message. A restart after sunset re-runs that evening's pulls and, 5 minutes later, the report; a report already sent is not sent again.

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
13. Open-Meteo terms, pricing, commercial host and `daily=sunset`. Telegram HTML tag list and webhook ports. Weather is only needed for RAY, which is disabled.

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
- A dashboard or any admin UI. Admin is the CLI only.
- LLM or free-form Q&A. Only the fixed questions are answered.
- Weather inputs. They are only needed for RAY, which is disabled until its formula arrives.
- Anything that lets a customer message change an account. Inbound messages can only request a connect link, register with an invite, and acknowledge a fault.

## Development

```bash
uv venv && source .venv/bin/activate
uv pip install -e '.[dev]'
pytest
```

The report card needs Pillow with the Raqm layout engine. On Debian and Ubuntu that means `libfribidi0` (the Docker image installs it). The service checks this at start-up and refuses to run without it.
