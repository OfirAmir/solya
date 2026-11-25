#!/usr/bin/env bash
# Bring up Solya on an EC2 instance for a trial: Docker + compose, the repo in /opt/solya,
# a .env with PUBLIC_BASE_URL on <a-b-c-d>.sslip.io and a fresh TOKEN_ENCRYPTION_KEY, the
# image built and checked, and the stack (Solya + Caddy with a Let's Encrypt certificate)
# started. Safe to re-run: it never overwrites a value already set in .env.
#
# Supported: Amazon Linux 2023, Ubuntu 22.04 and 24.04. Run as a user who can sudo.
#
#   deploy/ec2/setup.sh [--branch NAME] [--ip A.B.C.D] [--repo URL] [--dry-run]
#
# See deploy/ec2/README.md for the full walkthrough.

set -euo pipefail

DEFAULT_BRANCH="claude/dreamy-turing-jyrdw1"
DEFAULT_REPO="https://github.com/OfirAmir/solya.git"
INSTALL_DIR="${SOLYA_DIR:-/opt/solya}"
OVERRIDE_REL="deploy/ec2/docker-compose.caddy.yml"
COMPOSE_FILE_VALUE="docker-compose.yml:${OVERRIDE_REL}"
EXAMPLE_BASE_URL="https://solya.example.com"
COMPOSE_MIN="2.24.0"   # `ports: !reset []` in the override needs 2.24+
COMPOSE_VERSION="${SOLYA_COMPOSE_VERSION:-v2.32.4}"   # used only when the plugin is missing or too old
BUILDX_VERSION="${SOLYA_BUILDX_VERSION:-v0.20.1}"     # used only when buildx is missing
PLUGIN_DIR="/usr/local/lib/docker/cli-plugins"

# Keys the service cannot start without, and that only a person can fill.
REQUIRED_SECRETS=(SOLAREDGE_CLIENT_ID SOLAREDGE_CLIENT_SECRET TELEGRAM_BOT_TOKEN TELEGRAM_BOT_USERNAME)

BRANCH="$DEFAULT_BRANCH"
REPO_URL="$DEFAULT_REPO"
IP_ARG=""
DRY_RUN=0

SUDO=""
FSUDO=""   # prefix for writes inside INSTALL_DIR when it is not writable by this user
OS_ID=""
OS_VERSION=""
OS_CODENAME=""

# ----------------------------------------------------------------------------- output

log()  { printf '\n==> %s\n' "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '\n!!  %s\n' "$*" >&2; }
die()  { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<EOF
Usage: $0 [--branch NAME] [--ip A.B.C.D] [--repo URL] [--dry-run]

  --branch NAME   git branch to deploy (default: $DEFAULT_BRANCH)
  --ip A.B.C.D    public IPv4 to build the sslip.io host from. Wins over the
                  EC2 metadata lookup; needed when IMDSv2 is not reachable.
  --repo URL      git URL to clone (default: $DEFAULT_REPO)
  --dry-run       print the planned actions; change nothing
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

# docker compose against both files, from the install dir, as root.
dc() {
    run_root docker compose --project-directory "$INSTALL_DIR" \
        -f "$INSTALL_DIR/docker-compose.yml" -f "$INSTALL_DIR/$OVERRIDE_REL" "$@"
}

# Same, but for capturing output (never in dry-run; callers guard it). Not echoed.
dc_capture() {
    ${SUDO:+"$SUDO"} docker compose --project-directory "$INSTALL_DIR" \
        -f "$INSTALL_DIR/docker-compose.yml" -f "$INSTALL_DIR/$OVERRIDE_REL" "$@"
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
            --dry-run)  DRY_RUN=1; shift ;;
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
        *)
            if (( DRY_RUN )); then
                warn "unsupported OS '$OS_ID $OS_VERSION' (supported: Amazon Linux 2023, Ubuntu 22.04/24.04). Dry-run continues; a real run would stop here."
            else
                die "unsupported OS '$OS_ID $OS_VERSION'. Supported: Amazon Linux 2023, Ubuntu 22.04, Ubuntu 24.04."
            fi
            ;;
    esac
}

setup_sudo() {
    if [[ "$(id -u)" -eq 0 ]]; then
        SUDO=""
    else
        command -v sudo >/dev/null 2>&1 || die "not root and sudo is not installed"
        SUDO="sudo"
        if (( ! DRY_RUN )); then
            sudo -v || die "this user cannot sudo"
        fi
    fi
}

