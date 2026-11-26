# Solya on an Azure VM

`create-vm.sh` runs on your own machine. It creates an Ubuntu 24.04 VM in Azure (Israel Central by default) and installs Solya on it with the EC2 kit, `deploy/ec2/setup.sh`, which also runs on any Ubuntu VM. Caddy serves HTTPS on the VM's stable Azure DNS name, `<label>.israelcentral.cloudapp.azure.com`, with a Let's Encrypt certificate.

The script never handles secrets. You type them into `/opt/solya/.env` on the VM yourself.

## Prerequisites

- The Azure CLI (`az`), logged in with `az login`. Pick the subscription first with `az account set --subscription <id>` if you have several.
- An OpenSSH client (`ssh`) and `curl`.
- An SSH key. The script uses `~/.ssh/id_ed25519.pub` if it exists. Otherwise az uses `~/.ssh/id_rsa.pub`, and creates it if it is missing.

## Run it

Preview first. This changes nothing, though it runs read-only `az` queries:

```bash
deploy/azure/create-vm.sh --dry-run
```

Then create and deploy:

```bash
deploy/azure/create-vm.sh
```

It shows the subscription and asks before going on (`--yes` skips the question). It checks that the region offers the VM size. If it doesn't, it lists the B-series sizes that are offered and stops.

It then creates each of the following, skipping any that already exist. Every resource is tagged `app=solya`. An existing resource group is used only if it already carries the tag `app=solya`; any other group is left alone and the script stops (see Troubleshooting).

| Resource | Default name | Notes |
| --- | --- | --- |
| Resource group | `solya-rg` | in `israelcentral` |
| Public IP | `solya-vm-ip` | Standard SKU, static, DNS label `solya-<6 random chars>` |
| Network security group | `solya-vm-nsg` | inbound TCP 80 and 443 from `Internet`; TCP 22 from your current public IPv4 only. **Port 8000 is never opened.** The VM is created with `--nsg-rule NONE`, so az adds no rule of its own, and the script then checks that the VM's network interface uses this NSG. |
| VM | `solya-vm` | `Standard_B1ms`, `Canonical:ubuntu-24_04-lts:server:latest`, user `azureuser` |
| VNet and subnet | `solya-vm-vnet`, `solya-vm-subnet` | `10.10.0.0/16` and `10.10.0.0/24` |

The DNS label is saved in `~/.solya-azure.env`, so a re-run keeps the same name. A changed name would change the HTTPS address and the SolarEdge redirect URI.

On the VM, over SSH, it:

1. waits for cloud-init;
2. installs git, curl and iproute2 if any is missing (`setup.sh` needs `ss` and `ip`);
3. clones the repo into `/opt/solya`, or pulls it if it is already there;
4. creates `/opt/solya/.env` (mode 600) with `PUBLIC_BASE_URL=https://<fqdn>`, so Caddy uses the Azure name instead of sslip.io (`setup.sh` never overwrites a value that is already set);
5. runs `SOLYA_SUBNET=192.168.250.0/24 /opt/solya/deploy/ec2/setup.sh --ip <public ip>`.

Azure has no AWS metadata service. That is why the script passes the IP and the Docker subnet explicitly. The subnet is outside the `10.10.0.0/16` VNet.

When it has just created the VM (not when it reuses one), it first removes any old SSH host keys for the FQDN and the IP from `~/.ssh/known_hosts` (`ssh-keygen -R`), since a new VM has new keys.

At the end it prints the FQDN, the redirect URI `https://<fqdn>/oauth/callback` and the SSH command.

Options: `--resource-group`, `--location`, `--name`, `--size`, `--ssh-source`, `--image`, `--admin-user`, `--ssh-key`, `--dns-label`, `--yes`, `--dry-run`, `--destroy`. Each has an environment variable too, such as `SOLYA_AZ_SIZE`. See `--help`.

### Cost

`Standard_B1ms` (1 vCPU, 2 GB RAM) is roughly $15–20 a month. The static Standard public IP and the OS disk cost a few dollars more. Check the Azure pricing page for current prices in your region. A stopped but not deallocated VM still costs money. `--destroy` removes everything.

## Fill in `.env`

Follow `deploy/ec2/HANDOFF-PROMPT.md`, Part 3. Open the file on the VM:

```bash
ssh -t azureuser@<fqdn> nano /opt/solya/.env
```

