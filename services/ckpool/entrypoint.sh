#!/usr/bin/env bash
set -euo pipefail

PAYOUT_FILE="${PAYOUT_ADDRESS_FILE:-/shared/payout.address}"
SETTINGS_FILE="${SETTINGS_FILE:-/shared/settings.json}"
TEMPLATE="/etc/ckpool/ckpool.conf.template"
CONF="/etc/ckpool/ckpool.conf"

# Defaults that match app.py — keep these in sync.
DEFAULT_MINDIFF="1"
DEFAULT_MAXDIFF="0"     # 0 = unlimited
DEFAULT_STARTDIFF="42"

CKPOOL_PID=""

cleanup() {
    if [ -n "$CKPOOL_PID" ] && kill -0 "$CKPOOL_PID" 2>/dev/null; then
        echo "[entrypoint] stopping ckpool (pid $CKPOOL_PID)..."
        kill -TERM "$CKPOOL_PID" 2>/dev/null || true
        wait "$CKPOOL_PID" 2>/dev/null || true
    fi
    exit 0
}
trap cleanup TERM INT

wait_for_node() {
    echo "[entrypoint] waiting for BCH node RPC at ${BCH_RPC_HOST}:${BCH_RPC_PORT}..."
    until curl -sf --user "${BCH_RPC_USER}:${BCH_RPC_PASS}" \
        --data '{"jsonrpc":"1.0","id":"ck","method":"getblockchaininfo","params":[]}' \
        -H 'content-type: text/plain;' \
        "http://${BCH_RPC_HOST}:${BCH_RPC_PORT}/" >/dev/null 2>&1; do
        sleep 5
    done
    echo "[entrypoint] BCH node RPC is up."
}

read_address() {
    if [ -f "$PAYOUT_FILE" ]; then
        tr -d '[:space:]' < "$PAYOUT_FILE"
    else
        printf ''
    fi
}

# Reads one difficulty value from settings.json; falls back to default.
read_diff() {
    local key="$1"
    local default="$2"
    if [ -f "$SETTINGS_FILE" ]; then
        local val
        val="$(jq -r --arg k "$key" '
            if has($k) and (.[$k] != null) then (.[$k] | tostring) else "" end
        ' "$SETTINGS_FILE" 2>/dev/null || true)"
        if [ -n "$val" ]; then
            printf '%s' "$val"
            return
        fi
    fi
    printf '%s' "$default"
}

# Hash of just the bits ckpool cares about — payout address + difficulties.
# When this changes we restart ckpool. Discord settings don't appear here so
# editing them never kicks miners.
ckpool_settings_hash() {
    printf '%s|%s|%s|%s' \
        "$(read_address)" \
        "$(read_diff mindiff "$DEFAULT_MINDIFF")" \
        "$(read_diff maxdiff "$DEFAULT_MAXDIFF")" \
        "$(read_diff startdiff "$DEFAULT_STARTDIFF")" \
        | sha256sum | awk '{print $1}'
}

start_ckpool() {
    local addr="$1"
    local mindiff maxdiff startdiff
    mindiff="$(read_diff mindiff "$DEFAULT_MINDIFF")"
    maxdiff="$(read_diff maxdiff "$DEFAULT_MAXDIFF")"
    startdiff="$(read_diff startdiff "$DEFAULT_STARTDIFF")"

    echo "[entrypoint] rendering ckpool config:"
    echo "             payout=$addr  sig=${POOL_SIG:-/ducky-pool/}"
    echo "             mindiff=$mindiff  maxdiff=$maxdiff  startdiff=$startdiff"

    PAYOUT_ADDRESS="$addr" \
    BCH_RPC_HOST="$BCH_RPC_HOST" \
    BCH_RPC_PORT="$BCH_RPC_PORT" \
    BCH_RPC_USER="$BCH_RPC_USER" \
    BCH_RPC_PASS="$BCH_RPC_PASS" \
    POOL_SIG="${POOL_SIG:-/ducky-pool/}" \
    STRATUM_PORT="${STRATUM_PORT:-4567}" \
    MINDIFF="$mindiff" \
    MAXDIFF="$maxdiff" \
    STARTDIFF="$startdiff" \
    envsubst < "$TEMPLATE" > "$CONF"

    echo "[entrypoint] starting ckpool..."
    /usr/local/bin/ckpool -c "$CONF" &
    CKPOOL_PID=$!
    echo "[entrypoint] ckpool pid=$CKPOOL_PID"
}

stop_ckpool() {
    if [ -n "$CKPOOL_PID" ] && kill -0 "$CKPOOL_PID" 2>/dev/null; then
        echo "[entrypoint] stopping ckpool (pid $CKPOOL_PID) for restart..."
        kill -TERM "$CKPOOL_PID" 2>/dev/null || true
        wait "$CKPOOL_PID" 2>/dev/null || true
    fi
    CKPOOL_PID=""
}

wait_for_node

LAST_HASH=""
while true; do
    ADDR="$(read_address)"

    if [ -z "$ADDR" ]; then
        if [ -n "$CKPOOL_PID" ] && kill -0 "$CKPOOL_PID" 2>/dev/null; then
            echo "[entrypoint] payout empty; stopping ckpool until address is set."
            stop_ckpool
            LAST_HASH=""
        fi
        sleep 10
        continue
    fi

    CUR_HASH="$(ckpool_settings_hash)"

    if [ "$CUR_HASH" != "$LAST_HASH" ]; then
        echo "[entrypoint] config change detected (hash $LAST_HASH -> $CUR_HASH)"
        stop_ckpool
        start_ckpool "$ADDR"
        LAST_HASH="$CUR_HASH"
    fi

    if [ -n "$CKPOOL_PID" ] && ! kill -0 "$CKPOOL_PID" 2>/dev/null; then
        echo "[entrypoint] ckpool exited; restarting in 5s..."
        CKPOOL_PID=""
        sleep 5
        start_ckpool "$ADDR"
    fi

    sleep 5
done
