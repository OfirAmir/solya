#!/usr/bin/env bash
# Bring up Solya on an EC2 instance for a trial: Docker + compose, the repo in /opt/solya,
# a .env with PUBLIC_BASE_URL on <a-b-c-d>.sslip.io and a fresh TOKEN_ENCRYPTION_KEY, the
# image pulled from GHCR (or built from source with --build) and checked, and the stack
# (Solya + Caddy with a Let's Encrypt certificate) started. Safe to re-run: it never overwrites a value already set in .env.
#
# Supported: Amazon Linux 2023, Ubuntu 22.04 and 24.04. Run it as your normal user (not
# root, not through sudo); it calls sudo itself for the privileged steps.
#
#   deploy/ec2/setup.sh [--branch NAME] [--ip A.B.C.D] [--repo URL] [--build] [--dry-run] [--allow-root]
#
# It is written for a host that may run other things: it stops rather than take ports
# 80/443 from another service, reuse another compose project, overlap a network in use,
# replace an existing Docker install, or install anything host-wide next to an existing
# Docker. See deploy/ec2/README.md for the full walkthrough.

set -euo pipefail

DEFAULT_BRANCH="main"
DEFAULT_IMAGE="ghcr.io/ofiramir/solya:latest"   # published by .github/workflows/cd.yml
DEFAULT_REPO="https://github.com/OfirAmir/solya.git"
INSTALL_DIR="${SOLYA_DIR:-/opt/solya}"
PROJECT="solya-ec2"
OVERRIDE_REL="deploy/ec2/docker-compose.caddy.yml"
COMPOSE_FILE_VALUE="docker-compose.yml:${OVERRIDE_REL}"
EXAMPLE_BASE_URL="https://solya.example.com"
DEFAULT_SUBNET="192.168.250.0/24"   # outside 10/8 and 172.16/12, where VPCs usually live
COMPOSE_MIN="2.24.0"   # `ports: !reset []` in the override needs 2.24+
COMPOSE_VERSION="${SOLYA_COMPOSE_VERSION:-v2.32.4}"   # used only when the plugin is missing or too old
BUILDX_VERSION="${SOLYA_BUILDX_VERSION:-v0.20.1}"     # used only with --build, when buildx is missing
DOCKER_CFG="$INSTALL_DIR/.docker"                     # private DOCKER_CONFIG, set only in dc/dc_capture
PLUGIN_DIR="$DOCKER_CFG/cli-plugins"
IMDS="http://169.254.169.254/latest"

# Keys the service cannot start without, and that only a person can fill.
REQUIRED_SECRETS=(SOLAREDGE_CLIENT_ID SOLAREDGE_CLIENT_SECRET TELEGRAM_BOT_TOKEN TELEGRAM_BOT_USERNAME)

BRANCH="$DEFAULT_BRANCH"
REPO_URL="$DEFAULT_REPO"
IP_ARG=""
BUILD=0
DRY_RUN=0
ALLOW_ROOT=0

ME="$(id -un)"
MY_GROUP="$(id -gn)"
SUDO=""
FSUDO=""   # prefix for writes inside INSTALL_DIR when it is not writable by this user
OS_ID=""
OS_VERSION=""
OS_CODENAME=""
HAVE_DOCKER=0
INSTALLED_DOCKER=0
IMDS_TOKEN=""
SUBNET_EXPLICIT=0   # SOLYA_SUBNET came from .env or the environment, not the default

# ----------------------------------------------------------------------------- output

log()  { printf '\n==> %s\n' "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '\n!!  %s\n' "$*" >&2; }
die()  { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

# A check that must stop a real run. Under --dry-run it only warns, so the rest of the
# plan is still shown.
stop_check() {
    if (( DRY_RUN )); then
        warn "$* (dry-run continues; a real run stops here)"
    else
        die "$*"
    fi
}

usage() {
    cat <<EOF
Usage: $0 [--branch NAME] [--ip A.B.C.D] [--repo URL] [--build] [--dry-run] [--allow-root]

  --branch NAME   git branch to deploy (default: $DEFAULT_BRANCH)
  --ip A.B.C.D    public IPv4 to build the sslip.io host from. Wins over the
                  EC2 metadata lookup; needed when IMDSv2 is not reachable
                  (and on every run outside EC2, for example on Azure).
  --repo URL      git URL to clone (default: $DEFAULT_REPO)
  --build         build the image from the checked-out source and tag it as SOLYA_IMAGE,
                  instead of pulling it from GHCR (GHCR unreachable, or testing local changes)
  --dry-run       print the planned actions; change nothing
  --allow-root    allow running as root (normally refused: run as your own user)
  -h, --help      this help
EOF
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

# Same, with root privileges.
run_root() {
    if [[ -n "$SUDO" ]]; then run "$SUDO" "$@"; else run "$@"; fi
}

# A read-only command that needs root. Runs in dry-run too, but never prompts there.
ro_root() {
    if [[ -z "$SUDO" ]]; then "$@"
    elif (( DRY_RUN )); then sudo -n "$@"
    else sudo "$@"
    fi
}

# docker compose for this kit, as root: both files, the project name and the private
# plugin dir. DOCKER_CONFIG is set here only, never for the rest of the host.
dc() {
    run_root env DOCKER_CONFIG="$DOCKER_CFG" docker compose \
        --project-name "$PROJECT" --project-directory "$INSTALL_DIR" \
        -f "$INSTALL_DIR/docker-compose.yml" -f "$INSTALL_DIR/$OVERRIDE_REL" "$@"
}

# Same, for capturing output; not echoed. Callers never use it in dry-run.
dc_capture() {
    ${SUDO:+"$SUDO"} env DOCKER_CONFIG="$DOCKER_CFG" docker compose \
        --project-name "$PROJECT" --project-directory "$INSTALL_DIR" \
        -f "$INSTALL_DIR/docker-compose.yml" -f "$INSTALL_DIR/$OVERRIDE_REL" "$@"
}

# The compose version root sees with the private plugin dir, or nothing.
compose_version() {
    ro_root env DOCKER_CONFIG="$DOCKER_CFG" docker compose version --short 2>/dev/null | sed 's/^v//' || true
}

# ----------------------------------------------------------------------------- args

parse_args() {
    while (( $# )); do
        case "$1" in
            --branch)   [[ $# -ge 2 ]] || die "--branch needs a value"; BRANCH="$2"; shift 2 ;;
            --branch=*) BRANCH="${1#*=}"; shift ;;
            --ip)       [[ $# -ge 2 ]] || die "--ip needs a value"; IP_ARG="$2"; shift 2 ;;
            --ip=*)     IP_ARG="${1#*=}"; shift ;;
            --repo)     [[ $# -ge 2 ]] || die "--repo needs a value"; REPO_URL="$2"; shift 2 ;;
            --repo=*)   REPO_URL="${1#*=}"; shift ;;
            --build)    BUILD=1; shift ;;
            --dry-run)  DRY_RUN=1; shift ;;
            --allow-root) ALLOW_ROOT=1; shift ;;
            -h|--help)  usage; exit 0 ;;
            *) usage >&2; die "unknown argument: $1" ;;
        esac
    done
    [[ -n "$BRANCH" ]] || die "--branch is empty"
}

