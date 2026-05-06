#!/usr/bin/env bash
# Ducky Pool installer for Ubuntu 24.04+
#
# What it does:
#   1. Installs Docker if not present
#   2. Drops the app config into /opt/ducky-pool
#   3. Generates a strong RPC password
#   4. Builds the web + ckpool images
#   5. Starts everything via docker compose
#   6. Installs a systemd unit so it boots with the host
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/ducksdev/ducky-installer/main/install.sh | sudo bash
#
# Or from a local clone:
#   sudo ./install.sh
#
# Idempotent: safe to re-run. Won't touch your data dirs or RPC password
# on subsequent runs.

set -euo pipefail

#────────────────────────────── config ──────────────────────────────#

INSTALL_DIR="${INSTALL_DIR:-/opt/ducky-pool}"
DATA_DIR="${DATA_DIR:-/var/lib/ducky-pool}"
SERVICE_NAME="ducky-pool"
STRATUM_PORT="${STRATUM_PORT:-4567}"
WEB_PORT="${WEB_PORT:-4568}"
P2P_PORT="${P2P_PORT:-8333}"
BCHN_IMAGE="${BCHN_IMAGE:-zquestz/bitcoin-cash-node:latest}"

# License signing secret. This is intentionally a fixed string in the
# distribution: license tokens minted with this secret (via the
# scripts/issue_license.py tool) will validate on every install that
# uses the same install.sh. Rotating this string in a future release
# invalidates ALL outstanding tokens, which is the revocation
# mechanism if a leak occurs.
#
# The secret being committed to the public repo is fine for our
# honor-system license model: HMAC just prevents trivial forging,
# enforcement is legal/contractual not cryptographic. Anyone running
# their own fork should generate their own secret and update this line.
DUCKY_LICENSE_SECRET="${DUCKY_LICENSE_SECRET:-CHANGEME-set-me-before-publishing-with-python3-c-secrets}"
DUCKY_PURCHASE_URL="${DUCKY_PURCHASE_URL:-https://ducksdev.gumroad.com/l/ducky-pool-pro}"
APP_VERSION="${APP_VERSION:-1.0}"
GITHUB_URL_DEFAULT="${GITHUB_URL:-https://github.com/ducksdev/ducky-installer}"

# Branch / channel switch. Default is `main` for stable users. Set
# BRANCH=dev (or another branch name) to fetch in-progress changes
# during development. Both branches share the same install.sh — the
# only difference is which branch the manifest pulls from.
#
#   Stable (default):  curl … main/install.sh | sudo bash
#   Dev channel:       curl … dev/install.sh  | sudo BRANCH=dev bash
#
# REPO_RAW can be overridden directly for testing against forks or
# private mirrors; BRANCH is the simpler knob most users will reach for.
BRANCH="${BRANCH:-main}"
REPO_RAW="${REPO_RAW:-https://raw.githubusercontent.com/ducksdev/ducky-installer/${BRANCH}}"

#────────────────────────────── helpers ──────────────────────────────#

c_blue()  { printf '\033[1;34m%s\033[0m\n' "$*"; }
c_green() { printf '\033[1;32m%s\033[0m\n' "$*"; }
c_red()   { printf '\033[1;31m%s\033[0m\n' "$*" >&2; }

step() { c_blue "▶ $*"; }
ok()   { c_green "  ✓ $*"; }
die()  { c_red "✗ $*"; exit 1; }

require_root() {
    if [ "${EUID}" -ne 0 ]; then
        die "Run as root: sudo $0"
    fi
}

detect_local_clone() {
    # If this script is being run from inside a checkout that has the
    # service files alongside it, use those instead of downloading.
    local here
    here="$(cd "$(dirname "$0")" && pwd)"
    if [ -f "$here/services/web/Dockerfile" ] && [ -f "$here/services/ckpool/Dockerfile" ]; then
        SOURCE_MODE="local"
        SOURCE_DIR="$here"
    else
        SOURCE_MODE="remote"
        SOURCE_DIR=""
    fi
}

fetch_or_copy() {
    local rel="$1"
    local dest="$2"
    if [ "$SOURCE_MODE" = "local" ]; then
        cp "$SOURCE_DIR/$rel" "$dest"
    else
        curl -fsSL "$REPO_RAW/$rel" -o "$dest" \
            || die "Could not download $rel from $REPO_RAW"
    fi
}

