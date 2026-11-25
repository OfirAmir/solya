# Solya trial on AWS EC2

This kit brings Solya up on one EC2 instance for a trial. The instance runs two containers. The Solya container holds the web app, the Telegram bot and the scheduler. The Caddy container serves HTTPS with a free Let's Encrypt certificate. The address comes from the instance's public IPv4: for IP `1.2.3.4` it is `https://1-2-3-4.sslip.io`. sslip.io is a public DNS service that resolves that name back to `1.2.3.4`, so no domain needs to be bought.

What is in this folder:

| File | What it does |
| --- | --- |
| `setup.sh` | Installs Docker, fetches the code into `/opt/solya`, prepares `.env`, pulls and checks the image, and starts the stack. Safe to run again; it is also how you update. `--build` builds the image locally instead of pulling it. |
| `docker-compose.caddy.yml` | Adds Caddy on ports 80 and 443 and hides Solya's port 8000 from the outside. Uses its own compose project (`solya-ec2`) and network, runs the image named by `SOLYA_IMAGE`, so it does not collide with anything else on the host. |
| `compose` | `sudo docker compose` for this kit: both compose files, the `solya-ec2` project and the kit's own plugin folder. Use it for every compose command in this guide. |
| `Caddyfile` | Caddy's config. The host name comes from `PUBLIC_BASE_URL` in `.env`. |

The main [README](../../README.md) is the reference for every setting and command. This guide only says how to apply them on EC2.

Throughout this guide:

- `<host>` means your sslip.io name, for example `1-2-3-4.sslip.io`. `setup.sh` prints it.
- Run `setup.sh` and `git` as **your normal user**, not as root and not through `sudo`. The script calls `sudo` itself for the steps that need it. `/opt/solya` and `/opt/solya/.env` belong to your user.
- Run every compose command through `/opt/solya/deploy/ec2/compose`. It calls `sudo docker compose` with the right files, so your user does not need to be in the `docker` group.
- Secrets go only into `/opt/solya/.env` on the server. Never commit them, and never paste them into chat, email or a ticket.

## 1. EC2 prerequisites

Check these in the AWS console before you start.

- **Instance size:** at least about 512 MB RAM (a `t3.micro` with 1 GB is comfortable) and 10 GB free disk. A local build with `--build` wants about 1 GB RAM.
- **Operating system:** Amazon Linux 2023, Ubuntu 22.04 or Ubuntu 24.04. The script detects which one from `/etc/os-release` and refuses any other.
- **Security group, inbound rules:**

  | Port | Source | Why |
  | --- | --- | --- |
  | 22 (SSH) | your own IP only (recommended), or 0.0.0.0/0 | to log in |
  | 80 (HTTP) | 0.0.0.0/0 | Let's Encrypt checks this port before it issues the certificate. Caddy also redirects HTTP to HTTPS here. |
  | 443 (HTTPS) | 0.0.0.0/0 | the web pages, the OAuth callback, `/admin` |

  Do **not** open port 8000. Solya listens only inside Docker's network, and Caddy is its only way in.
