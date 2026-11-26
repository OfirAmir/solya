#!/usr/bin/env bash
# Create an Ubuntu 24.04 VM in Azure (Israel Central by default) and deploy Solya on it
# with the EC2 kit (deploy/ec2/setup.sh), which runs on any Ubuntu VM.
#
# Run it on your own machine as a normal user, after `az login`. Safe to re-run: every
# Azure resource is created only when it is missing, and the remote steps are idempotent.
# It handles no secrets: you type those into /opt/solya/.env on the VM yourself.
#
#   deploy/azure/create-vm.sh [--dry-run] [--yes] [--resource-group RG] [--location LOC]
#                             [--size SIZE] [--name VM] [--ssh-source A.B.C.D]
#   deploy/azure/create-vm.sh --destroy [--resource-group RG]
#
# See deploy/azure/README.md.

set -euo pipefail

STATE_FILE="${SOLYA_AZ_STATE_FILE:-$HOME/.solya-azure.env}"
# The state file holds only the DNS label: plain KEY=value lines, read without sourcing.
state_get() {
    [[ -f "$STATE_FILE" ]] || return 0
    grep -E "^$1=" "$STATE_FILE" | tail -n1 | cut -d= -f2- || true
}

RG="${SOLYA_AZ_RESOURCE_GROUP:-solya-rg}"
LOCATION="${SOLYA_AZ_LOCATION:-israelcentral}"
VM="${SOLYA_AZ_VM_NAME:-solya-vm}"
SIZE="${SOLYA_AZ_SIZE:-Standard_B1ms}"
IMAGE="${SOLYA_AZ_IMAGE:-Canonical:ubuntu-24_04-lts:server:latest}"
ADMIN="${SOLYA_AZ_ADMIN_USER:-azureuser}"
SSH_KEY="${SOLYA_AZ_SSH_KEY:-$HOME/.ssh/id_ed25519.pub}"
SSH_SOURCE="${SOLYA_AZ_SSH_SOURCE:-}"
DNS_LABEL="${SOLYA_AZ_DNS_LABEL:-}"
REPO_URL="${SOLYA_AZ_REPO:-https://github.com/OfirAmir/solya.git}"
BRANCH="${SOLYA_AZ_BRANCH:-main}"
# The kit's Docker network. It must not overlap the VNet (10.10.0.0/16 below); there is no
# AWS metadata service on Azure, so setup.sh cannot check the VNet itself.
SOLYA_SUBNET_VALUE="${SOLYA_AZ_DOCKER_SUBNET:-192.168.250.0/24}"
VNET_PREFIX="10.10.0.0/16"
SUBNET_PREFIX="10.10.0.0/24"
TAG="app=solya"

DRY_RUN=0
YES=0
DESTROY=0