#────────────────────────────── steps ──────────────────────────────#

ensure_dependencies() {
    # Quick check + install for tools we need outside Docker. envsubst is
    # used to render ckpool.conf from the template; python3 to parse the
    # rendered config and the user's settings.json.
    local missing=()
    command -v envsubst >/dev/null 2>&1 || missing+=("gettext-base")
    command -v python3 >/dev/null 2>&1 || missing+=("python3")
    if [ ${#missing[@]} -gt 0 ]; then
        step "Installing host helpers (${missing[*]})"
        apt-get update -y -qq >/dev/null 2>&1 || true
        apt-get install -y -qq "${missing[@]}" >/dev/null 2>&1
        ok "Host helpers installed"
    fi
}

install_docker() {
    if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
        ok "Docker + Compose already installed"
        return
    fi

    step "Installing Docker"
    apt-get update -y
    apt-get install -y ca-certificates curl gnupg

    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg

    local codename
    codename="$(. /etc/os-release && echo "$VERSION_CODENAME")"
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $codename stable" \
        > /etc/apt/sources.list.d/docker.list

    apt-get update -y
    apt-get install -y \
        docker-ce docker-ce-cli containerd.io \
        docker-buildx-plugin docker-compose-plugin

    systemctl enable --now docker
    ok "Docker installed"
}

create_dirs() {
    step "Creating directories"
    mkdir -p \
        "$INSTALL_DIR/services/web/templates" \
        "$INSTALL_DIR/services/ckpool" \
        "$DATA_DIR/bchnode" \
        "$DATA_DIR/ckpool" \
        "$DATA_DIR/ckpool-config" \
        "$DATA_DIR/shared"
    ok "Layout ready under $INSTALL_DIR and $DATA_DIR"
}

generate_secrets() {
    step "Generating RPC credentials"
    local pass_file="$DATA_DIR/rpc.pass"
    if [ ! -s "$pass_file" ]; then
        head -c 24 /dev/urandom | base64 | tr -d '/+=' | head -c 32 > "$pass_file"
        chmod 600 "$pass_file"
        ok "Generated new RPC password at $pass_file"
    else
        ok "RPC password already exists, leaving it alone"
    fi
}

render_ckpool_config() {
    step "Rendering ckpool config"
    local rpc_pass
    rpc_pass="$(cat "$DATA_DIR/rpc.pass")"

    # Read settings from settings.json if it exists, otherwise use sensible
    # defaults. Users tune these in the dashboard's Settings page; the web
    # app rewrites settings.json and re-runs the render. On fresh install
    # there's no settings.json yet — defaults below match DEFAULT_SETTINGS
    # in app.py.
    local mindiff maxdiff startdiff
    if [ -f "$DATA_DIR/shared/settings.json" ]; then
        mindiff=$(python3 -c "import json,sys; print(json.load(open('$DATA_DIR/shared/settings.json')).get('mindiff', 1))" 2>/dev/null || echo 1)
        maxdiff=$(python3 -c "import json,sys; print(json.load(open('$DATA_DIR/shared/settings.json')).get('maxdiff', 0))" 2>/dev/null || echo 0)
        startdiff=$(python3 -c "import json,sys; print(json.load(open('$DATA_DIR/shared/settings.json')).get('startdiff', 1000))" 2>/dev/null || echo 1000)
    else
        mindiff=1
        maxdiff=0
        startdiff=1000
    fi

    # Payout address. ckpool refuses to start without a valid address, so
    # we fall back to a clearly-fake-looking address on fresh installs —
    # the user MUST set their real address before any mining can happen.
    # The dashboard's set_payout handler updates this file and re-renders.
    local payout
    if [ -f "$DATA_DIR/shared/payout.address" ]; then
        payout="$(cat "$DATA_DIR/shared/payout.address" | head -n 1 | tr -d '[:space:]')"
    fi
    if [ -z "${payout:-}" ]; then
        # Bitcoin Cash genesis address — placeholder. ckpool will accept it
        # syntactically but the user MUST change before mining or all
        # rewards go nowhere. The dashboard surfaces this as a big red
        # warning on the Payout card until they set a real address.
        payout="1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"
    fi

    # Substitute into template and write to a temp file first so we
    # never leave a half-written config on disk if envsubst fails.
    local template="$INSTALL_DIR/services/ckpool/ckpool.conf.template"
    local tmp="$DATA_DIR/ckpool-config/ckpool.conf.tmp"
    local out="$DATA_DIR/ckpool-config/ckpool.conf"

    BCH_RPC_HOST=bchnode \
    BCH_RPC_PORT=8332 \
    BCH_RPC_USER=bchrpc \
    BCH_RPC_PASS="$rpc_pass" \
    STRATUM_PORT="$STRATUM_PORT" \
    PAYOUT_ADDRESS="$payout" \
    POOL_SIG="/ducky-pool/" \
    MINDIFF="$mindiff" \
    MAXDIFF="$maxdiff" \
    STARTDIFF="$startdiff" \
    envsubst < "$template" > "$tmp"

    # Sanity check the rendered config is valid JSON before swapping it in.
    if ! python3 -m json.tool < "$tmp" >/dev/null 2>&1; then
        rm -f "$tmp"
        echo "  ✗ Rendered ckpool.conf is not valid JSON — refusing to install" >&2
        exit 1
    fi
    mv "$tmp" "$out"
    chmod 644 "$out"
    ok "ckpool config rendered to $out (mindiff=$mindiff maxdiff=$maxdiff startdiff=$startdiff)"
}

write_compose() {
    step "Writing docker-compose.yml"
    local rpc_pass
    rpc_pass="$(cat "$DATA_DIR/rpc.pass")"

    cat > "$INSTALL_DIR/docker-compose.yml" <<COMPOSE
# Generated by install.sh — do not edit by hand.
# Re-run install.sh to regenerate.

services:
  bchnode:
    image: ${BCHN_IMAGE}
    container_name: ducky-bchnode
    restart: unless-stopped
    stop_grace_period: 5m
    volumes:
      - ${DATA_DIR}/bchnode:/home/bitcoin/.bitcoin
    command:
      - bitcoind
      - -datadir=/home/bitcoin/.bitcoin
      - -server=1
      - -listen=1
      - -rpcbind=0.0.0.0
      - -rpcallowip=0.0.0.0/0
      - -rpcuser=bchrpc
      - -rpcpassword=${rpc_pass}
      - -rpcport=8332
      - -rpcworkqueue=64
      - -rpcthreads=8
      - -port=8333
      - -dbcache=512
      - -maxmempool=300
      # ZMQ block notifications — ckpool subscribes to this to learn
      # about new blocks instantly instead of RPC-polling every 100ms.
      # AxeBCH uses 28334 by convention; we follow suit. The socket is
      # only reachable on the docker network, no host port exposed.
      - -zmqpubhashblock=tcp://0.0.0.0:28334
    ports:
      - "${P2P_PORT}:8333"

  ckpool:
    image: ghcr.io/willitmod/wim-solo-ckpool:0.8.3-rc1-590fb2a
    container_name: ducky-ckpool
    restart: unless-stopped
    stop_grace_period: 30s
    # ckpool.conf is rendered by install.sh into ${DATA_DIR}/ckpool-config
    # and bind-mounted read-only here. To change settings: edit
    # ${DATA_DIR}/shared/settings.json (or use the dashboard) and the web
    # container's settings handler re-renders the config + drops the
    # .restart_ckpool marker. The host-side ducky-restart-watcher then
    # runs `docker compose restart ckpool` which picks up the new file.
    entrypoint: ["/bin/sh", "-ec"]
    command:
      - |
        # Stale pid file recovery — if Docker restarts our container
        # without recreating its writable layer, /tmp/ckpool/*.pid can
        # linger and block startup. Same trick AxeBCH uses.
        rm -f /tmp/ckpool/*.pid 2>/dev/null || true
        # ckpool flags: -k killold, -B BTCSOLO mode, -L log shares
        if command -v ckpool >/dev/null 2>&1; then
          exec ckpool -k -B -L -c /config/ckpool.conf
        elif [ -x /usr/bin/ckpool ]; then
          exec /usr/bin/ckpool -k -B -L -c /config/ckpool.conf
        else
          echo "ckpool binary not found in image"; exit 1
        fi
    volumes:
      - ${DATA_DIR}/ckpool-config:/config:ro
      - ${DATA_DIR}/ckpool:/var/log/ckpool:rw
    ports:
      - "${STRATUM_PORT}:${STRATUM_PORT}"
    depends_on:
      - bchnode

  web:
    build: ./services/web
    image: ducky-web:local
    container_name: ducky-web
    restart: unless-stopped
    stop_grace_period: 30s
    environment:
      FLASK_PORT: ${WEB_PORT}
      BCH_RPC_HOST: bchnode
      BCH_RPC_PORT: 8332
      BCH_RPC_USER: bchrpc
      BCH_RPC_PASS: ${rpc_pass}
      STRATUM_PORT: ${STRATUM_PORT}
      PAYOUT_ADDRESS_FILE: /shared/payout.address
      POOL_STATUS_FILE: /pool-logs/pool/pool.status
      WORKERS_DIR: /pool-logs/users
      # Health page reads host stats from /host-proc and disk usage from
      # /host-root. These are bind-mounted read-only below.
      HOST_PROC: /host-proc
      HOST_ROOT: /host-root
      BCHNODE_LOG: /bchnode-logs/debug.log
      # License signing secret. When set, validates Pro license tokens
      # signed with the SAME secret on the developer's side. When empty,
      # the dashboard runs in free tier with no Pro features. On
      # ducksdev's official builds this comes from a host-side env file
      # written by the installer; users running their own forks should
      # generate and bake in their own secret to issue tokens.
      DUCKY_LICENSE_SECRET: "${DUCKY_LICENSE_SECRET:-}"
      DUCKY_PURCHASE_URL: "${DUCKY_PURCHASE_URL:-https://ducksdev.gumroad.com/l/ducky-pool-pro}"
      APP_VERSION: "${APP_VERSION:-1.0}"
      GITHUB_URL: "${GITHUB_URL:-https://github.com/ducksdev/ducky-installer}"
    volumes:
      - ${DATA_DIR}/shared:/shared:rw
      # Read pool.status + workers/* and allow removing worker files
      # so the dashboard's "Forget worker" button can clean them up.
      - ${DATA_DIR}/ckpool:/pool-logs
      # Health page: system stats (read-only)
      - /proc:/host-proc:ro
      # Health page: disk usage of the data drive (read-only)
      - /:/host-root:ro
      # Health page: bchnode debug log tail (read-only)
      - ${DATA_DIR}/bchnode:/bchnode-logs:ro
    ports:
      - "${WEB_PORT}:${WEB_PORT}"
    depends_on:
      - bchnode
COMPOSE
    chmod 600 "$INSTALL_DIR/docker-compose.yml"   # contains the RPC pass
    ok "docker-compose.yml written"
}

fetch_app_files() {
    step "Fetching app files (from manifest)"

    # The manifest is a list of relative paths the installer should pull
    # from the repo. Adding new files only needs an entry here — no
    # installer code change.
    local manifest_path="$INSTALL_DIR/.manifest"
    fetch_or_copy MANIFEST "$manifest_path" \
        || die "Could not fetch MANIFEST"

    local count=0
    while IFS= read -r rel || [ -n "$rel" ]; do
        # skip blanks and # comments
        [ -z "$rel" ] && continue
        case "$rel" in \#*) continue;; esac
        rel="${rel#"${rel%%[![:space:]]*}"}"   # ltrim
        rel="${rel%"${rel##*[![:space:]]}"}"   # rtrim
        [ -z "$rel" ] && continue

        local dest="$INSTALL_DIR/$rel"
        mkdir -p "$(dirname "$dest")"
        fetch_or_copy "$rel" "$dest" \
            || die "Could not fetch $rel"
        count=$((count + 1))
    done < "$manifest_path"

    chmod +x "$INSTALL_DIR/services/ckpool/entrypoint.sh" 2>/dev/null || true
    rm -f "$manifest_path"
    ok "$count app files in place"
}

record_install_sha() {
    # Captures the GitHub commit SHA the install fetched. The dashboard
    # later compares this against the latest SHA on the branch to tell
    # the user whether updates are available. Best-effort — if curl
    # or GitHub's API are unavailable, we record what we know (channel
    # + timestamp) and the dashboard simply shows "unable to check."
    step "Recording install version"
    local meta_dir="$DATA_DIR/shared"
    local meta_file="$meta_dir/install_meta.json"
    mkdir -p "$meta_dir"

    local sha=""
    if [ "$SOURCE_MODE" = "remote" ] && command -v curl >/dev/null 2>&1; then
        # GitHub API: GET /repos/{owner}/{repo}/commits/{branch}
        # Returns 60 unauthenticated calls/hr per IP — fine for one
        # install + occasional dashboard checks.
        sha=$(curl -fsSL --max-time 8 \
            "https://api.github.com/repos/ducksdev/ducky-installer/commits/${BRANCH}" 2>/dev/null \
            | python3 -c "import json,sys; print(json.load(sys.stdin).get('sha',''))" 2>/dev/null \
            || true)
    fi

    cat > "$meta_file" <<EOF
{
    "branch": "$BRANCH",
    "sha": "$sha",
    "installed_at": $(date +%s),
    "repo": "ducksdev/ducky-installer"
}
EOF
    if [ -n "$sha" ]; then
        ok "Install SHA recorded: ${sha:0:8} (branch $BRANCH)"
    else
        ok "Install metadata recorded (SHA unavailable, update check disabled)"
    fi
}

build_and_start() {
    step "Building images (ckpool compile takes a few minutes the first time)"
    cd "$INSTALL_DIR"
    docker compose build
    ok "Images built"

    step "Starting stack"
    # Compose down first so stale container configs (e.g. an old ckpool
    # with /shared mounted read-only) don't survive into the new run.
    # Existing volumes and data are preserved — only the container
    # configs get rebuilt from the latest docker-compose.yml.
    if docker compose ps -q 2>/dev/null | grep -q .; then
        docker compose down
    fi
    docker compose up -d
    ok "Containers started"

    step "Verifying mount config"
    # Sanity check: ckpool's config file must be readable in the
    # container. The dashboard renders config changes onto the host's
    # ckpool-config dir; if the bind-mount didn't take, ckpool would
    # silently keep using whatever config it had at startup.
    sleep 2
    local conf_present
    conf_present="$(docker exec ducky-ckpool sh -c 'test -r /config/ckpool.conf && echo yes' 2>/dev/null || true)"
    if [ "$conf_present" = "yes" ]; then
        ok "ckpool config mount is healthy"
    else
        echo
        c_red "✗ FATAL: ckpool cannot read /config/ckpool.conf"
        echo "  Check that ${DATA_DIR}/ckpool-config/ckpool.conf exists and is readable."
        echo "  Inspect with: sudo docker logs ducky-ckpool"
        exit 1
    fi
}

install_systemd() {
    step "Installing systemd unit"
    cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<UNIT
[Unit]
Description=Ducky Pool (BCH node + ckpool + dashboard)
Requires=docker.service
After=docker.service network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=${INSTALL_DIR}
ExecStart=/usr/bin/docker compose up -d
ExecStop=/usr/bin/docker compose down
TimeoutStartSec=0

[Install]
WantedBy=multi-user.target
UNIT
    systemctl daemon-reload
    systemctl enable "${SERVICE_NAME}.service"
    ok "Service ${SERVICE_NAME} enabled — will start on boot"
}

install_restart_watcher() {
    # Watches the marker dir (/shared on the host = $DATA_DIR/shared)
    # for .restart_<service> files dropped by the dashboard, then runs
    # `docker compose restart <service>`. For ckpool we ALSO re-render
    # the ckpool.conf from the latest settings.json before restarting,
    # so config changes (mindiff/maxdiff/startdiff/payout) take effect.
    step "Installing host-side restart watcher"
    cat > "/usr/local/bin/ducky-restart-watcher.sh" <<WATCHER
#!/usr/bin/env bash
set -u
MARKER_DIR="${DATA_DIR}/shared"
COMPOSE_DIR="${INSTALL_DIR}"
DATA_DIR_HOST="${DATA_DIR}"
TEMPLATE="${INSTALL_DIR}/services/ckpool/ckpool.conf.template"
STRATUM_PORT_HOST="${STRATUM_PORT}"
mkdir -p "\$MARKER_DIR"

# Re-render ckpool.conf from settings.json + payout.address. Mirrors
# render_ckpool_config() in install.sh — keep in sync if you change one.
render_ckpool_config() {
    local rpc_pass mindiff maxdiff startdiff payout
    rpc_pass="\$(cat "\$DATA_DIR_HOST/rpc.pass")"
    if [ -f "\$DATA_DIR_HOST/shared/settings.json" ]; then
        mindiff=\$(python3 -c "import json; print(json.load(open('\$DATA_DIR_HOST/shared/settings.json')).get('mindiff', 1))" 2>/dev/null || echo 1)
        maxdiff=\$(python3 -c "import json; print(json.load(open('\$DATA_DIR_HOST/shared/settings.json')).get('maxdiff', 0))" 2>/dev/null || echo 0)
        startdiff=\$(python3 -c "import json; print(json.load(open('\$DATA_DIR_HOST/shared/settings.json')).get('startdiff', 1000))" 2>/dev/null || echo 1000)
    else
        mindiff=1; maxdiff=0; startdiff=1000
    fi
    if [ -f "\$DATA_DIR_HOST/shared/payout.address" ]; then
        payout="\$(cat "\$DATA_DIR_HOST/shared/payout.address" | head -n 1 | tr -d '[:space:]')"
    fi
    [ -z "\${payout:-}" ] && payout="1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"

    local tmp="\$DATA_DIR_HOST/ckpool-config/ckpool.conf.tmp"
    local out="\$DATA_DIR_HOST/ckpool-config/ckpool.conf"
    BCH_RPC_HOST=bchnode \\
    BCH_RPC_PORT=8332 \\
    BCH_RPC_USER=bchrpc \\
    BCH_RPC_PASS="\$rpc_pass" \\
    STRATUM_PORT="\$STRATUM_PORT_HOST" \\
    PAYOUT_ADDRESS="\$payout" \\
    POOL_SIG="/ducky-pool/" \\
    MINDIFF="\$mindiff" \\
    MAXDIFF="\$maxdiff" \\
    STARTDIFF="\$startdiff" \\
    envsubst < "\$TEMPLATE" > "\$tmp"
    if python3 -m json.tool < "\$tmp" >/dev/null 2>&1; then
        mv "\$tmp" "\$out"
        chmod 644 "\$out"
        echo "[ducky-restart-watcher] re-rendered ckpool.conf (min=\$mindiff max=\$maxdiff start=\$startdiff)"
        return 0
    else
        rm -f "\$tmp"
        echo "[ducky-restart-watcher] ckpool.conf render FAILED (invalid JSON) — keeping old config"
        return 1
    fi
}

handle_marker() {
    local marker="\$1"
    local svc="\$2"
    if [ -e "\$marker" ]; then
        echo "[ducky-restart-watcher] \$svc restart requested"
        local stamp
        stamp=\$(date +%s)
        local staged="\$marker.processing.\$stamp"
        if mv "\$marker" "\$staged" 2>/dev/null; then
            # ckpool needs a fresh-rendered config before restart.
            if [ "\$svc" = "ckpool" ]; then
                render_ckpool_config || true
            fi
            (
                cd "\$COMPOSE_DIR" || exit 1
                /usr/bin/docker compose restart "\$svc"
            )
            local rc=\$?
            rm -f "\$staged"
            if [ \$rc -eq 0 ]; then
                echo "[ducky-restart-watcher] \$svc restarted (rc=0)"
            else
                echo "[ducky-restart-watcher] \$svc restart FAILED (rc=\$rc)"
            fi
        fi
    fi
}

# Pool-wide stats wipe. Different from a plain restart: we have to STOP
# ckpool first, then delete the user files + pool.status (which contain
# the bestshare cache + per-worker share counts), then START ckpool.
# Doing it while ckpool is running is racey — ckpool's flush thread
# rewrites the files faster than we can delete them. The dashboard
# drops .restart_ckpool_wipe when the user clicks "Reset stats".
handle_wipe_marker() {
    local marker="\$MARKER_DIR/.restart_ckpool_wipe"
    if [ -e "\$marker" ]; then
        echo "[ducky-restart-watcher] ckpool WIPE+restart requested"
        local stamp
        stamp=\$(date +%s)
        local staged="\$marker.processing.\$stamp"
        if mv "\$marker" "\$staged" 2>/dev/null; then
            (
                cd "\$COMPOSE_DIR" || exit 1
                /usr/bin/docker compose stop ckpool
            )
            # ckpool is now stopped — safe to delete state files.
            local users_dir="\$DATA_DIR_HOST/ckpool/users"
            local pool_status="\$DATA_DIR_HOST/ckpool/pool/pool.status"
            local pool_users="\$DATA_DIR_HOST/ckpool/pool/users"
            local pool_workers="\$DATA_DIR_HOST/ckpool/pool/workers"
            if [ -d "\$users_dir" ]; then
                find "\$users_dir" -mindepth 1 -maxdepth 1 -type f -delete 2>/dev/null || true
                echo "[ducky-restart-watcher] cleared per-user files in \$users_dir"
            fi
            for f in "\$pool_status" "\$pool_users" "\$pool_workers"; do
                if [ -f "\$f" ]; then
                    rm -f "\$f"
                    echo "[ducky-restart-watcher] removed \$f"
                fi
            done
            # Re-render config in case settings changed since last start.
            render_ckpool_config || true
            (
                cd "\$COMPOSE_DIR" || exit 1
                /usr/bin/docker compose start ckpool
            )
            local rc=\$?
            rm -f "\$staged"
            if [ \$rc -eq 0 ]; then
                echo "[ducky-restart-watcher] ckpool wiped + started (rc=0)"
            else
                echo "[ducky-restart-watcher] ckpool wipe-start FAILED (rc=\$rc)"
            fi
        fi
    fi
}

while true; do
    handle_wipe_marker
    handle_marker "\$MARKER_DIR/.restart_ckpool"  "ckpool"
    handle_marker "\$MARKER_DIR/.restart_bchnode" "bchnode"
    handle_marker "\$MARKER_DIR/.restart_web"     "web"
    sleep 2
done
WATCHER
    chmod 755 "/usr/local/bin/ducky-restart-watcher.sh"

    cat > "/etc/systemd/system/ducky-restart-watcher.service" <<UNIT
[Unit]
Description=Ducky Pool — host-side restart watcher
After=docker.service ${SERVICE_NAME}.service
Requires=docker.service

[Service]
Type=simple
ExecStart=/usr/local/bin/ducky-restart-watcher.sh
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=multi-user.target
UNIT
    systemctl daemon-reload
    systemctl enable --now ducky-restart-watcher.service
    ok "Restart watcher installed and running"
}

print_summary() {
    local ip
    ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
    [ -z "$ip" ] && ip="<this-host>"

    echo
    c_green "════════════════════════════════════════════════════════════"
    c_green "  Ducky Pool is up."
    c_green "════════════════════════════════════════════════════════════"
    echo
    echo "  Dashboard :  http://${ip}:${WEB_PORT}"
    echo "  Stratum   :  stratum+tcp://${ip}:${STRATUM_PORT}"
    echo
    echo "  Next steps:"
    echo "    1. Open the dashboard, set a BCH payout address (legacy or CashAddr)."
    echo "    2. Wait for the BCH node to sync (~250 GB, hours-to-days)."
    echo "    3. Point miners at the stratum URL."
    echo
    echo "  Useful commands:"
    echo "    sudo systemctl status ${SERVICE_NAME}"
    echo "    sudo docker logs -f ducky-bchnode"
    echo "    sudo docker logs -f ducky-ckpool"
    echo "    sudo docker logs -f ducky-web"
    echo "    sudo docker compose -f ${INSTALL_DIR}/docker-compose.yml down"
    echo
}

#────────────────────────────── main ──────────────────────────────#

require_root
detect_local_clone

step "Source mode: $SOURCE_MODE"
if [ "$SOURCE_MODE" = "remote" ]; then
    if [ "$BRANCH" = "main" ]; then
        ok "Channel: main (stable)"
    else
        c_red "  ⚠ Channel: $BRANCH (dev — may be unstable)"
    fi
fi

install_docker
ensure_dependencies
create_dirs
generate_secrets
fetch_app_files
record_install_sha
write_compose
render_ckpool_config
build_and_start
install_systemd
install_restart_watcher
print_summary