check_resources() {
    local mem_kb avail_kb
    mem_kb="$(awk '/^MemTotal:/ {print $2}' /proc/meminfo 2>/dev/null || echo 0)"
    if [[ -n "$mem_kb" ]] && (( mem_kb > 0 && mem_kb < 900000 )); then
        warn "only $(( mem_kb / 1024 )) MB RAM. About 1 GB is the minimum; the image build may fail. Consider a larger instance or adding swap."
    fi
    avail_kb="$(df -Pk "$(dirname "$INSTALL_DIR")" 2>/dev/null | awk 'NR==2 {print $4}' || echo 0)"
    if [[ -n "$avail_kb" ]] && (( avail_kb > 0 && avail_kb < 4000000 )); then
        warn "only $(( avail_kb / 1024 )) MB free disk under $(dirname "$INSTALL_DIR"). Plan for about 10 GB."
    fi
    if command -v ss >/dev/null 2>&1; then
        local listeners
        listeners="$(ss -ltnH 2>/dev/null | awk '{print $4}' | grep -E '(^|[:.])(80|443)$' || true)"
        if [[ -n "$listeners" ]]; then
            warn "something already listens on port 80 or 443:"
            # shellcheck disable=SC2086  # one address per line
            printf '        %s\n' $listeners >&2
            info "If that is not this stack's Caddy (from an earlier run), stop it, or Caddy cannot start."
        fi
    fi
}

# ----------------------------------------------------------------------------- public IP

valid_ipv4() {
    local ip="$1" o
    [[ "$ip" =~ ^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})$ ]] || return 1
    for o in "${BASH_REMATCH[@]:1}"; do (( 10#$o <= 255 )) || return 1; done
}

is_private_ipv4() {
    [[ "$1" =~ ^10\. || "$1" =~ ^192\.168\. || "$1" =~ ^172\.(1[6-9]|2[0-9]|3[01])\. || "$1" =~ ^127\. || "$1" =~ ^169\.254\. ]]
}

imds_public_ip() {
    local token
    token="$(curl -fsS --noproxy '*' --max-time 3 -X PUT \
        -H 'X-aws-ec2-metadata-token-ttl-seconds: 300' \
        http://169.254.169.254/latest/api/token 2>/dev/null)" || return 1
    [[ -n "$token" ]] || return 1
    curl -fsS --noproxy '*' --max-time 3 \
        -H "X-aws-ec2-metadata-token: $token" \
        http://169.254.169.254/latest/meta-data/public-ipv4 2>/dev/null
}

PUBLIC_IP=""
SSLIP_HOST=""
BASE_URL=""

resolve_ip() {
    if [[ -n "$IP_ARG" ]]; then
        PUBLIC_IP="$IP_ARG"
        info "public IP from --ip: $PUBLIC_IP"
    elif PUBLIC_IP="$(imds_public_ip)" && [[ -n "$PUBLIC_IP" ]]; then
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

# ----------------------------------------------------------------------------- packages

version_ge() {  # version_ge A B: A >= B
    [[ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -n1)" == "$2" ]]
}

compose_version() {
    docker compose version --short 2>/dev/null | sed 's/^v//' || true
}

install_git() {
    if command -v git >/dev/null 2>&1; then info "git: present"; return; fi
    log "Installing git"
    case "$OS_ID" in
        amzn)   run_root dnf install -y git ;;
        ubuntu) run_root apt-get update; run_root apt-get install -y git ;;
        *)      info "(no package plan for this OS)" ;;
    esac
}

install_docker() {
    if command -v docker >/dev/null 2>&1; then
        info "docker: present ($(docker --version 2>/dev/null || echo unknown))"
    else
        log "Installing Docker"
        case "$OS_ID" in
            amzn)
                run_root dnf install -y docker
                ;;
            ubuntu)
                local arch
                arch="$(dpkg --print-architecture 2>/dev/null || echo amd64)"
                run_root apt-get update
                run_root apt-get install -y ca-certificates curl
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
                run_root apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
                ;;
            *) info "(no package plan for this OS)" ;;
        esac
    fi

    if command -v systemctl >/dev/null 2>&1; then
        if systemctl is-active --quiet docker 2>/dev/null && systemctl is-enabled --quiet docker 2>/dev/null; then
            info "docker service: running and enabled"
        else
            run_root systemctl enable --now docker
        fi
    fi

    local me
    me="$(id -un)"
    if [[ "$me" != "root" ]] && ! id -nG "$me" 2>/dev/null | tr ' ' '\n' | grep -qx docker; then
        run_root usermod -aG docker "$me"
        info "Added $me to the docker group (takes effect at the next login). The guide uses sudo anyway."
    fi
}

docker_arch() {  # $1 = compose|buildx
    case "$(uname -m)" in
        x86_64)        [[ "$1" == compose ]] && echo x86_64  || echo amd64 ;;
        aarch64|arm64) [[ "$1" == compose ]] && echo aarch64 || echo arm64 ;;
        *) die "unsupported CPU architecture $(uname -m)" ;;
    esac
}