**Required:**

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_BOT_USERNAME`, without the `@`
- `SOLAREDGE_CLIENT_ID`
- `SOLAREDGE_CLIENT_SECRET`. Copy the existing secret; do not regenerate it.

The SolarEdge app's redirect URI must be exactly `https://<fqdn>/oauth/callback`, and it must be the app's **default** redirect URI. SolarEdge ignores the other entries and sends the owner to the default one (observed live on 26 Nov: owners landed on an old URL).

**Recommended:**

- `ADMIN_PASSWORD`
- `ADMIN_CHAT_IDS`, for example `[123456789]`
- `BUDGET_MONTHLY_CREDITS=20000` while on the Starter plan (until 17 Dec 2026), then `2000`
- `BUDGET_BILLING_DAY=17`

Leave `PUBLIC_BASE_URL`, `TOKEN_ENCRYPTION_KEY`, `SOLYA_*` and `COMPOSE_*` as they were written. Keep a copy of `TOKEN_ENCRYPTION_KEY` in a password manager. Never paste a secret into a chat, a commit or a command line.

Apply the changes by re-running `setup.sh`. On Azure, always pass `--ip`:

```bash
ssh azureuser@<fqdn> /opt/solya/deploy/ec2/setup.sh --ip <public ip>
```

Re-running `deploy/azure/create-vm.sh` does the same.

Then create the one-time invite, on the VM:

```bash
/opt/solya/deploy/ec2/compose exec solya solya invite create
```

## Update

Re-run `deploy/azure/create-vm.sh`. It pulls the repo, and `setup.sh` pulls the newest image and restarts the stack. Or, on the VM:

```bash
/opt/solya/deploy/ec2/setup.sh --ip <public ip>
```

## Destroy

```bash
deploy/azure/create-vm.sh --destroy
```

It refuses a resource group that is not tagged `app=solya`. Otherwise it lists the resources and asks you to type the resource group name, then deletes the whole group, including the database on the VM. Only after a successful delete does it remove the DNS label from `~/.solya-azure.env`.

## Troubleshooting

- **No certificate, or `https://<fqdn>` does not load.** Let's Encrypt must reach the VM on port 80.
  - Check the NSG rules with `az network nsg rule list -g solya-rg --nsg-name solya-vm-nsg -o table`.
  - Check that the name resolves to the IP: `getent hosts <fqdn>`. A new DNS label can take a few minutes to resolve.
  - Then re-run `setup.sh`. Caddy retries by itself.
  - Logs: `/opt/solya/deploy/ec2/compose logs caddy`.
- **SSH times out.** Your public IP has probably changed since the NSG rule was made, or your connection leaves through more than one address (dual WAN, load balancing). By default the script asks api.ipify.org six times and allows every address it sees. Re-running it updates the rule in place. To set the addresses yourself, pass a comma-separated list. Each entry is an address or a /24–/32 range. The rule is set to exactly this list:
  ```bash
  deploy/azure/create-vm.sh --ssh-source 203.0.113.10,198.51.100.20
  ```
- **`REMOTE HOST IDENTIFICATION HAS CHANGED`, or "SSH refused ...'s host key".** `known_hosts` has another key for the name or the IP, usually because the VM was recreated by hand or the address was reused. The script clears old keys itself only for a VM it has just created. If you trust the new VM, remove the old keys and re-run:
  ```bash
  ssh-keygen -R <fqdn>; ssh-keygen -R <public ip>
  ```
- **"resource group ... is not tagged app=solya".** The group exists but was not made by this script, so it may hold other resources. The script will neither use nor `--destroy` it. Pick another name with `--resource-group`. If the group really is Solya's, tag it first: `az group update -n <rg> --set tags.app=solya`.
- **"network interface is not bound to the NSG".** After creating the VM, the script checks that its NIC uses `solya-vm-nsg`. If not, the VM's open ports are not the ones the script set. Check with `az network nic show --ids <nic id> --query networkSecurityGroup.id`, and fix it, or run `--destroy` and start over.
- **macOS.** The script runs with the bash 3.2 that macOS ships (`/bin/bash`); no newer bash is needed.
- **The size is not available.** Pick one of the listed sizes with `--size`, or use another region with `--location`.
- **`setup.sh` stops on a check.** Read its message, and see Troubleshooting in `deploy/ec2/README.md`. Without `--ip` it stops with "could not read the public IPv4 from EC2 metadata". On Azure that is expected: pass `--ip`.