- **Elastic IP (recommended):** attach an Elastic IP to the instance. Without one, the public IP changes when the instance is stopped and started. A new IP means a new `<host>`, and so a new OAuth redirect URI that must be updated in the SolarEdge app, plus new invite links.
- **The security group is the real firewall.** A host firewall such as `ufw` does **not** filter ports that Docker publishes, because Docker writes its own firewall rules ahead of ufw's. Whatever the security group allows on 80 and 443 reaches Caddy.
- **Other services on the instance:** this is a shared host, so the kit is careful not to disturb what already runs there. `setup.sh` stops before installing anything if:
  - something other than this kit's Caddy already uses port 80 or 443 (for example an existing nginx, Apache or another container);
  - another copy of this kit, started from a different folder, already uses the `solya-ec2` compose project;
  - the kit's network range (`192.168.250.0/24` by default) overlaps the VPC's ranges, a route on the host, or another Docker network;
  - the VPC's ranges cannot be read from the EC2 metadata service and you did not choose the range yourself with `SOLYA_SUBNET`;
  - Docker is not installed but another container runtime is (`containerd`, `runc`, `docker.io`, `podman-docker`, or the Docker snap).

  The script can only see this instance's own VPC ranges, routes and Docker networks. It cannot see ranges that reach the VPC from elsewhere: peered VPCs, a VPN, or a Transit Gateway. **Ask the host owner (Ofir) whether `192.168.250.0/24` is used anywhere in those.** If it is, or if you are not sure, pick a range together and pass it on the first run (see "`setup.sh` says the subnet overlaps a network" under Troubleshooting).

  If it stops for ports 80/443, do not stop the other service. Talk to the host owner (Ofir) first. The alternative is to put Solya behind the existing web server instead of this kit's Caddy, as in the main README step 8 ("any other proxy"). This kit does not cover that setup.
- **Image:** the server runs the published image `ghcr.io/ofiramir/solya:latest` from GitHub Container Registry (built by CI for amd64 and arm64: `latest` follows `main`, `sha-<commit>` pins a commit, `v*` tags give versions). Nothing is built on the instance. The `.env` key `SOLYA_IMAGE` names the image and `setup.sh` fills it in. The instance needs outbound access to `ghcr.io`. If the package is private, log in once with a GitHub token that has `read:packages`, using the kit's own Docker config (see Troubleshooting, "`setup.sh` cannot pull the image"). If GHCR is unreachable, or to try local changes, `setup.sh --build` builds from the checkout instead.
- **Docker already installed:** the script leaves it as it is. It does not enable it at boot or add anyone to the `docker` group. If the host's compose plugin is missing or older than 2.24, it downloads it (and buildx, only with `--build`), checks their checksums and stores them only for this kit in `/opt/solya/.docker/cli-plugins`. Nothing is installed host-wide.

## 2. SSH in and run `setup.sh`

Log in to the instance. `<user>` is `ec2-user` on Amazon Linux and `ubuntu` on Ubuntu.

```bash
ssh -i /path/to/key.pem <user>@<instance-public-ip>
```

Install git if it is missing. On Amazon Linux 2023:

```bash
sudo dnf install -y git
```

On Ubuntu:

```bash
sudo apt-get update && sudo apt-get install -y git
```

Fetch the code into `/opt/solya`:

```bash
sudo install -d -m 0755 -o "$USER" -g "$(id -gn)" /opt/solya
```

```bash
git clone --branch main https://github.com/OfirAmir/solya.git /opt/solya
```

The repository is public, so no token or password is needed.

Preview what the script will do. This changes nothing. Run it as your normal user, without `sudo`:

```bash
/opt/solya/deploy/ec2/setup.sh --dry-run
```

Run it:

```bash
/opt/solya/deploy/ec2/setup.sh
```

The script:

1. refuses to run as root (pass `--allow-root` only if you really mean it);
2. runs the safety checks from step 1 (ports 80/443, the compose project, the network range) before it installs anything;
3. installs git, and Docker only if Docker is not there at all; adds a private compose plugin if needed (buildx too, only with `--build`);
4. clones or updates the repository in `/opt/solya` at the branch (`--branch NAME` picks another one);
5. reads the public IP from the EC2 metadata service (IMDSv2) and builds `<host>` from it;
6. creates `/opt/solya/.env` (mode 600, owned by you) from `.env.example` if it does not exist, and fills in the empty keys `PUBLIC_BASE_URL=https://<host>`, `COMPOSE_FILE`, `COMPOSE_PROJECT_NAME=solya-ec2`, `SOLYA_SUBNET`, `SOLYA_IP_RANGE` and `SOLYA_CADDY_IP` and `SOLYA_IMAGE=ghcr.io/ofiramir/solya:latest`. Each change is written atomically, and the previous file is kept as `/opt/solya/.env.bak`;
7. pulls the image named by `SOLYA_IMAGE` from GHCR (with `--build` it builds it from the checkout instead, and tags it with that name) and checks that the Hebrew text layout (Raqm) works inside it. If the pull fails it stops and says why; it never falls back to building on its own;
8. generates `TOKEN_ENCRYPTION_KEY` with `solya genkey` if it is empty;
9. starts Caddy, and Solya too once the secrets are filled in;
10. prints the host, the redirect URI and exactly which keys in `.env` are still empty.

