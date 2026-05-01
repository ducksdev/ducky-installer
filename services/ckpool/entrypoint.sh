#!/usr/bin/env bash
set -euo pipefail

PAYOUT_FILE="${PAYOUT_ADDRESS_FILE:-/shared/payout.address}"
TEMPLATE="/etc/ckpool/ckpool.conf.template"
CONF="/etc/ckpool/ckpool.conf"

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
        # strip whitespace
        tr -d '[:space:]' < "$PAYOUT_FILE"
    else
        printf ''
    fi
}

start_ckpool() {
    local addr="$1"
    echo "[entrypoint] rendering ckpool config with payout=$addr ..."
    PAYOUT_ADDRESS="$addr" \
    BCH_RPC_HOST="$BCH_RPC_HOST" \
    BCH_RPC_PORT="$BCH_RPC_PORT" \
    BCH_RPC_USER="$BCH_RPC_USER" \
    BCH_RPC_PASS="$BCH_RPC_PASS" \
    POOL_SIG="${POOL_SIG:-/ducky-pool/}" \
    STRATUM_PORT="${STRATUM_PORT:-4567}" \
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

LAST_ADDR=""
while true; do
    ADDR="$(read_address)"

    if [ -z "$ADDR" ]; then
        if [ -n "$CKPOOL_PID" ] && kill -0 "$CKPOOL_PID" 2>/dev/null; then
            echo "[entrypoint] payout file empty; stopping ckpool until address is set."
            stop_ckpool
            LAST_ADDR=""
        fi
        sleep 10
        continue
    fi

    if [ "$ADDR" != "$LAST_ADDR" ]; then
        echo "[entrypoint] payout address changed: '$LAST_ADDR' -> '$ADDR'"
        stop_ckpool
        start_ckpool "$ADDR"
        LAST_ADDR="$ADDR"
    fi

    # if ckpool died unexpectedly, restart it
    if [ -n "$CKPOOL_PID" ] && ! kill -0 "$CKPOOL_PID" 2>/dev/null; then
        echo "[entrypoint] ckpool exited; restarting in 5s..."
        CKPOOL_PID=""
        sleep 5
        start_ckpool "$LAST_ADDR"
    fi

    sleep 5
done