install_plugin_binary() {  # $1 = file name in PLUGIN_DIR, $2 = url, $3 = expected sha256 (may be empty)
    local name="$1" url="$2" sum="$3"
    if (( DRY_RUN )); then
        info "[dry-run] download $url, verify its sha256, install as $PLUGIN_DIR/$name"
        return
    fi
    local tmp
    tmp="$(mktemp -d)"
    curl -fsSL "$url" -o "$tmp/$name" || { rm -rf "$tmp"; die "download failed: $url"; }
    if [[ -n "$sum" ]]; then
        printf '%s  %s\n' "$sum" "$tmp/$name" | sha256sum -c --quiet - || { rm -rf "$tmp"; die "checksum mismatch for $url"; }
    else
        warn "no checksum published for $url; installed unverified"
    fi
    run_root install -d -m 0755 "$PLUGIN_DIR"
    run_root install -m 0755 "$tmp/$name" "$PLUGIN_DIR/$name"
    rm -rf "$tmp"
}

install_compose_plugin() {
    local have
    have="$(compose_version)"
    if [[ -n "$have" ]] && version_ge "$have" "$COMPOSE_MIN"; then
        info "docker compose: $have (>= $COMPOSE_MIN)"
        return
    fi
    if [[ -n "$have" ]]; then
        log "docker compose $have is older than $COMPOSE_MIN; installing $COMPOSE_VERSION"
    else
        log "Installing the docker compose plugin $COMPOSE_VERSION"
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
        version_ge "${have:-0}" "$COMPOSE_MIN" || die "docker compose is still '${have:-missing}' after install. An older plugin in ~/.docker/cli-plugins may shadow $PLUGIN_DIR."
        info "docker compose: $have"
    fi
}

install_buildx_plugin() {
    if docker buildx version >/dev/null 2>&1; then
        info "docker buildx: present"
        return
    fi
    log "Installing the docker buildx plugin $BUILDX_VERSION (compose build uses it)"
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
        local git=(git -C "$INSTALL_DIR")
        if [[ ! -w "$INSTALL_DIR" ]]; then
            git=(${SUDO:+"$SUDO"} git -c "safe.directory=$INSTALL_DIR" -C "$INSTALL_DIR")
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
        local me
        me="$(id -un)"
        run_root install -d -m 0755 -o "$me" -g "$(id -gn)" "$INSTALL_DIR"
        run git clone --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
    fi
    if [[ -d "$INSTALL_DIR" && ! -w "$INSTALL_DIR" ]]; then
        FSUDO="$SUDO"
    fi
}

# ----------------------------------------------------------------------------- .env

ENV_FILE=""

