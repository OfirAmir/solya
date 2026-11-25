# Solya trial on AWS EC2

This kit brings Solya up on one EC2 instance for a trial. The instance runs two containers. The Solya container holds the web app, the Telegram bot and the scheduler. The Caddy container serves HTTPS with a free Let's Encrypt certificate. The address comes from the instance's public IPv4: for IP `1.2.3.4` it is `https://1-2-3-4.sslip.io`. sslip.io is a public DNS service that resolves that name back to `1.2.3.4`, so no domain needs to be bought.

What is in this folder:

| File | What it does |
| --- | --- |
| `setup.sh` | Installs Docker, fetches the code into `/opt/solya`, prepares `.env`, builds and checks the image, and starts the stack. Safe to run again. |
| `docker-compose.caddy.yml` | Adds Caddy on ports 80 and 443 and hides Solya's port 8000 from the outside. Uses its own compose project (`solya-ec2`), image tag and network, so it does not collide with anything else on the host. |
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

- **Instance size:** at least about 1 GB RAM (for example `t3.micro` or bigger) and 10 GB free disk.
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
  - the kit's network range (`172.30.0.0/24` by default) overlaps the VPC's ranges, a route on the host, or another Docker network;
  - Docker is not installed but another container runtime is (`containerd`, `runc`, `docker.io`, `podman-docker`, or the Docker snap).

  If it stops for ports 80/443, do not stop the other service. Talk to the host owner (Ofir) first. The alternative is to put Solya behind the existing web server instead of this kit's Caddy, as in the main README step 8 ("any other proxy"). This kit does not cover that setup.
- **Docker already installed:** the script leaves it as it is. It does not enable it at boot or add anyone to the `docker` group. If the host's compose plugin is missing or older than 2.24, or buildx is missing, it downloads them, checks their checksums and stores them only for this kit in `/opt/solya/.docker/cli-plugins`. Nothing is installed host-wide.

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
git clone --branch claude/dreamy-turing-jyrdw1 https://github.com/OfirAmir/solya.git /opt/solya
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
3. installs git, and Docker only if Docker is not there at all; adds a private compose or buildx plugin if needed;
4. clones or updates the repository in `/opt/solya` at the branch (`--branch NAME` picks another one);
5. reads the public IP from the EC2 metadata service (IMDSv2) and builds `<host>` from it;
6. creates `/opt/solya/.env` (mode 600, owned by you) from `.env.example` if it does not exist, and fills in the empty keys `PUBLIC_BASE_URL=https://<host>`, `COMPOSE_FILE`, `COMPOSE_PROJECT_NAME=solya-ec2`, `SOLYA_SUBNET`, `SOLYA_IP_RANGE` and `SOLYA_CADDY_IP`. Each change is written atomically, and the previous file is kept as `/opt/solya/.env.bak`;
7. builds the image (`solya-ec2`) and checks that the Hebrew text layout (Raqm) works inside it;
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

> [!WARNING]
> **Do not reuse the software house's (vendor's) SolarEdge app for this trial.**
>
> - A SolarEdge app has one registered redirect URI. Pointing the vendor's app at this server breaks the vendor's own connect flow for as long as it points here.
> - The vendor's app shares one credit quota (2,000 credits a month on the free tier) with everything the vendor runs. This trial would use up the vendor's credits, and the vendor would use up the trial's.
>
> **Recommended:** Oren creates a **new free developer app** at developer.solaredge.com:
>
> - type **"Site Access (OAuth)"**;
> - scopes **`SITE_DATA`** and **`DEVICE_DATA`**;
> - redirect URI **`https://<host>/oauth/callback`**.
>
> As the main README says, also do not reuse Oren's personal "Oren Home Monitor" app: its daily job rotates the same sites' refresh tokens, and the two would race.

Oren sends the client secret through a private channel of his choice, and you type it straight into `.env` on the server.

## 5. Admin and optional settings

These names come from the main README's `.env` table. Set them in `/opt/solya/.env`:

| Key | What to put |
| --- | --- |
| `ADMIN_PASSWORD` | A long passphrase for the read-only `https://<host>/admin` page. While unset, `/admin` returns 404. |
| `ADMIN_CHAT_IDS` | A JSON list of Telegram chat ids that get admin notices, for example `[123456789]`. To find your id, send a message to a bot such as @userinfobot. |
| `SESSION_SECRET` | Optional. Signs the web session cookies. When unset it is derived from `TOKEN_ENCRYPTION_KEY`. To set one, use the output of `openssl rand -hex 32`. |
| `LLM_ENABLED`, `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`, `LLM_MONTHLY_TOKENS_PER_OWNER` | Optional AI answers, off by default. Read "Optional AI answers" in the main README before you turn them on, because owners' questions then leave the server. |

Do **not** set `TRUSTED_PROXIES` in `.env`. `docker-compose.caddy.yml` sets it to Caddy's fixed address (`SOLYA_CADDY_IP`, `172.30.0.10` by default), so the real visitor IP reaches the rate limits and the admin login lockout. A value in `.env` is ignored.

Leave `PUBLIC_BASE_URL`, `TOKEN_ENCRYPTION_KEY`, `COMPOSE_FILE`, `COMPOSE_PROJECT_NAME`, `SOLYA_SUBNET`, `SOLYA_IP_RANGE` and `SOLYA_CADDY_IP` as `setup.sh` wrote them.

## 6. Start and check

Apply the new `.env`. Run `up -d`, not `restart`: `restart` keeps the old settings.

```bash
/opt/solya/deploy/ec2/compose up -d
```

Re-running `/opt/solya/deploy/ec2/setup.sh` does the same and also checks the health URL.

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

Fetch the new code:

```bash
git -C /opt/solya pull --ff-only
```

Then re-run the script. It rebuilds the image, keeps `.env` as it is and recreates the containers:

```bash
/opt/solya/deploy/ec2/setup.sh
```

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

The kit's Docker network (`172.30.0.0/24` by default) must not overlap the VPC's ranges, a route on the host or another Docker network. Pick a free private range, for example `192.168.250.0/24`. The default VPC uses `172.31.0.0/16`, so avoid that one.

On the first run, before `.env` exists, pass the range in front of the command:

```bash
SOLYA_SUBNET=192.168.250.0/24 /opt/solya/deploy/ec2/setup.sh
```

If `.env` already exists, edit `SOLYA_SUBNET` there and delete the `SOLYA_IP_RANGE` and `SOLYA_CADDY_IP` lines. The script derives them again: the upper half of the range for Docker's own addresses, and network + 10 for Caddy, which `TRUSTED_PROXIES` then follows. If the stack ran before, take it down first so the network is recreated:

```bash
/opt/solya/deploy/ec2/compose down
```

Then re-run `/opt/solya/deploy/ec2/setup.sh`.

### `docker compose` says it does not know `!reset`

The compose plugin is older than 2.24. Use `/opt/solya/deploy/ec2/compose`, which picks up the newer plugin that `setup.sh` stores in `/opt/solya/.docker/cli-plugins`. A plain `sudo docker compose` uses the host's older one.
