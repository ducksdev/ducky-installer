"""
Status + config UI for ducky-bch (Ducky Pool).

- Reads BCHN sync status via JSON-RPC.
- Reads ckpool's pool.status JSON for live pool stats.
- Shows the stratum URL miners should connect to.
- Lets the user set a payout address. Accepts both legacy Base58 (1.../3...)
  and CashAddr (q.../p... or bitcoincash:q.../p...). CashAddr is converted
  to legacy server-side because ckpool only understands legacy.
"""

from __future__ import annotations

import json
import os
import re
import socket
import time

import requests
from cashaddress import convert as cashaddr_convert
from flask import Flask, render_template, request, redirect, url_for, flash

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", os.urandom(16).hex())

BCH_RPC_HOST = os.environ.get("BCH_RPC_HOST", "bchnode")
BCH_RPC_PORT = int(os.environ.get("BCH_RPC_PORT", "8332"))
BCH_RPC_USER = os.environ.get("BCH_RPC_USER", "")
BCH_RPC_PASS = os.environ.get("BCH_RPC_PASS", "")
STRATUM_PORT = int(os.environ.get("STRATUM_PORT", "4567"))
PAYOUT_FILE = os.environ.get("PAYOUT_ADDRESS_FILE", "/shared/payout.address")
POOL_STATUS_FILE = os.environ.get("POOL_STATUS_FILE", "/pool-logs/pool/pool.status")
FLASK_PORT = int(os.environ.get("FLASK_PORT", "4568"))

LEGACY_BCH_RE = re.compile(r"^[13][a-km-zA-HJ-NP-Z1-9]{25,34}$")
CASHADDR_RE = re.compile(r"^(bitcoincash:)?[qp][a-z0-9]{40,}$", re.IGNORECASE)


def rpc(method: str, params: list | None = None) -> dict:
    payload = {"jsonrpc": "1.0", "id": "web", "method": method, "params": params or []}
    r = requests.post(
        f"http://{BCH_RPC_HOST}:{BCH_RPC_PORT}/",
        json=payload,
        auth=(BCH_RPC_USER, BCH_RPC_PASS),
        timeout=5,
    )
    r.raise_for_status()
    return r.json()["result"]


def read_payout() -> str:
    try:
        with open(PAYOUT_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


def write_payout(addr: str) -> None:
    os.makedirs(os.path.dirname(PAYOUT_FILE), exist_ok=True)
    with open(PAYOUT_FILE, "w", encoding="utf-8") as f:
        f.write(addr.strip() + "\n")


def normalise_address(raw: str) -> tuple[str | None, str | None]:
    """Return (legacy_address, error). One is None.

    Accepts legacy Base58 unchanged. Converts CashAddr (with or without the
    bitcoincash: prefix) to legacy. Returns (None, msg) if the input is not
    a recognisable BCH address in either form.
    """
    addr = raw.strip()
    if not addr:
        return None, "Address is empty."

    if LEGACY_BCH_RE.match(addr):
        return addr, None

    if CASHADDR_RE.match(addr):
        try:
            full = addr if addr.lower().startswith("bitcoincash:") else f"bitcoincash:{addr}"
            legacy = cashaddr_convert.to_legacy_address(full)
            return legacy, None
        except Exception as exc:  # noqa: BLE001
            return None, f"Could not convert CashAddr to legacy: {exc}"

    return (
        None,
        "Not a BCH address. Expected legacy (1.../3...) or CashAddr (q.../p... or bitcoincash:q...).",
    )


def get_node_status() -> dict:
    try:
        info = rpc("getblockchaininfo")
        net = rpc("getnetworkinfo")
        return {
            "ok": True,
            "chain": info.get("chain"),
            "blocks": info.get("blocks"),
            "headers": info.get("headers"),
            "verification_progress": info.get("verificationprogress", 0.0),
            "ibd": info.get("initialblockdownload", True),
            "version": net.get("subversion"),
            "connections": net.get("connections"),
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def get_pool_stats() -> dict:
    """Read ckpool's pool.status.

    The file is overwritten each cycle and should parse as a single JSON
    object, but some forks emit JSON-per-line so we fall back to last
    non-empty line.
    """
    try:
        with open(POOL_STATUS_FILE, "r", encoding="utf-8") as f:
            raw = f.read().strip()
        if not raw:
            return {"ok": False, "reason": "empty"}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            last_line = next(
                (ln for ln in reversed(raw.splitlines()) if ln.strip()), ""
            )
            data = json.loads(last_line)

        last_update = data.get("lastupdate", 0)
        age = int(time.time()) - int(last_update) if last_update else None
        stale = age is not None and age > 120

        return {
            "ok": True,
            "stale": stale,
            "age_s": age,
            "users": data.get("Users", 0),
            "workers": data.get("Workers", 0),
            "idle": data.get("Idle", 0),
            "disconnected": data.get("Disconnected", 0),
            "hashrate_1m": data.get("hashrate1m", "—"),
            "hashrate_1hr": data.get("hashrate1hr", "—"),
            "hashrate_24hr": data.get("hashrate1d") or data.get("hashrate24hr") or "—",
            "accepted": data.get("accepted", 0),
            "rejected": data.get("rejected", 0),
            "best_share": data.get("bestshare", 0),
            "diff": data.get("diff", 0),
        }
    except FileNotFoundError:
        return {"ok": False, "reason": "no-file"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": "error", "error": str(exc)}


@app.route("/", methods=["GET"])
def index():
    status = get_node_status()
    pool = get_pool_stats()
    payout = read_payout()
    host = request.host.split(":")[0] or socket.gethostname()
    stratum_url = f"stratum+tcp://{host}:{STRATUM_PORT}"
    return render_template(
        "index.html",
        status=status,
        pool=pool,
        payout=payout,
        stratum_url=stratum_url,
        stratum_port=STRATUM_PORT,
    )


@app.route("/payout", methods=["POST"])
def set_payout():
    raw = (request.form.get("address") or "").strip()
    legacy, err = normalise_address(raw)
    if err or not legacy:
        flash(err or "Invalid address.", "error")
        return redirect(url_for("index"))
    write_payout(legacy)
    if legacy != raw:
        flash(
            f"Saved. Converted CashAddr → legacy: {legacy}. "
            "ckpool will restart within ~10s.",
            "ok",
        )
    else:
        flash("Payout address saved. ckpool will restart within ~10s.", "ok")
    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=FLASK_PORT)