log()  { printf '\n==> %s\n' "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '\n!!  %s\n' "$*" >&2; }
die()  { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<EOF
Usage: $0 [options]

Creates (if missing) a resource group, a static Standard public IP with a DNS label, an
NSG (80/443 from Internet, 22 from your IP), and an Ubuntu 24.04 VM, then installs Solya
on it with deploy/ec2/setup.sh over SSH.

  --resource-group RG   resource group           (default: solya-rg;       env SOLYA_AZ_RESOURCE_GROUP)
  --location LOC        Azure region             (default: israelcentral;  env SOLYA_AZ_LOCATION)
  --name VM             VM name                  (default: solya-vm;       env SOLYA_AZ_VM_NAME)
  --size SIZE           VM size                  (default: Standard_B1ms;  env SOLYA_AZ_SIZE)
  --image URN           image                    (default: $IMAGE; env SOLYA_AZ_IMAGE)
  --admin-user NAME     VM login user            (default: azureuser;      env SOLYA_AZ_ADMIN_USER)
  --ssh-key FILE.pub    public key for the VM    (default: ~/.ssh/id_ed25519.pub if present,
                                                  else az --generate-ssh-keys; env SOLYA_AZ_SSH_KEY)
  --ssh-source LIST     comma-separated IPv4 addresses or CIDRs (/24 to /32) allowed to SSH
                        in (default: every public IPv4 api.ipify.org reports over a few
                        tries, which covers dual-WAN and load-balanced connections; env
                        SOLYA_AZ_SSH_SOURCE). The SSH rule is set to exactly this list on
                        every run.
  --dns-label LABEL     DNS label (default: solya-<6 random chars>, saved in $STATE_FILE;
                        env SOLYA_AZ_DNS_LABEL)
  --dry-run             print the az and ssh commands; change nothing (read-only az queries
                        such as 'az account show' still run)
  --yes                 do not ask before using the current subscription
  --destroy             delete the resource group (asks you to type its name)
  -h, --help            this help
EOF
}

need_val() { [[ $# -ge 2 && -n "$2" ]] || die "$1 needs a value"; }

parse_args() {
    while (( $# )); do
        case "$1" in
            --resource-group) need_val "$@"; RG="$2"; shift 2 ;;
            --location)       need_val "$@"; LOCATION="$2"; shift 2 ;;
            --name)           need_val "$@"; VM="$2"; shift 2 ;;
            --size)           need_val "$@"; SIZE="$2"; shift 2 ;;
            --image)          need_val "$@"; IMAGE="$2"; shift 2 ;;
            --admin-user)     need_val "$@"; ADMIN="$2"; shift 2 ;;
            --ssh-key)        need_val "$@"; SSH_KEY="$2"; shift 2 ;;
            --ssh-source)     need_val "$@"; SSH_SOURCE="$2"; shift 2 ;;
            --dns-label)      need_val "$@"; DNS_LABEL="$2"; shift 2 ;;
            --dry-run)        DRY_RUN=1; shift ;;
            --yes|-y)         YES=1; shift ;;
            --destroy)        DESTROY=1; shift ;;
            -h|--help)        usage; exit 0 ;;
            *) usage >&2; die "unknown argument: $1" ;;
        esac
    done
    LOCATION="$(tr '[:upper:]' '[:lower:]' <<< "$LOCATION")"
}

# Print a command; run it unless --dry-run.
run() {
    if (( DRY_RUN )); then
        printf '    [dry-run] %s\n' "$(printf '%q ' "$@")"
    else
        printf '    + %s\n' "$(printf '%q ' "$@")"
        "$@"
    fi
}

# A read-only az query: runs in dry-run too. Quiet on failure ("not found").
azq() { az "$@" 2>/dev/null; }

confirm() {  # prompt; true on y/yes
    local ans
    read -r -p "$1 [y/N] " ans || return 1
    [[ "$ans" =~ ^[Yy]([Ee][Ss])?$ ]]
}