It never overwrites a value that is already set in `.env`. If it stops on a check, it says why; see step 1 and Troubleshooting. If the IP lookup fails (for example IMDS is turned off on the instance), pass the IP yourself:

```bash
/opt/solya/deploy/ec2/setup.sh --ip <public-ipv4>
```

**Keep a copy of `TOKEN_ENCRYPTION_KEY`** somewhere safe, such as a password manager. If it is lost, every stored SolarEdge token becomes unreadable and each site must be connected again.

The first run ends with "Fill these keys". That is expected: steps 3 to 5 fill them.

To edit `.env` in the steps below, open it with an editor such as `nano`:

```bash
nano /opt/solya/.env
```

Write values without spaces around `=`. If a value contains `$`, `#` or spaces, wrap it in single quotes, for example `ADMIN_PASSWORD='my $ecret phrase'`.

## 3. Create the Telegram bot

1. In Telegram, open a chat with **@BotFather** and send `/newbot`.
2. Give it a display name, then a user name that ends in `bot`.
3. BotFather replies with a token like `123456789:AA...`. Treat it like a password.

Put both in `/opt/solya/.env`:

```
TELEGRAM_BOT_TOKEN=<token from BotFather>
TELEGRAM_BOT_USERNAME=<bot user name without the @>
```

Use a **new bot for this trial**. Only one running process may use a bot token at a time (see Troubleshooting).

## 4. SolarEdge app credentials from Oren

Solya connects to SolarEdge through a developer app of type "Site Access (OAuth)". Oren, the site owner, provides the app's client ID and client secret. Put them in `/opt/solya/.env`:

```
SOLAREDGE_CLIENT_ID=<client id>
SOLAREDGE_CLIENT_SECRET=<client secret>
```

The app's redirect URI must be exactly this (it is the path in `src/solya/web/oauth.py`, and `setup.sh` prints it):

```
https://<host>/oauth/callback
```

Which app to use is Oren's decision. There are two routes.

> [!WARNING]
> Do not reuse Oren's personal "Oren Home Monitor" app, whichever route you take. Its daily job rotates the same sites' refresh tokens, and the two systems would race.

### Route A: a new developer app

This is the isolated option. Nothing else is affected.

Oren signs up for a **new free developer account** at developer.solaredge.com, using an email not already registered there. A free account holds exactly one app, so his existing accounts cannot take a second one. He then creates an app with these settings:

- type **"Site Access (OAuth)"**
- scopes **`SITE_DATA`** and **`DEVICE_DATA`**
- redirect URI **`https://<host>/oauth/callback`**

The account comes with its own 2,000 credits a month. Oren's two sites use about 520 of them.

### Route B: the Solya app (the software house's)

This route reuses the app in the Solya developer account, which the software house connected. It is on the Business Starter plan until 17 Dec 2026. Oren chose this route because he is leaving the vendor. Before switching, know what it does:

1. **The vendor's new connections stop working.** The app has one registered redirect URI, and pointing it at this server breaks the vendor's connect flow for anyone new. Connections that already exist should keep running, because grants already given do not depend on the redirect URI. If other customers are still onboarding through the vendor, tell Oren first.
2. **Copy the client secret. Do not regenerate it.** The vendor's running system uses the same secret to refresh its tokens. If the portal only lets Oren *regenerate* the secret, the vendor's service stops that same day. Do that only if Oren means to cut the vendor off.
3. **Credits and the rate limit are shared with the vendor.** The plan's 20,000 credits a month and 25 calls a minute cover everything the vendor still runs, plus this server. Solya's budget guard counts only its own calls. If the vendor uses up the month, SolarEdge refuses this server's pulls as well. The evening report then says the data has not arrived; it never shows zero. Ask the vendor to stop polling, or switch the app off on their side, as soon as Oren is ready.
4. **The plan reverts on 17 Dec.** After that the account is on the free tier: 2,000 credits a month and one app. That is still enough for Oren's two sites, as long as the vendor's system has stopped by then.
5. **Oren approves both sites again** through this server (step 8). His sites already have a grant on this same app from the vendor's side. Whether a second grant for the same app and site cancels the first is unconfirmed. If it does, the vendor's monitoring of Oren's own sites stops.

Also tell Solya the plan's real budget. Add these lines to `/opt/solya/.env`:

```
BUDGET_MONTHLY_CREDITS=20000
BUDGET_BILLING_DAY=17
```

The billing day is the 17th because the Starter plan was bought on 17 Nov. After 17 Dec, change the first line to `BUDGET_MONTHLY_CREDITS=2000` and keep the billing day. Then apply it:

```bash
/opt/solya/deploy/ec2/compose up -d
```

### Either route

Oren sends the client secret through a private channel of his choice. You type it straight into `.env` on the server. Never paste it into a chat.

## 5. Admin and optional settings

These names come from the main README's `.env` table. Set them in `/opt/solya/.env`:

| Key | What to put |
| --- | --- |
| `ADMIN_PASSWORD` | A long passphrase for the read-only `https://<host>/admin` page. While unset, `/admin` returns 404. |
| `ADMIN_CHAT_IDS` | A JSON list of Telegram chat ids that get admin notices, for example `[123456789]`. To find your id, send a message to a bot such as @userinfobot. |
| `SESSION_SECRET` | Optional. Signs the web session cookies. When unset it is derived from `TOKEN_ENCRYPTION_KEY`. To set one, use the output of `openssl rand -hex 32`. |
| `LLM_ENABLED`, `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`, `LLM_MONTHLY_TOKENS_PER_OWNER` | Optional AI answers, off by default. Read "Optional AI answers" in the main README before you turn them on, because owners' questions then leave the server. |

Do **not** set `TRUSTED_PROXIES` in `.env`. `docker-compose.caddy.yml` sets it to Caddy's fixed address (`SOLYA_CADDY_IP`, `192.168.250.10` by default), so the real visitor IP reaches the rate limits and the admin login lockout. A value in `.env` is ignored.

Leave `PUBLIC_BASE_URL`, `TOKEN_ENCRYPTION_KEY`, `COMPOSE_FILE`, `COMPOSE_PROJECT_NAME`, `SOLYA_SUBNET`, `SOLYA_IP_RANGE` and `SOLYA_CADDY_IP` as `setup.sh` wrote them.

## 6. Start and check

Apply the new `.env`. Run `up -d`, not `restart`: `restart` keeps the old settings.

```bash
/opt/solya/deploy/ec2/compose up -d
```

Re-running `/opt/solya/deploy/ec2/setup.sh` does the same, pulls the newest image first and also checks the health URL.

Check that both containers are running:

```bash
/opt/solya/deploy/ec2/compose ps
```

Check the health URL from the server:

```bash
curl https://<host>/healthz
```

Open `https://<host>/healthz` in a browser too. It must load with a valid padlock (no certificate warning). If it does not, see Troubleshooting.

## 7. Create an invite

```bash
/opt/solya/deploy/ec2/compose exec solya solya invite create
```

It prints two links for the same one-time token:

```
telegram: https://t.me/<bot_username>?start=<token>
web: https://<host>/join/<token>
```

Send Oren one of them. The token works once, whichever link is used.

## 8. Oren's trial

**Web path** (the `web:` link):