# ----------------------------------------------------------------------------- preflight

detect_os() {
    [[ -r /etc/os-release ]] || die "/etc/os-release not found; cannot tell the OS"
    # shellcheck disable=SC1091
    OS_ID="$(. /etc/os-release && printf '%s' "${ID:-}")"
    # shellcheck disable=SC1091
    OS_VERSION="$(. /etc/os-release && printf '%s' "${VERSION_ID:-}")"
    # shellcheck disable=SC1091
    OS_CODENAME="$(. /etc/os-release && printf '%s' "${VERSION_CODENAME:-${UBUNTU_CODENAME:-}}")"

    case "$OS_ID:$OS_VERSION" in
        amzn:2023|ubuntu:22.04|ubuntu:24.04) info "OS: $OS_ID $OS_VERSION" ;;
        *) stop_check "unsupported OS '$OS_ID $OS_VERSION'. Supported: Amazon Linux 2023, Ubuntu 22.04, Ubuntu 24.04." ;;
    esac
}

setup_sudo() {
    if [[ "$(id -u)" -eq 0 ]]; then
        (( ALLOW_ROOT )) || die "do not run this as root or through sudo. Run it as your normal user (it calls sudo itself), or pass --allow-root if you really mean root."
        SUDO=""
        info "running as root (--allow-root)"
    else
        command -v sudo >/dev/null 2>&1 || die "sudo is not installed"
        SUDO="sudo"
        if (( ! DRY_RUN )); then
            # Passwordless sudo first: on images where the user has NOPASSWD but is
            # also in a password-required group (e.g. Azure's azureuser), `sudo -v`
            # still asks for a password and fails without a terminal.
            sudo -n true 2>/dev/null || sudo -v || die "this user cannot sudo"
        elif ! sudo -n true 2>/dev/null; then
            info "(sudo needs a password; dry-run skips the checks that need root)"
        fi
    fi
    command -v docker >/dev/null 2>&1 && HAVE_DOCKER=1 || HAVE_DOCKER=0
}

check_resources() {
    local mem_kb avail_kb
    mem_kb="$(awk '/^MemTotal:/ {print $2}' /proc/meminfo 2>/dev/null || echo 0)"
    if [[ -n "$mem_kb" ]] && (( mem_kb > 0 && mem_kb < 450000 )); then
        warn "only $(( mem_kb / 1024 )) MB RAM. About 512 MB is the minimum to run Solya and Caddy."
    elif (( BUILD )) && [[ -n "$mem_kb" ]] && (( mem_kb > 0 && mem_kb < 900000 )); then
        warn "only $(( mem_kb / 1024 )) MB RAM. A local build (--build) wants about 1 GB; it may fail. Pulling the published image needs far less."
    fi
    avail_kb="$(df -Pk "$(dirname "$INSTALL_DIR")" 2>/dev/null | awk 'NR==2 {print $4}' || echo 0)"
    if [[ -n "$avail_kb" ]] && (( avail_kb > 0 && avail_kb < 4000000 )); then
        warn "only $(( avail_kb / 1024 )) MB free disk under $(dirname "$INSTALL_DIR"). Plan for about 10 GB."
    fi
}

# Containers of a compose project, as "name|working_dir|service" lines.
project_containers() {
    ro_root docker ps -a --filter "label=com.docker.compose.project=$PROJECT" \
        --format '{{.Names}}|{{.Label "com.docker.compose.project.working_dir"}}|{{.Label "com.docker.compose.service"}}' 2>/dev/null
}

check_project() {
    (( HAVE_DOCKER )) || return 0
    local lines line name wd clash=0
    if ! lines="$(project_containers)"; then
        info "(could not list containers: no passwordless sudo, or the Docker daemon is stopped; project check skipped)"
        return 0
    fi
    while IFS= read -r line; do
        [[ -n "$line" ]] || continue
        name="${line%%|*}"; wd="${line#*|}"; wd="${wd%%|*}"
        if [[ "$wd" != "$INSTALL_DIR" ]]; then
            stop_check "container '$name' belongs to compose project '$PROJECT' but was started from '$wd', not $INSTALL_DIR. Another copy of this kit runs on this host; do not touch it. Talk to whoever runs it."
            clash=1
        fi
    done <<< "$lines"
    (( clash )) || info "compose project '$PROJECT': free or ours"
}