valid_ipv4() {
    local o
    [[ "$1" =~ ^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})$ ]] || return 1
    for o in "${BASH_REMATCH[@]:1}"; do (( 10#$o <= 255 )) || return 1; done
}

# ----------------------------------------------------------------------------- preflight

check_tools() {
    local t
    for t in az ssh curl; do
        command -v "$t" >/dev/null 2>&1 || die "'$t' is not installed (needed: Azure CLI, OpenSSH client, curl)"
    done
    [[ "$(id -u)" -ne 0 ]] || warn "running as root; run this as your normal user so the SSH keys and $STATE_FILE are yours"
}

check_account() {
    log "Azure account"
    local name id user
    id="$(az account show --query id -o tsv 2>/dev/null)" && [[ -n "$id" ]] \
        || die "'az account show' failed. Run 'az login' first (and 'az account set --subscription <id>' to pick one)."
    name="$(azq account show --query name -o tsv || true)"
    user="$(azq account show --query user.name -o tsv || true)"
    info "subscription: $name"
    info "id:           $id"
    info "signed in as: ${user:-?}"
    if (( ! YES )); then
        confirm "Use this subscription?" || die "stopped. Pick another with: az account set --subscription <id>"
    fi
}

check_sku() {
    log "VM size $SIZE in $LOCATION"
    # list-skus already drops sizes restricted for the whole region; the filter also
    # demands the exact name (--size is a substring match) and no Location restriction.
    # A Zone-only restriction does not matter here: the VM is not zonal.
    local ok
    ok="$(az vm list-skus -l "$LOCATION" --size "$SIZE" --resource-type virtualMachines \
        --query "[?name=='$SIZE' && length(restrictions[?type=='Location']) == \`0\`].name" -o tsv 2>/dev/null)" \
        || die "'az vm list-skus -l $LOCATION' failed. Is '$LOCATION' a valid region (az account list-locations -o table)?"
    if [[ -z "$ok" ]]; then
        warn "$SIZE is not offered to this subscription in $LOCATION."
        info "B-series sizes that are:"
        az vm list-skus -l "$LOCATION" --size Standard_B --resource-type virtualMachines \
            --query "[?restrictions==\`[]\`].name" -o tsv 2>/dev/null | sort -V | sed 's/^/      /' || true
        die "pick one with --size <name> and re-run (or try another region with --location)."
    fi
    info "available"
}

SSH_SOURCES=()   # normalised CIDRs for the SSH rule

resolve_ssh_source() {
    local raw=() item ip prefix seen=" "
    if [[ -z "$SSH_SOURCE" ]]; then
        # A connection with several exits (dual WAN, load balancing) shows a different
        # public IP per request, so ask a few times and allow every address seen.
        local i got
        for i in 1 2 3 4 5 6; do
            got="$(curl -fsS --max-time 10 https://api.ipify.org 2>/dev/null || true)"
            valid_ipv4 "$got" && raw+=("$got")
            (( i < 6 )) && sleep 1
        done
        if (( ${#raw[@]} == 0 )); then
            if (( DRY_RUN )); then
                warn "could not detect your public IPv4 (api.ipify.org); the plan shows a placeholder"
                raw=(203.0.113.10)
            else
                die "could not detect your public IPv4 via api.ipify.org. Pass it with --ssh-source A.B.C.D[,E.F.G.H]"
            fi
        fi
    else
        IFS=',' read -r -a raw <<< "${SSH_SOURCE// /}"
    fi
    SSH_SOURCES=()
    for item in "${raw[@]}"; do
        [[ -n "$item" ]] || continue
        ip="${item%/*}"
        prefix=32
        [[ "$item" == */* ]] && prefix="${item#*/}"
        valid_ipv4 "$ip" || die "--ssh-source '$item' is not an IPv4 address or CIDR"
        if ! [[ "$prefix" =~ ^[0-9]+$ ]] || (( prefix < 24 || prefix > 32 )); then
            die "--ssh-source '$item': the prefix must be /24 to /32 (SSH is never opened wider)"
        fi
        item="$ip/$prefix"
        [[ "$seen" == *" $item "* ]] && continue
        seen+="$item "
        SSH_SOURCES+=("$item")
    done
    (( ${#SSH_SOURCES[@]} > 0 )) || die "--ssh-source is empty"
    SSH_SOURCE="$(IFS=','; echo "${SSH_SOURCES[*]}")"
    SSH_SOURCE="${SSH_SOURCE//,/, }"
    info "SSH allowed from: $SSH_SOURCE"
}

# The DNS label: --dns-label/env, else the one on an existing public IP, else the state
# file, else a new random one (saved to the state file before it is used).
resolve_dns_label() {
    local existing
    existing="$(azq network public-ip show -g "$RG" -n "$VM-ip" --query dnsSettings.domainNameLabel -o tsv || true)"
    if [[ -n "$DNS_LABEL" ]]; then
        [[ -z "$existing" || "$existing" == "$DNS_LABEL" ]] \
            || die "public IP $VM-ip already has DNS label '$existing', not '$DNS_LABEL'. Drop --dns-label to keep it."
    elif [[ -n "$existing" ]]; then
        DNS_LABEL="$existing"
    else
        DNS_LABEL="$(state_get SOLYA_AZ_DNS_LABEL)"
    fi
    if [[ -z "$DNS_LABEL" ]]; then
        local rand
        rand="$(LC_ALL=C tr -dc 'a-z0-9' < /dev/urandom | head -c 6 || true)"
        DNS_LABEL="solya-$rand"
        info "new DNS label: $DNS_LABEL"
    fi
    [[ "$DNS_LABEL" =~ ^[a-z][a-z0-9-]{1,61}[a-z0-9]$ ]] \
        || die "DNS label '$DNS_LABEL' is invalid: 3-63 chars, lowercase letters, digits and '-', starting with a letter"
    save_label "$DNS_LABEL"
}

save_label() {
    [[ "$(state_get SOLYA_AZ_DNS_LABEL)" != "$1" ]] || return 0
    if (( DRY_RUN )); then
        info "[dry-run] save SOLYA_AZ_DNS_LABEL=$1 in $STATE_FILE"
        return
    fi
    local tmp
    tmp="$(mktemp "$STATE_FILE.XXXXXX")"
    { [[ -f "$STATE_FILE" ]] && grep -vE '^SOLYA_AZ_DNS_LABEL=' "$STATE_FILE" || true
      printf 'SOLYA_AZ_DNS_LABEL=%s\n' "$1"; } > "$tmp"
    mv -f "$tmp" "$STATE_FILE"
    info "saved DNS label in $STATE_FILE"
}

# ----------------------------------------------------------------------------- Azure resources

ensure_group() {
    log "Resource group $RG ($LOCATION)"
    if [[ "$(azq group exists -n "$RG" || echo false)" == true ]]; then
        local loc
        loc="$(azq group show -n "$RG" --query location -o tsv || true)"
        info "exists${loc:+ (in $loc)}"
        [[ -z "$loc" || "$loc" == "$LOCATION" ]] \
            || warn "the group is in $loc; the VM and its resources will still go to $LOCATION"
        run az group update -n "$RG" --set tags.app=solya -o none
    else
        run az group create -n "$RG" -l "$LOCATION" --tags "$TAG" -o none
    fi
}

ensure_public_ip() {
    log "Public IP $VM-ip (static, Standard, DNS label $DNS_LABEL)"
    if azq network public-ip show -g "$RG" -n "$VM-ip" -o none; then
        info "exists"
    else
        run az network public-ip create -g "$RG" -n "$VM-ip" -l "$LOCATION" \
            --sku Standard --allocation-method Static --version IPv4 \
            --dns-name "$DNS_LABEL" --tags "$TAG" -o none
    fi
}

# Create the rule, or update an existing one in place (so a changed --ssh-source applies).
# Sorted, /32-normalised form of a list of address prefixes, for comparing rule sources.
norm_prefixes() {
    local p
    for p in "$@"; do
        [[ -n "$p" && "$p" != "None" ]] || continue
        if valid_ipv4 "$p"; then echo "$p/32"; else echo "$p"; fi
    done | sort -u | tr '\n' ' '
}

# Create the rule, or update an existing one in place so its sources are exactly the
# given list (a changed --ssh-source, or addresses added by hand, are reconciled).
ensure_rule() {  # name priority port source...
    local name="$1" prio="$2" port="$3"; shift 3
    local src=("$@") one many cur want
    if one="$(azq network nsg rule show -g "$RG" --nsg-name "$VM-nsg" -n "$name" \
            --query sourceAddressPrefix -o tsv)"; then
        many="$(azq network nsg rule show -g "$RG" --nsg-name "$VM-nsg" -n "$name" \
            --query 'sourceAddressPrefixes[]' -o tsv || true)"
        # shellcheck disable=SC2086 # word-split the tsv lists on purpose
        cur="$(norm_prefixes $one $many)"
        want="$(norm_prefixes "${src[@]}")"
        if [[ "$cur" == "$want" ]]; then
            info "rule $name: present (TCP $port from ${want% })"
            return
        fi
        run az network nsg rule update -g "$RG" --nsg-name "$VM-nsg" -n "$name" \
            --source-address-prefixes "${src[@]}" -o none
    else
        run az network nsg rule create -g "$RG" --nsg-name "$VM-nsg" -n "$name" \
            --priority "$prio" --direction Inbound --access Allow --protocol Tcp \
            --source-address-prefixes "${src[@]}" --source-port-ranges '*' \
            --destination-address-prefixes '*' --destination-port-ranges "$port" -o none
    fi
}

ensure_nsg() {
    log "Network security group $VM-nsg (80, 443 from Internet; 22 from $SSH_SOURCE; never 8000)"
    if azq network nsg show -g "$RG" -n "$VM-nsg" -o none; then
        info "exists"
    else
        run az network nsg create -g "$RG" -n "$VM-nsg" -l "$LOCATION" --tags "$TAG" -o none
    fi
    ensure_rule allow-http  1010 80  Internet
    ensure_rule allow-https 1020 443 Internet
    ensure_rule allow-ssh   1030 22  "${SSH_SOURCES[@]}"
    # Port 8000 (uvicorn) is never opened: Caddy reaches it over the Docker network only.
    local open8000
    open8000="$(azq network nsg rule list -g "$RG" --nsg-name "$VM-nsg" \
        --query "[?direction=='Inbound' && access=='Allow' && (destinationPortRange=='8000' || destinationPortRange=='*' || contains(destinationPortRanges, '8000'))].name" -o tsv || true)"
    [[ -z "$open8000" ]] || warn "NSG rule(s) $open8000 allow port 8000 (or all ports) inbound. Remove them: Solya must only be reached through Caddy."
}

SSH_KEY_ARGS=()
SSH_ID_ARGS=()
# ssh uses the private key next to the chosen .pub; with --generate-ssh-keys (id_rsa),
# ssh finds it by default.
set_ssh_identity() {
    if [[ "$SSH_KEY" == *.pub && -f "$SSH_KEY" && -f "${SSH_KEY%.pub}" ]]; then
        SSH_ID_ARGS=(-i "${SSH_KEY%.pub}")
    fi
}
pick_ssh_key() {
    if [[ -f "$SSH_KEY" ]]; then
        SSH_KEY_ARGS=(--ssh-key-values "$SSH_KEY")
    else
        info "no $SSH_KEY; az will use ~/.ssh/id_rsa.pub, creating it if missing (--generate-ssh-keys)"
        SSH_KEY_ARGS=(--generate-ssh-keys)
    fi
}

ensure_vm() {
    log "VM $VM ($SIZE, $IMAGE)"
    if azq vm show -g "$RG" -n "$VM" -o none; then
        info "exists"
        local state
        state="$(azq vm get-instance-view -g "$RG" -n "$VM" \
            --query "instanceView.statuses[?starts_with(code, 'PowerState/')].code | [0]" -o tsv || true)"
        if [[ -n "$state" && "$state" != PowerState/running ]]; then
            info "power state: $state; starting it"
            run az vm start -g "$RG" -n "$VM" -o none
        fi
        return
    fi
    pick_ssh_key
    # The public IP and NSG above are referenced by name, so az creates neither its own IP
    # nor its default NSG (which would open 22 to everyone). The VNet is new and explicit.
    run az vm create -g "$RG" -n "$VM" -l "$LOCATION" \
        --image "$IMAGE" --size "$SIZE" \
        --admin-username "$ADMIN" "${SSH_KEY_ARGS[@]}" \
        --public-ip-address "$VM-ip" --public-ip-sku Standard \
        --nsg "$VM-nsg" \
        --vnet-name "$VM-vnet" --vnet-address-prefix "$VNET_PREFIX" \
        --subnet "$VM-subnet" --subnet-address-prefix "$SUBNET_PREFIX" \
        --tags "$TAG" -o none
}

# az vm create tags the VM, NIC and VNet, but not the OS disk; tag everything in the group.
tag_all() {
    log "Tagging every resource in $RG with $TAG"
    if (( DRY_RUN )); then
        info "[dry-run] for each id in \$(az resource list -g $RG --query '[].id' -o tsv): az resource tag --ids <id> --tags $TAG --is-incremental -o none"
        return
    fi
    local id ids
    ids="$(az resource list -g "$RG" --query "[?tags.app!='solya'].id" -o tsv)"
    if [[ -z "$ids" ]]; then info "all tagged"; return; fi
    while IFS= read -r id; do
        [[ -n "$id" ]] || continue
        run az resource tag --ids "$id" --tags "$TAG" --is-incremental -o none
    done <<< "$ids"
}

FQDN=""
PUBLIC_IP=""
read_address() {
    log "Address"
    if (( DRY_RUN )); then
        FQDN="$(azq network public-ip show -g "$RG" -n "$VM-ip" --query dnsSettings.fqdn -o tsv || true)"
        PUBLIC_IP="$(azq network public-ip show -g "$RG" -n "$VM-ip" --query ipAddress -o tsv || true)"
        FQDN="${FQDN:-$DNS_LABEL.$LOCATION.cloudapp.azure.com}"
        PUBLIC_IP="${PUBLIC_IP:-<public-ip>}"
    else
        FQDN="$(az network public-ip show -g "$RG" -n "$VM-ip" --query dnsSettings.fqdn -o tsv)"
        PUBLIC_IP="$(az network public-ip show -g "$RG" -n "$VM-ip" --query ipAddress -o tsv)"
        [[ -n "$FQDN" ]] || die "public IP $VM-ip has no DNS name"
        valid_ipv4 "$PUBLIC_IP" || die "public IP $VM-ip has no IPv4 address yet ('$PUBLIC_IP')"
    fi
    info "FQDN: $FQDN"
    info "IP:   $PUBLIC_IP"
}

# ----------------------------------------------------------------------------- remote install

SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30)

wait_for_ssh() {
    log "Waiting for SSH on $ADMIN@$FQDN"
    if (( DRY_RUN )); then
        info "[dry-run] $(printf '%q ' ssh "${SSH_ID_ARGS[@]}" "${SSH_OPTS[@]}" -o BatchMode=yes -o ConnectTimeout=5 "$ADMIN@$FQDN" true)(retried for up to 5 minutes)"
        return
    fi
    local i
    for i in $(seq 1 60); do
        if ssh "${SSH_ID_ARGS[@]}" "${SSH_OPTS[@]}" -o BatchMode=yes -o ConnectTimeout=5 \
                "$ADMIN@$FQDN" true 2>/dev/null; then
            info "SSH is up"
            return
        fi
        (( i % 6 )) || info "still waiting ($(( i * 5 ))s)..."
        sleep 5
    done
    die "no SSH on $ADMIN@$FQDN after 5 minutes. If your public IP changed, re-run with --ssh-source <your IPv4>. Try by hand: ssh $ADMIN@$FQDN"
}

# Runs on the VM as $ADMIN. Quoted heredoc: nothing expands locally; values come in as
# arguments. Everything is inside main(), so bash has read the whole script before any
# command could read stdin.
REMOTE_SCRIPT=$(cat <<'REMOTE'
set -euo pipefail
main() {
    local fqdn="$1" ip="$2" subnet="$3" repo="$4" branch="$5" dir=/opt/solya
    echo "==> On $(hostname): waiting for cloud-init to finish"
    cloud-init status --wait >/dev/null 2>&1 || true
    sudo -n true 2>/dev/null || { echo "ERROR: $USER needs passwordless sudo (setup.sh calls sudo itself)" >&2; exit 1; }

    # Fresh Ubuntu runs apt timers at boot; wait for the dpkg lock instead of failing.
    # This VM is dedicated to Solya, so a host-wide apt setting is fine here.
    echo 'DPkg::Lock::Timeout "600";' | sudo tee /etc/apt/apt.conf.d/90solya-lock-timeout >/dev/null

    # setup.sh needs git, curl, and ss/ip from iproute2 (port and route checks).
    if ! command -v git >/dev/null || ! command -v curl >/dev/null \
            || ! command -v ss >/dev/null || ! command -v ip >/dev/null; then
        echo "==> Installing git, curl, iproute2"
        sudo DEBIAN_FRONTEND=noninteractive apt-get update -q
        sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-remove git curl iproute2 ca-certificates
    fi

    sudo install -d -m 0755 -o "$USER" -g "$(id -gn)" "$dir"
    if [[ -d "$dir/.git" ]]; then
        echo "==> Updating $dir"
        git -C "$dir" pull --ff-only origin "$branch"
    else
        echo "==> Cloning $repo ($branch) into $dir"
        git clone --branch "$branch" "$repo" "$dir"
    fi

    # PUBLIC_BASE_URL on the Azure name, set before setup.sh runs: setup.sh never
    # overwrites a value already in .env, so Caddy gets this name instead of sslip.io.
    local want="https://$fqdn" cur=""
    if [[ ! -f "$dir/.env" ]]; then
        install -m 600 "$dir/.env.example" "$dir/.env"
    fi
    chmod 600 "$dir/.env"
    cur="$(grep -E '^[[:space:]]*PUBLIC_BASE_URL=' "$dir/.env" | tail -n1 | cut -d= -f2- || true)"
    if [[ -z "$cur" || "$cur" == https://solya.example.com ]]; then
        if grep -qE '^[[:space:]]*PUBLIC_BASE_URL=' "$dir/.env"; then
            sed -i -E "s|^[[:space:]]*PUBLIC_BASE_URL=.*|PUBLIC_BASE_URL=$want|" "$dir/.env"
        else
            printf 'PUBLIC_BASE_URL=%s\n' "$want" >> "$dir/.env"
        fi
        echo "    set PUBLIC_BASE_URL=$want"
    elif [[ "$cur" != "$want" ]]; then
        echo "!!  PUBLIC_BASE_URL in $dir/.env is '$cur', not '$want'. Left unchanged." >&2
    else
        echo "    PUBLIC_BASE_URL=$want (already set)"
    fi

    # No AWS metadata on Azure: pass the IP, and the Docker subnet explicitly (only used
    # until .env has SOLYA_SUBNET; setup.sh then reads it from there).
    echo "==> Running deploy/ec2/setup.sh"
    SOLYA_SUBNET="$subnet" "$dir/deploy/ec2/setup.sh" --branch "$branch" --ip "$ip" < /dev/null
}
main "$@"
REMOTE
)

remote_install() {
    log "Installing Solya on the VM (deploy/ec2/setup.sh)"
    local args=("$FQDN" "$PUBLIC_IP" "$SOLYA_SUBNET_VALUE" "$REPO_URL" "$BRANCH")
    if (( DRY_RUN )); then
        info "[dry-run] $(printf '%q ' ssh "${SSH_ID_ARGS[@]}" "${SSH_OPTS[@]}" "$ADMIN@$FQDN" bash -s -- "${args[@]}")<<'REMOTE'"
        # shellcheck disable=SC2001  # indent every line
        sed 's/^/      | /' <<< "$REMOTE_SCRIPT"
        info "REMOTE"
        return
    fi
    # The arguments are quoted here, on purpose, for the remote shell.
    # shellcheck disable=SC2029
    ssh "${SSH_ID_ARGS[@]}" "${SSH_OPTS[@]}" "$ADMIN@$FQDN" \
        "bash -s -- $(printf '%q ' "${args[@]}")" <<< "$REMOTE_SCRIPT"
}

summary() {
    local url="https://$FQDN" ssh_cmd
    ssh_cmd="ssh ${SSH_ID_ARGS[*]:+${SSH_ID_ARGS[*]} }$ADMIN@$FQDN"
    log "Done"
    cat <<EOF
    Host (FQDN):   $FQDN   (public IP $PUBLIC_IP)
    App URL:       $url
    Health check:  $url/healthz
    Redirect URI:  $url/oauth/callback   (register exactly this in the SolarEdge app)
    SSH:           $ssh_cmd

    Now fill /opt/solya/.env on the VM yourself (see deploy/ec2/HANDOFF-PROMPT.md, Part 3).
    Never paste these values into a chat or a command line.

      Required:
        TELEGRAM_BOT_TOKEN
        TELEGRAM_BOT_USERNAME        (without the @)
        SOLAREDGE_CLIENT_ID
        SOLAREDGE_CLIENT_SECRET      (copy the existing secret; do not regenerate it)
      Recommended:
        ADMIN_PASSWORD               (long passphrase for /admin; 404 while empty)
        ADMIN_CHAT_IDS               (JSON list, e.g. [123456789])
        BUDGET_MONTHLY_CREDITS=20000 (Starter plan, until 17 Dec 2026; then 2000)
        BUDGET_BILLING_DAY=17
      Leave PUBLIC_BASE_URL, TOKEN_ENCRYPTION_KEY and the SOLYA_*/COMPOSE_* keys as written.

    Edit, then apply (re-runs setup.sh; or simply re-run this script):
      ssh -t ${SSH_ID_ARGS[*]:+${SSH_ID_ARGS[*]} }$ADMIN@$FQDN nano /opt/solya/.env
      $ssh_cmd /opt/solya/deploy/ec2/setup.sh --ip $PUBLIC_IP

    Then create the invite:
      $ssh_cmd /opt/solya/deploy/ec2/compose exec -T solya solya invite create
EOF
    (( ! DRY_RUN )) || { printf '\n'; info "Dry-run: nothing was changed."; }
}

# ----------------------------------------------------------------------------- destroy

destroy() {
    log "Destroy resource group $RG"
    check_account
    if [[ "$(azq group exists -n "$RG" || echo false)" != true ]]; then
        info "resource group $RG does not exist; nothing to delete"
        return
    fi
    info "resources in $RG:"
    azq resource list -g "$RG" --query '[].[type, name]' -o tsv | sed 's/^/      /' || true
    if (( DRY_RUN )); then
        run az group delete -n "$RG" --yes
        info "[dry-run] then remove SOLYA_AZ_DNS_LABEL from $STATE_FILE"
        return
    fi
    local typed
    read -r -p "This deletes everything above, including the database on the VM. Type the resource group name to confirm: " typed || typed=""
    [[ "$typed" == "$RG" ]] || die "'$typed' does not match '$RG'; nothing deleted"
    run az group delete -n "$RG" --yes
    if [[ -f "$STATE_FILE" ]] && grep -qE '^SOLYA_AZ_DNS_LABEL=' "$STATE_FILE"; then
        local tmp
        tmp="$(mktemp "$STATE_FILE.XXXXXX")"
        grep -vE '^SOLYA_AZ_DNS_LABEL=' "$STATE_FILE" > "$tmp" || true
        mv -f "$tmp" "$STATE_FILE"
        info "removed the DNS label from $STATE_FILE"
    fi
    info "deleted $RG"
}

# ----------------------------------------------------------------------------- main

main() {
    parse_args "$@"
    (( DRY_RUN )) && log "DRY RUN: printing planned actions only"
    check_tools
    set_ssh_identity
    if (( DESTROY )); then destroy; return; fi

    check_account
    check_sku
    resolve_ssh_source
    resolve_dns_label

    ensure_group
    ensure_public_ip
    ensure_nsg
    ensure_vm
    tag_all
    read_address

    wait_for_ssh
    remote_install
    summary
}

main "$@"
