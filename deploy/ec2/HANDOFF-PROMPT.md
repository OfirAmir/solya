# Handoff: deploy Solya on the EC2 instance

Paste everything below the line into a coding agent that has AWS access and SSH access to the instance (for example Claude Code on your laptop, with the AWS CLI configured). You can also follow it by hand.

---

You are helping Ofir deploy **Solya** on his existing AWS EC2 instance so Oren can try it. Solya is a SolarEdge monitoring service with a Telegram bot and a Hebrew web sign-up.

- Repo: https://github.com/OfirAmir/solya (public, branch `main`).
- It runs as a published Docker image, `ghcr.io/ofiramir/solya:latest`, behind Caddy. Caddy gets a free HTTPS certificate for an `sslip.io` name built from the instance's public IP.
- The full guide is `deploy/ec2/README.md` in the repo. Read it first. Where this prompt and the guide differ, the guide wins.

## Ground rules

- **This is a shared host that already runs other things.** Do not stop, remove or reconfigure any existing service, container, firewall rule or package. If `setup.sh` stops on a safety check, stop too and report what it said. Never work around a check.
- **AWS changes:** make only the ones listed in Part 1. Show each change and get Ofir's OK before applying it.
- **Secrets:**
  - Never print, log or echo a secret. That covers the SolarEdge client secret, the Telegram bot token, `TOKEN_ENCRYPTION_KEY` and `ADMIN_PASSWORD`.
  - Never put a secret in a chat, a commit or a command line.
  - Ofir types secrets straight into `/opt/solya/.env` on the server.
- **Running the script:** run `setup.sh` as the normal login user (`ec2-user` on Amazon Linux, `ubuntu` on Ubuntu), not as root and not with `sudo`. The script calls `sudo` itself for the steps that need it.

## Part 1: AWS (instance, Elastic IP, security group)

1. **Find the instance.** Ask Ofir which one if it is not obvious, then record its ID, region, OS, instance type, VPC, subnet and security groups.
   - It needs about 512 MB RAM or more and 10 GB of free disk.
   - The OS must be Amazon Linux 2023 or Ubuntu 22.04/24.04.
   - IMDSv2 must be enabled. If it is not, `setup.sh` needs `--ip` and an explicit `SOLYA_SUBNET`.
2. **Elastic IP.** If the instance has no Elastic IP, allocate one and associate it with the instance. A changing IP would change the HTTPS address and the SolarEdge redirect URI. Note the resulting public IPv4.
3. **Security group, inbound rules.** Add only what is missing:
   - TCP 80 from 0.0.0.0/0, and ::/0 if the VPC uses IPv6. Let's Encrypt needs port 80 to issue the certificate.
   - TCP 443 from 0.0.0.0/0, and ::/0 if IPv6.
   - TCP 22 from Ofir's own IP only, if not already allowed.
   - **Do not open port 8000.**

   If the instance's security group is shared with other instances, point that out before you change it.
4. **Network range check.** Ask Ofir whether `192.168.250.0/24` is used anywhere his VPC can reach: peered VPCs, a VPN or a Transit Gateway. The script checks the VPC itself but cannot see those.
   - If the range is in use, or he isn't sure, agree a free range such as `192.168.251.0/24`.
   - Pass the agreed range on the first run as `SOLYA_SUBNET=<range>`.

## Part 2: Install on the instance

SSH in as the login user, then:

```bash
sudo dnf install -y git        # Amazon Linux 2023
# or: sudo apt-get update && sudo apt-get install -y git   # Ubuntu
```

```bash
sudo install -d -m 0755 -o "$USER" -g "$(id -gn)" /opt/solya
```

```bash
git clone --branch main https://github.com/OfirAmir/solya.git /opt/solya
```

Preview first. This changes nothing:

```bash
/opt/solya/deploy/ec2/setup.sh --dry-run
```

If the preview reports no blocking problem, run it for real. Prefix it with `SOLYA_SUBNET=<range>` if you agreed a range in Part 1.4:

```bash
/opt/solya/deploy/ec2/setup.sh
```

The script:
- installs Docker only if it is missing;
- pulls `ghcr.io/ofiramir/solya:latest`;
- checks the Hebrew card rendering inside the image;
- creates `/opt/solya/.env` (mode 600) and fills in the automatic keys;
- starts Caddy.