# Ports 80/443 must be free, or held only by this checkout's Caddy.
check_ports() {
    local bad=() names line c proj svc wd unverified=0
    if command -v ss >/dev/null 2>&1; then
        local listeners
        listeners="$(ss -ltnH '( sport = :80 or sport = :443 )' 2>/dev/null || true)"
        if [[ -n "$listeners" ]]; then
            if ! names="$(ro_root ss -ltnpH '( sport = :80 or sport = :443 )' 2>/dev/null)"; then
                stop_check "something listens on port 80 or 443 and its owner cannot be checked without sudo"
                names=""; unverified=1
            fi
            while IFS= read -r line; do
                [[ -n "$line" ]] || continue
                if [[ "$line" =~ \(\(\"([^\"]+)\" ]]; then
                    [[ "${BASH_REMATCH[1]}" == docker-proxy ]] || bad+=("$(awk '{print $4}' <<< "$line") held by ${BASH_REMATCH[1]}")
                else
                    bad+=("$(awk '{print $4}' <<< "$line") held by an unknown process")
                fi
            done <<< "$names"
        fi
    else
        stop_check "the 'ss' tool (package iproute2) is missing, so ports 80/443 cannot be checked. Install iproute2 and re-run."
        unverified=1
    fi
    if (( HAVE_DOCKER )); then
        # Host ports come from {{.Ports}} ("0.0.0.0:80->80/tcp, ..."). `--filter publish=`
        # would match container ports instead.
        local pubs ports host_port_re='(^|, )[^,]*:(80|443)->'
        if pubs="$(ro_root docker ps \
                --format '{{.Names}}|{{.Label "com.docker.compose.project"}}|{{.Label "com.docker.compose.service"}}|{{.Label "com.docker.compose.project.working_dir"}}|{{.Ports}}' 2>/dev/null)"; then
            while IFS='|' read -r c proj svc wd ports; do
                [[ -n "$c" && "$ports" =~ $host_port_re ]] || continue
                if [[ "$proj" != "$PROJECT" || "$svc" != caddy || "$wd" != "$INSTALL_DIR" ]]; then
                    bad+=("container '$c' publishes host port 80 or 443 ($ports)")
                fi
            done <<< "$pubs"
        elif command -v systemctl >/dev/null 2>&1 && ! systemctl is-active --quiet docker 2>/dev/null \
                && ! systemctl is-active --quiet docker.socket 2>/dev/null; then
            info "Docker daemon is not running, so no container holds ports 80/443"
        else
            stop_check "could not list containers (docker ps failed), so ports 80/443 cannot be checked against other containers"
            unverified=1
        fi
    fi
    if (( ${#bad[@]} )); then
        printf '        %s\n' "${bad[@]}" >&2
        stop_check "ports 80/443 are in use by another service (above). This kit needs both for Caddy and the certificate, and will not take them from another service. Talk to the host owner, or put Solya behind the existing proxy instead (main README step 8, \"any other proxy\")."
    elif (( ! unverified )); then
        info "ports 80/443: free or held by this kit's Caddy"
    fi
}

# ----------------------------------------------------------------------------- public IP and networks

ip2int() {
    local IFS=.
    # shellcheck disable=SC2086
    set -- $1
    printf '%s' $(( (10#$1 << 24) | (10#$2 << 16) | (10#$3 << 8) | 10#$4 ))
}

int2ip() {
    local n="$1"
    printf '%s.%s.%s.%s' $(( (n >> 24) & 255 )) $(( (n >> 16) & 255 )) $(( (n >> 8) & 255 )) $(( n & 255 ))
}

valid_ipv4() {
    local ip="$1" o
    [[ "$ip" =~ ^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})$ ]] || return 1
    for o in "${BASH_REMATCH[@]:1}"; do (( 10#$o <= 255 )) || return 1; done
}

valid_cidr() {
    [[ "$1" == */* ]] || return 1
    local p="${1#*/}"
    valid_ipv4 "${1%/*}" && [[ "$p" =~ ^[0-9]{1,2}$ ]] && (( p <= 32 ))
}

mask_of() {  # prefix length -> netmask int
    local p="$1"
    (( p == 0 )) && { printf 0; return; }
    printf '%s' $(( (0xFFFFFFFF << (32 - p)) & 0xFFFFFFFF ))
}

cidrs_overlap() {  # two CIDRs share at least one address
    local pa="${1#*/}" pb="${2#*/}" p m
    p=$(( pa < pb ? pa : pb ))
    m="$(mask_of "$p")"
    (( ( $(ip2int "${1%/*}") & m ) == ( $(ip2int "${2%/*}") & m ) ))
}

ip_in_cidr() {
    local m
    m="$(mask_of "${2#*/}")"
    (( ( $(ip2int "$1") & m ) == ( $(ip2int "${2%/*}") & m ) ))
}

is_private_ipv4() {
    [[ "$1" =~ ^10\. || "$1" =~ ^192\.168\. || "$1" =~ ^172\.(1[6-9]|2[0-9]|3[01])\. || "$1" =~ ^127\. || "$1" =~ ^169\.254\. ]]
}

imds_get() {  # path under /latest/ ; empty on failure
    if [[ -z "$IMDS_TOKEN" ]]; then
        IMDS_TOKEN="$(curl -fsS --noproxy '*' --max-time 3 -X PUT \
            -H 'X-aws-ec2-metadata-token-ttl-seconds: 300' "$IMDS/api/token" 2>/dev/null)" || IMDS_TOKEN=""
        [[ -n "$IMDS_TOKEN" ]] || { IMDS_TOKEN="-"; return 1; }
    fi
    [[ "$IMDS_TOKEN" != "-" ]] || return 1
    curl -fsS --noproxy '*' --max-time 3 -H "X-aws-ec2-metadata-token: $IMDS_TOKEN" "$IMDS/$1" 2>/dev/null
}

PUBLIC_IP=""
SSLIP_HOST=""
BASE_URL=""
SITE_URL=""   # the PUBLIC_BASE_URL in effect (.env's value when set), for the summary and health check

resolve_ip() {
    if [[ -n "$IP_ARG" ]]; then
        PUBLIC_IP="$IP_ARG"
        info "public IP from --ip: $PUBLIC_IP"
    elif PUBLIC_IP="$(imds_get meta-data/public-ipv4)" && [[ -n "$PUBLIC_IP" ]]; then
        info "public IP from EC2 metadata (IMDSv2): $PUBLIC_IP"
    else
        die "could not read the public IPv4 from EC2 metadata (IMDSv2). Does the instance have a public IP? Pass it with --ip A.B.C.D (the instance's public IPv4 or Elastic IP)."
    fi
    valid_ipv4 "$PUBLIC_IP" || die "not an IPv4 address: '$PUBLIC_IP'"
    if is_private_ipv4 "$PUBLIC_IP"; then
        warn "$PUBLIC_IP is a private address. Let's Encrypt cannot reach it; use the instance's public IPv4."
    fi
    SSLIP_HOST="${PUBLIC_IP//./-}.sslip.io"
    BASE_URL="https://$SSLIP_HOST"
    info "host: $SSLIP_HOST"
}

NET_SUBNET=""
NET_RANGE=""
NET_CADDY=""

# Subnet, dynamic range and Caddy's address: from .env where set, the rest derived from
# the subnet (range = upper half, Caddy = network + 10). Before .env exists, the subnet
# can be chosen with SOLYA_SUBNET=... in the environment.
load_net_settings() {
    local f="$INSTALL_DIR/.env"
    NET_SUBNET="$(env_get "$f" SOLYA_SUBNET)"; NET_SUBNET="${NET_SUBNET:-${SOLYA_SUBNET:-}}"
    if [[ -n "$NET_SUBNET" ]]; then SUBNET_EXPLICIT=1; else NET_SUBNET="$DEFAULT_SUBNET"; fi
    valid_cidr "$NET_SUBNET" || die "SOLYA_SUBNET '$NET_SUBNET' is not an IPv4 CIDR like 192.168.250.0/24"
    local p="${NET_SUBNET#*/}" net
    (( p >= 16 && p <= 27 )) || die "SOLYA_SUBNET '$NET_SUBNET': use a prefix between /16 and /27"
    net=$(( $(ip2int "${NET_SUBNET%/*}") & $(mask_of "$p") ))
    [[ "$(int2ip "$net")/$p" == "$NET_SUBNET" ]] || die "SOLYA_SUBNET '$NET_SUBNET' is not a network address (did you mean $(int2ip "$net")/$p?)"

    NET_RANGE="$(env_get "$f" SOLYA_IP_RANGE)"
    NET_RANGE="${NET_RANGE:-$(int2ip $(( net + (1 << (32 - p - 1)) )))/$(( p + 1 ))}"
    NET_CADDY="$(env_get "$f" SOLYA_CADDY_IP)"
    NET_CADDY="${NET_CADDY:-$(int2ip $(( net + 10 )))}"

    valid_cidr "$NET_RANGE" || die "SOLYA_IP_RANGE '$NET_RANGE' is not an IPv4 CIDR"
    valid_ipv4 "$NET_CADDY" || die "SOLYA_CADDY_IP '$NET_CADDY' is not an IPv4 address"
    if (( ${NET_RANGE#*/} <= p )) || ! ip_in_cidr "${NET_RANGE%/*}" "$NET_SUBNET"; then
        die "SOLYA_IP_RANGE '$NET_RANGE' must be a smaller range inside SOLYA_SUBNET '$NET_SUBNET'"
    fi
    ip_in_cidr "$NET_CADDY" "$NET_SUBNET" || die "SOLYA_CADDY_IP '$NET_CADDY' is outside SOLYA_SUBNET '$NET_SUBNET'"
    ! ip_in_cidr "$NET_CADDY" "$NET_RANGE" || die "SOLYA_CADDY_IP '$NET_CADDY' is inside SOLYA_IP_RANGE '$NET_RANGE'; pick one outside it"
    local c
    c="$(ip2int "$NET_CADDY")"
    # Docker puts the gateway on the first address of ip_range, so .1 is free, but keep
    # Caddy off the network address and .1 anyway.
    (( c > net + 1 )) || die "SOLYA_CADDY_IP '$NET_CADDY' is the network address or .1; pick another"
    info "network: subnet $NET_SUBNET, dynamic range $NET_RANGE, Caddy $NET_CADDY"
}

# The pinned subnet must overlap no VPC range, no route and no other Docker network.
check_subnet() {
    local clashes=() cidr dev line own_dev="" own_net="" unverified=0

    # This kit's own network (on a re-run) is not a clash, as long as it is the same subnet.
    if (( HAVE_DOCKER )); then
        local id
        id="$(ro_root docker network inspect --format '{{.Id}}' "${PROJECT}_default" 2>/dev/null || true)"
        if [[ -n "$id" ]]; then
            own_dev="br-${id:0:12}"
            own_net="$(ro_root docker network inspect --format '{{range .IPAM.Config}}{{.Subnet}} {{end}}' "${PROJECT}_default" 2>/dev/null || true)"
            own_net="${own_net%% *}"
            if [[ -n "$own_net" && "$own_net" != "$NET_SUBNET" ]]; then
                stop_check "network ${PROJECT}_default exists with subnet $own_net, but SOLYA_SUBNET is $NET_SUBNET. Run '$INSTALL_DIR/deploy/ec2/compose down' first, then re-run."
            fi
        fi
    fi

    # VPC ranges from IMDS.
    local mac vpc
    if mac="$(imds_get meta-data/mac)" && [[ -n "$mac" ]] \
        && vpc="$(imds_get "meta-data/network/interfaces/macs/$mac/vpc-ipv4-cidr-blocks")"; then
        for cidr in $vpc; do
            valid_cidr "$cidr" || continue
            cidrs_overlap "$cidr" "$NET_SUBNET" && clashes+=("VPC range $cidr")
        done
        info "VPC ranges: $(tr '\n' ' ' <<< "$vpc")"
    elif (( SUBNET_EXPLICIT )); then
        warn "could not read the VPC ranges from EC2 metadata; SOLYA_SUBNET=$NET_SUBNET was set explicitly, so the run continues without the VPC part of the overlap check. Outside EC2 (for example on Azure) that is expected: make sure the range is not in the VNet's address space."
    else
        stop_check "could not read the VPC ranges from EC2 metadata (IMDS), so the default subnet $NET_SUBNET cannot be checked against them. Ask the host owner for a free range (VPC, peered VPCs, VPN, Transit Gateway) and pass it explicitly, for example: SOLYA_SUBNET=192.168.251.0/24 $0"
        unverified=1
    fi

    # Routes on the host.
    if command -v ip >/dev/null 2>&1; then
        local kind
        while IFS= read -r line; do
            # Route types (unreachable, blackhole, ...) come before the destination.
            read -r kind cidr _ <<< "$line"
            case "$kind" in
                unicast|local|broadcast|multicast|throw|unreachable|prohibit|blackhole|nat|anycast) ;;
                *) cidr="$kind" ;;
            esac
            [[ "$cidr" == default ]] && continue
            # A single-host route has no prefix: count it as /32.
            [[ "$cidr" == */* ]] || cidr="$cidr/32"
            dev=""
            [[ "$line" =~ \ dev\ ([^ ]+) ]] && dev="${BASH_REMATCH[1]}"
            [[ -n "$own_dev" && "$dev" == "$own_dev" ]] && continue
            valid_cidr "$cidr" && cidrs_overlap "$cidr" "$NET_SUBNET" && clashes+=("route $cidr dev ${dev:-?}")
        done < <(ip -4 route show 2>/dev/null)
    else
        stop_check "the 'ip' tool (package iproute2) is missing, so the host's routes cannot be checked against SOLYA_SUBNET. Install iproute2 and re-run."
        unverified=1
    fi

    # Every Docker network.
    if (( HAVE_DOCKER )); then
        local ids nets name subs
        if ids="$(ro_root docker network ls -q 2>/dev/null)"; then
            if [[ -n "$ids" ]]; then
                # shellcheck disable=SC2086  # one id per word
                nets="$(ro_root docker network inspect --format '{{.Name}}|{{range .IPAM.Config}}{{.Subnet}} {{end}}' $ids 2>/dev/null || true)"
                while IFS='|' read -r name subs; do
                    [[ -n "$name" && "$name" != "${PROJECT}_default" ]] || continue
                    for cidr in $subs; do
                        valid_cidr "$cidr" || continue   # skips IPv6
                        cidrs_overlap "$cidr" "$NET_SUBNET" && clashes+=("Docker network $name ($cidr)")
                    done
                done <<< "$nets"
            fi
        else
            info "(could not list Docker networks without sudo; that part of the overlap check is skipped)"
        fi
    fi

    if (( ${#clashes[@]} )); then
        printf '        %s\n' "${clashes[@]}" >&2
        stop_check "SOLYA_SUBNET $NET_SUBNET overlaps the networks above. Pick a free private range, for example: SOLYA_SUBNET=192.168.251.0/24 $0. If $ENV_FILE already sets SOLYA_SUBNET, SOLYA_IP_RANGE and SOLYA_CADDY_IP, change SOLYA_SUBNET there and delete the other two lines so they are derived again. See Troubleshooting in deploy/ec2/README.md."
    elif (( ! unverified )); then
        info "subnet $NET_SUBNET: no overlap with VPC ranges, routes or Docker networks"
    fi
}

# ----------------------------------------------------------------------------- packages

version_ge() {  # version_ge A B: A >= B
    [[ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -n1)" == "$2" ]]
}

install_git() {
    if command -v git >/dev/null 2>&1; then info "git: present"; return; fi
    log "Installing git"
    case "$OS_ID" in
        amzn)   run_root dnf install -y git ;;
        ubuntu) run_root apt-get update; run_root apt-get install -y --no-remove git ;;
        *)      info "(no package plan for this OS)" ;;
    esac
}

# Before a fresh Docker install: stop if any other container runtime is already there.
check_no_other_runtime() {
    local found=()
    case "$OS_ID" in
        ubuntu)
            local pkgs
            pkgs="$(dpkg -l containerd runc docker.io podman-docker 2>/dev/null | awk '$1 ~ /^(ii|hi|iU|iF|hU|hF)$/ {print $2}' || true)"
            [[ -z "$pkgs" ]] || read -r -a found <<< "$(tr '\n' ' ' <<< "$pkgs")"
            if command -v snap >/dev/null 2>&1 && snap list docker >/dev/null 2>&1; then found+=("snap:docker"); fi
            ;;
        amzn)
            local p
            for p in podman-docker containerd runc moby-engine; do
                rpm -q "$p" >/dev/null 2>&1 && found+=("$p")
            done
            ;;
    esac
    if (( ${#found[@]} )); then
        stop_check "docker is not on PATH, but these container packages are installed: ${found[*]}. Installing Docker now could replace or break them. Talk to the host owner first."
    fi
}

install_docker() {
    if (( HAVE_DOCKER )); then
        local ver
        ver="$(docker --version 2>/dev/null || echo unknown)"
        info "docker: present ($ver); it is left as it is"
        if [[ "$ver" == *[Pp]odman* ]]; then
            stop_check "'docker' on this host is the podman-docker shim, not Docker Engine. This kit needs Docker Engine and will not replace podman. Talk to the host owner."
            return
        fi
        if ro_root docker info >/dev/null 2>&1; then
            info "docker daemon: running"
        elif command -v systemctl >/dev/null 2>&1 && ! systemctl is-active --quiet docker 2>/dev/null; then
            if ! systemctl cat docker.service >/dev/null 2>&1; then
                stop_check "docker is on PATH but not running, and there is no docker.service systemd unit to start. This usually means Docker from a snap, or a podman shim. This kit will not change that setup. Talk to the host owner."
                return
            fi
            if (( DRY_RUN )); then
                run_root systemctl start docker
            else
                run_root systemctl start docker || die "systemctl start docker failed. Check: sudo systemctl status docker"
            fi
        fi
        return
    fi
    check_no_other_runtime
    log "Installing Docker"
    case "$OS_ID" in
        amzn)
            run_root dnf install -y docker
            ;;
        ubuntu)
            local arch
            arch="$(dpkg --print-architecture 2>/dev/null || echo amd64)"
            run_root apt-get update
            run_root apt-get install -y --no-remove ca-certificates curl
            run_root install -m 0755 -d /etc/apt/keyrings
            run_root curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
            run_root chmod a+r /etc/apt/keyrings/docker.asc
            local line="deb [arch=$arch signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $OS_CODENAME stable"
            if (( DRY_RUN )); then
                info "[dry-run] write '$line' to /etc/apt/sources.list.d/docker.list"
            else
                printf '%s\n' "$line" | ${SUDO:+"$SUDO"} tee /etc/apt/sources.list.d/docker.list >/dev/null
            fi
            run_root apt-get update
            run_root apt-get install -y --no-remove docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
            ;;
        *) info "(no package plan for this OS)" ;;
    esac
    INSTALLED_DOCKER=1
    # Enabled at boot only because this script installed it.
    command -v systemctl >/dev/null 2>&1 && run_root systemctl enable --now docker
    (( DRY_RUN )) || HAVE_DOCKER=1
}

docker_arch() {  # $1 = compose|buildx
    case "$(uname -m)" in
        x86_64)        [[ "$1" == compose ]] && echo x86_64  || echo amd64 ;;
        aarch64|arm64) [[ "$1" == compose ]] && echo aarch64 || echo arm64 ;;
        *) die "unsupported CPU architecture $(uname -m)" ;;
    esac
}

# Download a CLI plugin into the kit's private dir ($INSTALL_DIR/.docker/cli-plugins),
# never host-wide. Fails closed without a published checksum.
install_plugin_binary() {  # $1 = file name, $2 = url, $3 = expected sha256
    local name="$1" url="$2" sum="$3"
    if (( DRY_RUN )); then
        info "[dry-run] download $url, verify its sha256, install as $PLUGIN_DIR/$name (private to this kit)"
        return
    fi
    [[ "$sum" =~ ^[0-9a-f]{64}$ ]] || die "no published sha256 found for $url; refusing to install it unverified"
    run ${FSUDO:+"$FSUDO"} install -d -m 0755 "$PLUGIN_DIR"
    local tmp
    tmp="$(${FSUDO:+"$FSUDO"} mktemp "$PLUGIN_DIR/.download.XXXXXX")"
    if ! ${FSUDO:+"$FSUDO"} curl -fsSL "$url" -o "$tmp"; then
        ${FSUDO:+"$FSUDO"} rm -f "$tmp"; die "download failed: $url"
    fi
    if ! printf '%s  %s\n' "$sum" "$tmp" | sha256sum -c --quiet -; then
        ${FSUDO:+"$FSUDO"} rm -f "$tmp"; die "checksum mismatch for $url"
    fi
    run ${FSUDO:+"$FSUDO"} chmod 0755 "$tmp"
    run ${FSUDO:+"$FSUDO"} mv -f "$tmp" "$PLUGIN_DIR/$name"
}

install_compose_plugin() {
    local have
    have="$(compose_version)"
    if [[ -n "$have" ]] && version_ge "$have" "$COMPOSE_MIN"; then
        info "docker compose (as root): $have (>= $COMPOSE_MIN)"
        return
    fi
    if [[ -n "$have" ]]; then
        log "docker compose $have is older than $COMPOSE_MIN; installing $COMPOSE_VERSION for this kit only"
    else
        log "Installing the docker compose plugin $COMPOSE_VERSION for this kit only"
    fi
    local a base sum=""
    a="$(docker_arch compose)"
    base="https://github.com/docker/compose/releases/download/$COMPOSE_VERSION"
    if (( ! DRY_RUN )); then
        sum="$(curl -fsSL "$base/docker-compose-linux-$a.sha256" | awk '{print $1}')" || sum=""
    fi
    install_plugin_binary docker-compose "$base/docker-compose-linux-$a" "$sum"
    if (( ! DRY_RUN )); then
        have="$(compose_version)"
        version_ge "${have:-0}" "$COMPOSE_MIN" || die "docker compose is still '${have:-missing}' after installing it into $PLUGIN_DIR"
        info "docker compose (as root): $have"
    fi
}

install_buildx_plugin() {
    if (( ! BUILD )); then
        info "docker buildx: not needed (the image is pulled; only --build needs it)"
        return
    fi
    if ro_root env DOCKER_CONFIG="$DOCKER_CFG" docker buildx version >/dev/null 2>&1; then
        info "docker buildx: present"
        return
    fi
    log "Installing the docker buildx plugin $BUILDX_VERSION for this kit only (--build uses it)"
    local a file base sum=""
    a="$(docker_arch buildx)"
    file="buildx-$BUILDX_VERSION.linux-$a"
    base="https://github.com/docker/buildx/releases/download/$BUILDX_VERSION"
    if (( ! DRY_RUN )); then
        sum="$(curl -fsSL "$base/checksums.txt" | awk -v f="$file" '$2==f || $2=="*"f {print $1}')" || sum=""
    fi
    install_plugin_binary docker-buildx "$base/$file" "$sum"
}

# ----------------------------------------------------------------------------- repo

fetch_repo() {
    log "Repository $REPO_URL ($BRANCH) in $INSTALL_DIR"
    if [[ -d "$INSTALL_DIR/.git" ]]; then
        local owner git=(git -C "$INSTALL_DIR")
        owner="$(stat -c %U "$INSTALL_DIR" 2>/dev/null || echo "?")"
        if [[ "$owner" != "$ME" ]]; then
            warn "$INSTALL_DIR is owned by '$owner', not '$ME'. The kit expects it to be yours (sudo chown -R $ME: $INSTALL_DIR)."
            git=(git -c "safe.directory=$INSTALL_DIR" -C "$INSTALL_DIR")
            [[ -w "$INSTALL_DIR" ]] || git=(${SUDO:+"$SUDO"} "${git[@]}")
        fi
        if (( ! DRY_RUN )) && [[ -n "$("${git[@]}" status --porcelain --untracked-files=no 2>/dev/null)" ]]; then
            die "$INSTALL_DIR has local changes to tracked files. Commit, stash or discard them first (git -C $INSTALL_DIR status)."
        fi
        # Explicit refspec: also works on a single-branch clone and for a new --branch.
        run "${git[@]}" fetch origin "+refs/heads/$BRANCH:refs/remotes/origin/$BRANCH"
        if (( DRY_RUN )) || [[ "$("${git[@]}" rev-parse --abbrev-ref HEAD)" != "$BRANCH" ]]; then
            if "${git[@]}" show-ref --verify --quiet "refs/heads/$BRANCH"; then
                run "${git[@]}" checkout "$BRANCH"
            else
                run "${git[@]}" checkout -b "$BRANCH" "origin/$BRANCH"
            fi
        fi
        run "${git[@]}" merge --ff-only "origin/$BRANCH"
    elif [[ -e "$INSTALL_DIR" && -n "$(ls -A "$INSTALL_DIR" 2>/dev/null)" ]]; then
        die "$INSTALL_DIR exists, is not empty and is not a git checkout. Move it aside first."
    else
        run_root install -d -m 0755 -o "$ME" -g "$MY_GROUP" "$INSTALL_DIR"
        run git clone --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
    fi
    if [[ -d "$INSTALL_DIR" && ! -w "$INSTALL_DIR" ]]; then
        FSUDO="$SUDO"
    fi
    ensure_docker_cfg
}

# The private DOCKER_CONFIG dir is created by and owned by this user before any root
# docker command runs, so a root build cannot create (and own) it.
ensure_docker_cfg() {
    if [[ ! -d "$DOCKER_CFG" ]]; then
        if [[ -n "$FSUDO" ]]; then
            run_root install -d -m 0755 -o "$ME" -g "$MY_GROUP" "$DOCKER_CFG"
        else
            run install -d -m 0755 "$DOCKER_CFG"
        fi
    elif [[ "$(stat -c %U "$DOCKER_CFG" 2>/dev/null)" != "$ME" ]]; then
        run_root chown "$ME:$MY_GROUP" "$DOCKER_CFG"
    fi
}

# ----------------------------------------------------------------------------- .env

ENV_FILE="$INSTALL_DIR/.env"
ENV_BACKED_UP=0

# KEY's value in FILE: the last assignment wins (as for env_set), quotes stripped.
env_get() {
    local file="$1" key="$2" line val
    [[ -f "$file" && -r "$file" ]] || return 0
    line="$(grep -E "^[[:space:]]*${key}=" "$file" | tail -n1 || true)"
    [[ -n "$line" ]] || return 0
    val="${line#*=}"
    val="${val%$'\r'}"
    val="${val#"${val%%[![:space:]]*}"}"
    val="${val%"${val##*[![:space:]]}"}"
    if [[ "$val" =~ ^\"(.*)\"$ || "$val" =~ ^\'(.*)\'$ ]]; then val="${BASH_REMATCH[1]}"; fi
    printf '%s' "$val"
}

# Set KEY=VALUE in FILE: replace the LAST KEY= line (the one env_get and dotenv readers
# use), else append. Atomic: a temp file in the same dir, then mv. The previous file is
# kept once per run as .env.bak. Callers only call this when KEY is unset. The value is
# never printed.
env_set() {
    local file="$1" key="$2" value="$3" label="${4:-$3}"
    if (( DRY_RUN )); then
        info "[dry-run] set $key=$label in $file"
        return
    fi
    local dir tmp
    dir="$(dirname "$file")"
    tmp="$(${FSUDO:+"$FSUDO"} mktemp "$dir/.env.tmp.XXXXXX")"   # mode 600
    if grep -qE "^[[:space:]]*${key}=" "$file"; then
        KEY="$key" VALUE="$value" awk '
            BEGIN { k = ENVIRON["KEY"]; v = ENVIRON["VALUE"]; re = "^[[:space:]]*" k "=" }
            NR == FNR { if ($0 ~ re) last = FNR; next }
            FNR == last { print k "=" v; next }
            { print }' "$file" "$file" | ${FSUDO:+"$FSUDO"} tee "$tmp" >/dev/null
    else
        {
            cat "$file"
            [[ -z "$(tail -c1 "$file")" ]] || printf '\n'
            printf '%s=%s\n' "$key" "$value"
        } | ${FSUDO:+"$FSUDO"} tee "$tmp" >/dev/null
    fi
    if [[ -n "$FSUDO" ]]; then
        "$FSUDO" chown "$ME:$MY_GROUP" "$tmp"
    fi
    ${FSUDO:+"$FSUDO"} chmod 600 "$tmp"
    if (( ! ENV_BACKED_UP )); then
        ${FSUDO:+"$FSUDO"} cp -p "$file" "$dir/.env.bak"
        ${FSUDO:+"$FSUDO"} chmod 600 "$dir/.env.bak"
        ENV_BACKED_UP=1
    fi
    ${FSUDO:+"$FSUDO"} mv -f "$tmp" "$file"
    info "set $key=$label"
}

# .env is owned by this user and mode 600, checked on every run.
fix_env_perms() {
    [[ -f "$ENV_FILE" ]] || return 0
    local owner
    owner="$(stat -c %U "$ENV_FILE" 2>/dev/null || echo "?")"
    if [[ "$owner" != "$ME" ]]; then
        run_root chown "$ME:$MY_GROUP" "$ENV_FILE"
    fi
    if [[ "$(stat -c %a "$ENV_FILE")" != 600 ]]; then
        run ${FSUDO:+"$FSUDO"} chmod 600 "$ENV_FILE"
    fi
}

# Fill KEY only when it is empty (or the template placeholder). Never overwrites: when
# the value differs from WANT it sets MISMATCH=1 for the caller to report.
MISMATCH=0
fill_or_check() {  # key want read_from [placeholder]
    local key="$1" want="$2" from="$3" placeholder="${4:-}" cur
    MISMATCH=0
    cur="$(env_get "$from" "$key")"
    if [[ -z "$cur" || ( -n "$placeholder" && "$cur" == "$placeholder" ) ]]; then
        env_set "$ENV_FILE" "$key" "$want"
    elif [[ "$cur" != "$want" ]]; then
        MISMATCH=1
    else
        info "$key=$cur (already set)"
    fi
}

prepare_env() {
    log "Configuration file $ENV_FILE"
    local read_from="$ENV_FILE"

    if [[ -f "$ENV_FILE" ]]; then
        info ".env exists; only empty keys are filled"
        fix_env_perms
    else
        run ${FSUDO:+"$FSUDO"} install -m 600 "$INSTALL_DIR/.env.example" "$ENV_FILE"
        [[ -z "$FSUDO" ]] || run_root chown "$ME:$MY_GROUP" "$ENV_FILE"
        (( ! DRY_RUN )) || read_from="$INSTALL_DIR/.env.example"   # what the copy will contain
    fi

    local cur
    cur="$(env_get "$read_from" PUBLIC_BASE_URL)"
    fill_or_check PUBLIC_BASE_URL "$BASE_URL" "$read_from" "$EXAMPLE_BASE_URL"
    if (( MISMATCH )); then
        warn "PUBLIC_BASE_URL in .env is '$cur', not '$BASE_URL'. Left unchanged (this script never overwrites a value). That is fine if it is your own DNS name for $PUBLIC_IP (for example an Azure FQDN). If the IP changed, edit it by hand and update the SolarEdge app's redirect URI to match."
    fi
    [[ "$cur" != */ ]] || warn "PUBLIC_BASE_URL ends with '/'. Remove the trailing slash: Caddy uses it as the site address."
    SITE_URL="$BASE_URL"
    if [[ -n "$cur" && "$cur" != "$EXAMPLE_BASE_URL" ]]; then SITE_URL="${cur%/}"; fi

    # Plain `docker compose` from $INSTALL_DIR then targets this kit's files and project.
    fill_or_check COMPOSE_FILE "$COMPOSE_FILE_VALUE" "$read_from"
    (( ! MISMATCH )) || warn "COMPOSE_FILE in .env is '$(env_get "$read_from" COMPOSE_FILE)', expected '$COMPOSE_FILE_VALUE'. Left unchanged; use deploy/ec2/compose, which ignores it."
    fill_or_check COMPOSE_PROJECT_NAME "$PROJECT" "$read_from"
    (( ! MISMATCH )) || stop_check "COMPOSE_PROJECT_NAME in .env is '$(env_get "$read_from" COMPOSE_PROJECT_NAME)'; this kit needs '$PROJECT'. Fix it by hand."

    # Network values: load_net_settings already read any set in .env and derived the rest.
    fill_or_check SOLYA_SUBNET "$NET_SUBNET" "$read_from"
    fill_or_check SOLYA_IP_RANGE "$NET_RANGE" "$read_from"
    fill_or_check SOLYA_CADDY_IP "$NET_CADDY" "$read_from"

    # The image the stack runs: pulled from GHCR, or built locally with --build. Pin a fixed
    # version by setting it by hand, e.g. ghcr.io/ofiramir/solya:sha-<commit>; never overwritten.
    fill_or_check SOLYA_IMAGE "$DEFAULT_IMAGE" "$read_from"

    if [[ -n "$(env_get "$read_from" TRUSTED_PROXIES)" ]]; then
        info "Note: TRUSTED_PROXIES in .env is ignored; docker-compose.caddy.yml sets it to SOLYA_CADDY_IP ($NET_CADDY)."
    fi
}

generate_key_if_missing() {
    local read_from="$ENV_FILE"
    [[ -f "$read_from" ]] || read_from="$INSTALL_DIR/.env.example"
    if [[ -n "$(env_get "$read_from" TOKEN_ENCRYPTION_KEY)" ]]; then
        info "TOKEN_ENCRYPTION_KEY already set; kept"
        return
    fi
    log "Generating TOKEN_ENCRYPTION_KEY (solya genkey)"
    if (( DRY_RUN )); then
        dc run --rm --no-deps -T solya solya genkey
        env_set "$ENV_FILE" TOKEN_ENCRYPTION_KEY "<generated>" "<generated key>"
        return
    fi
    local key
    key="$(dc_capture run --rm --no-deps -T solya solya genkey | tr -d '\r' | tail -n1)"
    [[ "$key" =~ ^[A-Za-z0-9_-]{43}=$ ]] || die "solya genkey did not print a Fernet key"
    env_set "$ENV_FILE" TOKEN_ENCRYPTION_KEY "$key" "<generated key, not shown>"
    info "Keep a safe copy of TOKEN_ENCRYPTION_KEY from $ENV_FILE. If it is lost, every site must be connected again."
}

missing_secrets() {
    local read_from="$ENV_FILE" k
    [[ -f "$read_from" ]] || read_from="$INSTALL_DIR/.env.example"
    for k in "${REQUIRED_SECRETS[@]}"; do
        [[ -n "$(env_get "$read_from" "$k")" ]] || printf '%s\n' "$k"
    done
}

# ----------------------------------------------------------------------------- stack

# Get the image named by SOLYA_IMAGE: pull it from the registry, or with --build build it
# from the checked-out source (compose tags the result with that name). Never falls back
# from one to the other silently.
get_image() {
    local image
    image="$(env_get "$ENV_FILE" SOLYA_IMAGE)"
    [[ -n "$image" ]] || image="$DEFAULT_IMAGE"
    if (( BUILD )); then
        log "Building the image from source in $INSTALL_DIR, tagged $image"
        dc build solya
        return
    fi
    log "Pulling the image $image"
    # --policy always: the service's pull_policy is `missing`, which would skip a tag
    # (such as latest) that is already present locally and so never update it.
    if ! dc pull --policy always solya; then
        die "could not pull $image. If the package is private, log in once with a token that has read:packages (user: your GitHub name, password: the token): sudo env DOCKER_CONFIG=$DOCKER_CFG docker login ghcr.io  (this kit reads credentials from its own DOCKER_CONFIG, so a plain sudo docker login is not seen), then re-run. Or build from this checkout instead: $0 --build"
    fi
}

build_and_check() {
    get_image
    log "Checking Pillow's Raqm layout (Hebrew report card)"
    dc run --rm --no-deps -T solya python -c "from solya.render.card import assert_raqm; assert_raqm()"
    (( DRY_RUN )) || info "Raqm OK"
}

wait_for_health() {
    local url="${SITE_URL:-$BASE_URL}/healthz" _
    log "Waiting for $url (the first certificate can take a minute)"
    for _ in $(seq 1 24); do
        if curl -fsS --max-time 5 "$url" >/dev/null 2>&1; then
            info "healthy: $url"
            return 0
        fi
        sleep 5
    done
    warn "$url did not answer yet. Check: $INSTALL_DIR/deploy/ec2/compose logs caddy solya. See Troubleshooting in deploy/ec2/README.md."
}

start_stack() {
    local missing
    missing="$(missing_secrets)"
    if [[ -n "$missing" ]]; then
        log "Starting Caddy only (Solya waits for the secrets below)"
        dc up -d caddy
        return 1
    fi
    log "Starting Solya and Caddy"
    dc up -d --remove-orphans
    (( DRY_RUN )) || wait_for_health
    return 0
}

summary() {
    local started="$1" missing k site="${SITE_URL:-$BASE_URL}" host
    missing="$(missing_secrets)"
    host="${site#*://}"
    log "Summary"
    info "Host:              $host"
    info "PUBLIC_BASE_URL:   $site"
    info "OAuth redirect URI (register this in the SolarEdge app): $site/oauth/callback"
    info "Health check:      $site/healthz"
    info "Compose commands:  $INSTALL_DIR/deploy/ec2/compose <command>   (sudo docker compose for this kit)"
    if [[ -n "$missing" ]]; then
        printf '\n'
        info "Fill these keys in $ENV_FILE (they are empty):"
        for k in $missing; do
            case "$k" in
                SOLAREDGE_CLIENT_ID|SOLAREDGE_CLIENT_SECRET) info "  $k   (SolarEdge developer app, from Oren)" ;;
                TELEGRAM_BOT_TOKEN)    info "  $k   (BotFather)" ;;
                TELEGRAM_BOT_USERNAME) info "  $k   (BotFather, without the @)" ;;
            esac
        done
        info "Then re-run this script, or: $INSTALL_DIR/deploy/ec2/compose up -d"
    fi
    local read_from="$ENV_FILE"
    [[ -f "$read_from" ]] || read_from="$INSTALL_DIR/.env.example"
    local chat_ids pw
    chat_ids="$(env_get "$read_from" ADMIN_CHAT_IDS)"
    pw="$(env_get "$read_from" ADMIN_PASSWORD)"
    if [[ -z "$chat_ids" || "$chat_ids" == "[]" || -z "$pw" ]]; then
        printf '\n'
        info "Recommended (optional):"
        [[ -n "$chat_ids" && "$chat_ids" != "[]" ]] || info "  ADMIN_CHAT_IDS   JSON list of Telegram chat ids for admin notices, e.g. [123456789]"
        [[ -n "$pw" ]] || info "  ADMIN_PASSWORD   password for the read-only /admin page (404 while unset)"
    fi
    if [[ "$started" == 1 ]]; then
        printf '\n'
        info "Next: $INSTALL_DIR/deploy/ec2/compose exec solya solya invite create"
    fi
    (( ! DRY_RUN )) || { printf '\n'; info "Dry-run: nothing was changed."; }
}

# ----------------------------------------------------------------------------- main

main() {
    parse_args "$@"
    (( DRY_RUN )) && log "DRY RUN: printing planned actions only"

    # Everything that can refuse runs before anything is installed or changed.
    log "Preflight"
    detect_os
    setup_sudo
    check_resources
    resolve_ip
    load_net_settings
    check_project
    check_ports
    check_subnet

    log "Packages"
    install_git
    install_docker
    if (( INSTALLED_DOCKER && ! DRY_RUN )); then
        check_subnet   # a fresh Docker brings its own bridge network
    fi

    fetch_repo
    install_compose_plugin
    install_buildx_plugin
    prepare_env
    build_and_check
    generate_key_if_missing

    local started=0
    if start_stack; then started=1; fi
    summary "$started"
}

# Everything above is only definitions, so a `git pull` that rewrites this file while it
# runs cannot change what this run executes.
main "$@"