1. Oren opens the web invite link. The sign-up form opens with the code filled in. He enters his name, email and phone.
2. On the connect page he goes to SolarEdge and approves **one site**. He comes back and connects the next site the same way, once per site.
3. He picks which connected sites to monitor and sees the price.
4. The payment page is a **stub**: it records the amount, charges nothing and starts no subscription.
5. The last page has a button that opens Sol in Telegram. Pressing **Start** there links his chat. The welcome messages arrive and the evening reports are planned.

Use only `https://<host>` in the browser. The session cookie is tied to that exact name, so a bare IP or another name sends him back to the start page.

**Bot path** (the `telegram:` link):

1. Oren opens the Telegram invite link and presses **Start**. This registers him in the bot.
2. He sends `/connect` to the bot once per site and approves each one at SolarEdge. The bot confirms each site in Telegram.

## 9. First-run verification

Do this after both sites are connected, before trusting the numbers. Run it after sunset, so the day is complete.

List the sites and note their ids:

```bash
/opt/solya/deploy/ec2/compose exec solya solya sites list
```

Pull each site from the live API. This uses up to 6 API credits per site and sends nothing to Oren:

```bash
/opt/solya/deploy/ec2/compose exec solya solya pull --site <site-id> --kind evening
```

Compare what it prints with the SolarEdge portal for the same site and day: production total, export, last update time, open alerts and the inverter list. Production must match the portal to rounding.

Build the evening report without sending it. Writing it under `/app/data` puts it in `/opt/solya/data` on the server:

```bash
/opt/solya/deploy/ec2/compose exec solya solya report --owner <owner-id> --dry-run --out /app/data/report-check
```

`solya owners list` shows the owner id (Oren is `1` if he is the first owner). The text is printed. To look at the card image, copy it to your own computer:

```bash
scp -i /path/to/key.pem '<user>@<instance-public-ip>:/opt/solya/data/report-check/*.png' .
```

Check the credits used:

```bash
/opt/solya/deploy/ec2/compose exec solya solya credits
```

The main README's "First-run verification" section lists what else to confirm on the first run.

## 10. Labels, tariffs and kW

These are Oren's two sites, with the values from the main README. Check the ids with `sites list` first. The tariffs 0.45 and 0.48 were inferred from the 24 Nov numbers, so confirm them with Oren.

```bash
/opt/solya/deploy/ec2/compose exec solya solya sites set --site 3033498 --label משק --tariff 0.45 --income-basis production --kwp 90.72 --commercial
```

```bash
/opt/solya/deploy/ec2/compose exec solya solya sites set --site 4552265 --label בית --card-name "IL 360" --tariff 0.48 --income-basis export
```

Labels, tariffs and the income basis apply from the next pull. Other options (`--commercial`, `--check-times`, `--status`) apply at the next 00:05 Israel-time planning or after a restart:

```bash
/opt/solya/deploy/ec2/compose restart solya
```

## 11. Updating

**Moving from the first version of this kit (commit `ee9d5d3`).** That version used the compose project name `solya` and the subnet `172.30.0.0/24`. If it was ever started on this host, take that stack down first, so its containers stop holding ports 80/443 and its network is removed. The command names only the base compose file on purpose: the new Caddy file needs settings an old `.env` does not have, and `--remove-orphans` still removes the old Caddy container:

```bash
cd /opt/solya && sudo docker compose -p solya -f docker-compose.yml down --remove-orphans
```

The database in `/opt/solya/data` is kept. The old Caddy certificate volume is left in place; the new stack fetches a new certificate. The old `.env` keeps its values, but add nothing by hand: `setup.sh` fills the new keys. An old `.env` has no `SOLYA_SUBNET`, so the new default `192.168.250.0/24` is used unless you set one.

Fetch the new code:

```bash
git -C /opt/solya pull
```

Then re-run the script. It pulls the newest `latest` image from GHCR, keeps `.env` as it is and recreates the Solya container when the image changed:

```bash
/opt/solya/deploy/ec2/setup.sh
```

To stay on a fixed version, set `SOLYA_IMAGE` in `/opt/solya/.env` to a pinned tag, for example `SOLYA_IMAGE=ghcr.io/ofiramir/solya:sha-<commit>` (the short commit from the CI run), and re-run the script. The script never overwrites a value you set. Change it back to `ghcr.io/ofiramir/solya:latest` to follow `main` again.

To build from the checked-out source instead (GHCR unreachable, or testing local changes), run `/opt/solya/deploy/ec2/setup.sh --build`. The result is tagged with the `SOLYA_IMAGE` name, so a later run without `--build` replaces it with the pulled image.

## 12. Backups

Everything Solya stores is in the SQLite file `/opt/solya/data/solya.db`. Also keep `/opt/solya/.env`, or at least `TOKEN_ENCRYPTION_KEY`: without that key the stored tokens in a backup cannot be read.

**Option A, stop and copy.** It is simple and briefly takes the service down.

```bash
/opt/solya/deploy/ec2/compose stop solya
```

```bash
sudo cp /opt/solya/data/solya.db /opt/solya/data/solya-$(date +%F).db.bak
```

```bash
/opt/solya/deploy/ec2/compose start solya
```

**Option B, online copy with `sqlite3 .backup`.** The service keeps running. Install the `sqlite3` tool once. On Amazon Linux 2023:

```bash
sudo dnf install -y sqlite
```

On Ubuntu:

```bash
sudo apt-get install -y sqlite3
```

Then take the backup:

```bash
sudo sqlite3 /opt/solya/data/solya.db ".backup '/opt/solya/data/solya-$(date +%F).db.bak'"
```

Copy the backup off the server, for example with `scp` to your computer or to S3.

## 13. Logs

Follow Solya's log:

```bash
/opt/solya/deploy/ec2/compose logs -f solya
```

Follow Caddy's log (certificates, proxy errors):

```bash
/opt/solya/deploy/ec2/compose logs -f caddy
```

Press Ctrl+C to stop following.

## 14. Troubleshooting

### The certificate is not issued (browser warning, or `curl` fails on TLS)

Look at Caddy's log:

```bash
/opt/solya/deploy/ec2/compose logs caddy
```

- **Port 80 or 443 is closed.** Let's Encrypt must reach the instance on port 80 (and 443). Open both to `0.0.0.0/0` in the security group (step 1), and check any network ACL on the subnet. The security group is what counts: `ufw` does not filter Docker's published ports, so changing ufw neither opens nor closes them.
- **Wrong IP in the name.** `<host>` must contain the instance's current public IP. If the IP changed, see "The IP changed" below.
- **Something else holds port 80 or 443.** `setup.sh` stops before it starts anything. Do not stop the other service: it belongs to someone else on this host. Talk to the host owner, or put Solya behind the existing web server instead (main README step 8, "any other proxy").
- Caddy retries on its own. After you fix the cause, restart it:

```bash
/opt/solya/deploy/ec2/compose restart caddy
```

### "redirect_uri mismatch", or SolarEdge shows an error after approval

The redirect URI registered in the SolarEdge app must be exactly `https://<host>/oauth/callback`. That means `https`, the same `<host>` as `PUBLIC_BASE_URL`, the path `/oauth/callback`, and no trailing slash. Compare it with what `setup.sh` printed, or with:

```bash
grep '^PUBLIC_BASE_URL=' /opt/solya/.env
```

The authorization code lives about a minute, so Oren should finish the approval without a long pause. Solya logs why it refused a callback (never the code itself):

```bash
/opt/solya/deploy/ec2/compose logs solya | grep -i 'oauth callback'
```

### The bot does not answer

Only **one process may poll a bot token** at a time. If the same token runs anywhere else (a laptop, another server, an older container), the two fight and messages go missing. Stop every other instance that uses this token, or create a new bot for the trial (step 3). Then check that Solya is running and look at its log:

```bash
/opt/solya/deploy/ec2/compose ps
```

```bash
/opt/solya/deploy/ec2/compose logs --tail 100 solya
```

Also check that `TELEGRAM_BOT_TOKEN` and `TELEGRAM_BOT_USERNAME` are filled, and that you ran `/opt/solya/deploy/ec2/compose up -d` after editing `.env`.

### Reports arrive at the "wrong" time (time zone)

The server's clock may be in UTC. That is fine, and you do not need to change the instance's time zone. Solya plans in Israel time (`Asia/Jerusalem`, the `default_timezone` in `src/solya/config.py`) and works in UTC internally. Each day's jobs are planned at 00:05 Israel time and at every start. The evening report goes out about 30 minutes after the site's local sunset. Log timestamps are in UTC, so add 2 hours (3 in summer) to read them in Israel time. Only the clock must be right: EC2 keeps it synced by default.

### The IP changed (no Elastic IP)

`setup.sh` never overwrites `PUBLIC_BASE_URL`. It warns when the value no longer matches the IP. To move to the new address:

1. Attach an Elastic IP so it does not happen again.
2. Edit `PUBLIC_BASE_URL` in `/opt/solya/.env` to `https://<new-host>`.
3. Change the SolarEdge app's redirect URI to `https://<new-host>/oauth/callback`.
4. Re-run `/opt/solya/deploy/ec2/setup.sh`.
5. Create new invites. Old links point at the old name.

### `setup.sh` says the subnet overlaps a network

The kit's Docker network (`192.168.250.0/24` by default) must not overlap the VPC's ranges, a route on the host, another Docker network, or anything reached through a peered VPC, a VPN or a Transit Gateway. Ask the host owner for a free private range, for example `192.168.251.0/24`. Use a prefix between `/16` and `/27`. The default VPC uses `172.31.0.0/16`, so avoid that one.

The same applies when `setup.sh` stops because it cannot read the VPC ranges from the EC2 metadata service: once you have chosen a range with the host owner, passing it explicitly lets the run continue.

On the first run, before `.env` exists, pass the range in front of the command:

```bash
SOLYA_SUBNET=192.168.251.0/24 /opt/solya/deploy/ec2/setup.sh
```

If `.env` already exists, edit `SOLYA_SUBNET` there and delete the `SOLYA_IP_RANGE` and `SOLYA_CADDY_IP` lines. The script derives them again: the upper half of the range for Docker's own addresses, and network + 10 for Caddy, which `TRUSTED_PROXIES` then follows. If the stack ran before, take it down first so the network is recreated:

```bash
/opt/solya/deploy/ec2/compose down
```

Then re-run `/opt/solya/deploy/ec2/setup.sh`.

### `setup.sh` cannot pull the image

The script stops with "could not pull" and does not build on its own. Two usual causes:

- **The package is private** (the error says `denied` or `unauthorized`). Create a GitHub token with the `read:packages` scope and log in once. The kit reads credentials from its own Docker config, so a plain `sudo docker login` is not seen. Use:

```bash
sudo env DOCKER_CONFIG=/opt/solya/.docker docker login ghcr.io
```

  Enter your GitHub user name and the token as the password, then re-run `setup.sh`. Alternatively, the repository owner can make the package public in the GitHub package settings.
- **`ghcr.io` is unreachable** from the instance (egress firewall, no internet route). Fix the network, or run `/opt/solya/deploy/ec2/setup.sh --build` to build from the checkout.

A `not found` error means the tag in `SOLYA_IMAGE` does not exist (for example a mistyped `sha-<commit>`, or CI has not published it yet).

### `docker compose` says it does not know `!reset`

The compose plugin is older than 2.24. Use `/opt/solya/deploy/ec2/compose`, which picks up the newer plugin that `setup.sh` stores in `/opt/solya/.docker/cli-plugins`. A plain `sudo docker compose` uses the host's older one.