# Print KEY's value from FILE (last uncommented assignment), quotes stripped.
env_get() {
    local file="$1" key="$2" line val
    [[ -f "$file" ]] || return 0
    line="$(grep -E "^[[:space:]]*${key}=" "$file" | tail -n1 || true)"
    [[ -n "$line" ]] || return 0
    val="${line#*=}"
    val="${val%$'\r'}"
    val="${val#"${val%%[![:space:]]*}"}"
    val="${val%"${val##*[![:space:]]}"}"
    if [[ "$val" =~ ^\"(.*)\"$ || "$val" =~ ^\'(.*)\'$ ]]; then val="${BASH_REMATCH[1]}"; fi
    printf '%s' "$val"
}

# Set KEY=VALUE in FILE: replace an existing (empty) KEY= line, else append.
# Callers only call this when KEY is unset. The value is never printed.
env_set() {
    local file="$1" key="$2" value="$3" label="${4:-$3}"
    if (( DRY_RUN )); then
        info "[dry-run] set $key=$label in $file"
        return
    fi
    local tmp
    tmp="$(mktemp)"
    if grep -qE "^[[:space:]]*${key}=" "$file"; then
        KEY="$key" VALUE="$value" awk '
            BEGIN { k = ENVIRON["KEY"]; v = ENVIRON["VALUE"]; done = 0 }
            {
                if (!done && $0 ~ "^[[:space:]]*" k "=") { print k "=" v; done = 1 }
                else print
            }' "$file" > "$tmp"
    else
        cat "$file" > "$tmp"
        [[ -z "$(tail -c1 "$file")" ]] || printf '\n' >> "$tmp"
        printf '%s=%s\n' "$key" "$value" >> "$tmp"
    fi
    # cat into the file keeps its owner and mode (600)
    ${FSUDO:+"$FSUDO"} tee "$file" < "$tmp" >/dev/null
    rm -f "$tmp"
    info "set $key=$label"
}

prepare_env() {
    log "Configuration file $INSTALL_DIR/.env"
    ENV_FILE="$INSTALL_DIR/.env"
    local read_from="$ENV_FILE"

    if [[ -f "$ENV_FILE" ]]; then
        info ".env exists; only empty keys are filled"
    else
        run ${FSUDO:+"$FSUDO"} cp "$INSTALL_DIR/.env.example" "$ENV_FILE"
        run ${FSUDO:+"$FSUDO"} chmod 600 "$ENV_FILE"
        read_from="$INSTALL_DIR/.env.example"   # what the copy will contain (dry-run)
    fi
    if (( ! DRY_RUN )); then read_from="$ENV_FILE"; fi

    local cur
    cur="$(env_get "$read_from" PUBLIC_BASE_URL)"
    if [[ -z "$cur" || "$cur" == "$EXAMPLE_BASE_URL" ]]; then
        env_set "$ENV_FILE" PUBLIC_BASE_URL "$BASE_URL"
    elif [[ "${cur%/}" != "$BASE_URL" ]]; then
        warn "PUBLIC_BASE_URL in .env is '$cur', not '$BASE_URL'. Left unchanged (this script never overwrites a value). If the IP changed, edit it by hand and update the SolarEdge app's redirect URI to match."
    else
        info "PUBLIC_BASE_URL=$cur (already set)"
    fi
    if [[ "$cur" == */ ]]; then
        warn "PUBLIC_BASE_URL ends with '/'. Remove the trailing slash: Caddy uses it as the site address."
    fi

    # Lets plain `docker compose ...` in /opt/solya use the Caddy override too.
    cur="$(env_get "$read_from" COMPOSE_FILE)"
    if [[ -z "$cur" ]]; then
        env_set "$ENV_FILE" COMPOSE_FILE "$COMPOSE_FILE_VALUE"
    elif [[ "$cur" != "$COMPOSE_FILE_VALUE" ]]; then
        warn "COMPOSE_FILE in .env is '$cur', expected '$COMPOSE_FILE_VALUE'. Left unchanged."
    fi

    local tp
    tp="$(env_get "$read_from" TRUSTED_PROXIES)"
    if [[ -n "$tp" ]]; then
        info "Note: TRUSTED_PROXIES in .env is ignored; deploy/ec2/docker-compose.caddy.yml sets it to Caddy's address."
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

build_and_check() {
    log "Building the image"
    dc build
    log "Checking Pillow's Raqm layout (Hebrew report card)"
    dc run --rm --no-deps -T solya python -c "from solya.render.card import assert_raqm; assert_raqm()"
    (( DRY_RUN )) || info "Raqm OK"
}

wait_for_health() {
    local url="$BASE_URL/healthz" _
    log "Waiting for $url (the first certificate can take a minute)"
    for _ in $(seq 1 24); do
        if curl -fsS --max-time 5 "$url" >/dev/null 2>&1; then
            info "healthy: $url"
            return 0
        fi
        sleep 5
    done
    warn "$url did not answer yet. Check: sudo docker compose logs caddy solya (from $INSTALL_DIR). See Troubleshooting in deploy/ec2/README.md."
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
    local started="$1" missing k
    missing="$(missing_secrets)"
    log "Summary"
    info "Host:              $SSLIP_HOST"
    info "PUBLIC_BASE_URL:   $BASE_URL"
    info "OAuth redirect URI (register this in the SolarEdge app): $BASE_URL/oauth/callback"
    info "Health check:      $BASE_URL/healthz"
    if [[ -n "$missing" ]]; then
        printf '\n'
        info "Fill these keys in $INSTALL_DIR/.env (they are empty):"
        for k in $missing; do
            case "$k" in
                SOLAREDGE_CLIENT_ID|SOLAREDGE_CLIENT_SECRET) info "  $k   (SolarEdge developer app, from Oren)" ;;
                TELEGRAM_BOT_TOKEN)    info "  $k   (BotFather)" ;;
                TELEGRAM_BOT_USERNAME) info "  $k   (BotFather, without the @)" ;;
            esac
        done
        info "Then re-run this script, or: cd $INSTALL_DIR && sudo docker compose up -d"
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
        info "Next: cd $INSTALL_DIR && sudo docker compose exec solya solya invite create"
    fi
    (( ! DRY_RUN )) || { printf '\n'; info "Dry-run: nothing was changed."; }
}

# ----------------------------------------------------------------------------- main

main() {
    parse_args "$@"
    (( DRY_RUN )) && log "DRY RUN: printing planned actions only"

    log "Preflight"
    detect_os
    setup_sudo
    check_resources
    resolve_ip

    log "Packages"
    install_git
    install_docker
    install_compose_plugin
    install_buildx_plugin

    fetch_repo
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