At the end it prints three things: the **host** (for example `3-120-45-6.sslip.io`), the **SolarEdge redirect URI** (`https://<host>/oauth/callback`), and the list of **empty `.env` keys** still to fill. Send Ofir the host and the redirect URI. The redirect URI goes to Oren for his SolarEdge app.

## Part 3: Fill in `/opt/solya/.env` (Ofir does this himself)

Tell Ofir to open the file on the server with `nano /opt/solya/.env`. **Prompt him for each key below, one at a time.** He types each value into the file himself. Do not ask him to paste any value to you.

**Required.** Solya will not start without these:

| Key | Where the value comes from |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | The teammate who created the bot with BotFather sends the token to Ofir privately. |
| `TELEGRAM_BOT_USERNAME` | The bot's username, **without** the `@`. |
| `SOLAREDGE_CLIENT_ID` | From Oren. This is the Solya developer app (route B in the guide, step 4). |
| `SOLAREDGE_CLIENT_SECRET` | From Oren, sent privately. He must **copy** the existing secret, **not regenerate** it, or the vendor's running system stops. |

Oren must also set that app's **redirect URI** to exactly `https://<host>/oauth/callback`.

**Strongly recommended:**

| Key | Value |
| --- | --- |
| `ADMIN_PASSWORD` | A long passphrase for the read-only `https://<host>/admin` page. While this is empty, `/admin` returns 404. |
| `ADMIN_CHAT_IDS` | JSON list of Telegram chat ids that receive admin notices, for example `[123456789]`. Get an id by messaging @userinfobot. |
| `BUDGET_MONTHLY_CREDITS` | `20000` while the Solya app is on the Starter plan (until 17 Dec 2026). After that date, change it to `2000`. |
| `BUDGET_BILLING_DAY` | `17` |

**Leave these alone.** `setup.sh` wrote them:
- `PUBLIC_BASE_URL`
- `TOKEN_ENCRYPTION_KEY`
- `SOLYA_IMAGE`
- `COMPOSE_FILE`
- `COMPOSE_PROJECT_NAME`
- `SOLYA_SUBNET`
- `SOLYA_IP_RANGE`
- `SOLYA_CADDY_IP`

**Do not set `TRUSTED_PROXIES`.** Also leave the `LLM_*` keys unset, so AI answers stay off unless Oren decides otherwise.

Formatting: there are no spaces around `=`. Wrap a value in single quotes if it contains `$`, `#` or spaces.

**Keep a copy of `TOKEN_ENCRYPTION_KEY`** in a password manager. If it is lost, every stored SolarEdge connection must be redone.

Before moving on, confirm that the four required keys are non-empty without printing their values:

```bash
for k in TELEGRAM_BOT_TOKEN TELEGRAM_BOT_USERNAME SOLAREDGE_CLIENT_ID SOLAREDGE_CLIENT_SECRET; do grep -q "^$k=..*" /opt/solya/.env && echo "$k set" || echo "$k MISSING"; done
```

## Part 4: Start, check and create an invite

```bash
/opt/solya/deploy/ec2/setup.sh
```

Re-running the script pulls the newest image, starts everything and checks the health URL. Then confirm:

```bash
/opt/solya/deploy/ec2/compose ps
```

```bash
curl https://<host>/healthz
```

Both containers must be up, and `https://<host>/healthz` must load in a browser with a valid padlock. If it does not, use the guide's Troubleshooting section.

Create Oren's one-time invite:

```bash
/opt/solya/deploy/ec2/compose exec solya solya invite create
```

It prints a `telegram:` link and a `web:` link for the same token. Give Oren one of them.

## Report back to Ofir and the team

- Instance ID, region, and the Elastic IP.
- The security-group rules you added.
- The host and the redirect URI.
- Which `.env` keys are set, by name only and never with values.
- The health check result.
- The two invite links.
- Anything that stopped or warned, quoting the script's message.

Next steps after Oren connects:
1. First-run verification: compare `solya pull` with the SolarEdge portal (guide step 9).
2. Set labels, tariffs and kW (guide step 10).
