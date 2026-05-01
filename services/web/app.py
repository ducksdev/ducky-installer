"""
Status + config UI for Ducky Pool.

Endpoints:
  GET  /              full HTML dashboard (server-renders initial state)
  GET  /api/stats     JSON snapshot, polled every 10s by the dashboard
  POST /payout        save payout address (form)
  POST /workers/forget?name=<worker> remove a worker file
"""

from __future__ import annotations

import json
import os
import re
import socket
import time
from glob import glob
from urllib.parse import unquote

import requests
from cashaddress import convert as cashaddr_convert
from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    jsonify,
    abort,
)

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", os.urandom(16).hex())

BCH_RPC_HOST = os.environ.get("BCH_RPC_HOST", "bchnode")
BCH_RPC_PORT = int(os.environ.get("BCH_RPC_PORT", "8332"))
BCH_RPC_USER = os.environ.get("BCH_RPC_USER", "")
BCH_RPC_PASS = os.environ.get("BCH_RPC_PASS", "")
STRATUM_PORT = int(os.environ.get("STRATUM_PORT", "4567"))
PAYOUT_FILE = os.environ.get("PAYOUT_ADDRESS_FILE", "/shared/payout.address")
POOL_STATUS_FILE = os.environ.get("POOL_STATUS_FILE", "/pool-logs/pool/pool.status")
WORKERS_DIR = os.environ.get("WORKERS_DIR", "/pool-logs/workers")
FLASK_PORT = int(os.environ.get("FLASK_PORT", "4568"))

# Worker status thresholds
ONLINE_SECONDS = 2 * 60      # < 2 min => online
STALE_SECONDS = 10 * 60      # 2..10 min => stale; > 10 min => offline

LEGACY_BCH_RE = re.compile(r"^[13][a-km-zA-HJ-NP-Z1-9]{25,34}$")
CASHADDR_RE = re.compile(r"^(bitcoincash:)?[qp][a-z0-9]{40,}$", re.IGNORECASE)
# ckpool worker filenames: <btcaddress>.<workername>
# btcaddress is base58 (no dots), so the first dot splits cleanly.
WORKER_FILENAME_RE = re.compile(r"^[A-Za-z0-9]+\.[A-Za-z0-9_\-]+$")


# ──────────────────────────── helpers ────────────────────────────

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
    addr = raw.strip()
    if not addr:
        return None, "Address is empty."
    if LEGACY_BCH_RE.match(addr):
        return addr, None
    if CASHADDR_RE.match(addr):
        try:
            full = addr if addr.lower().startswith("bitcoincash:") else f"bitcoincash:{addr}"
            return cashaddr_convert.to_legacy_address(full), None
        except Exception as exc:  # noqa: BLE001
            return None, f"Could not convert CashAddr to legacy: {exc}"
    return None, "Not a BCH address. Expected legacy (1.../3...) or CashAddr (q.../p...)."


def humanise_age(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    s = int(seconds)
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def humanise_diff(n: float | int | None) -> str:
    """Format a difficulty / share value as 1.23T, 456.7G, 8.9M, 12.3K."""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "—"
    if n <= 0:
        return "0"
    for unit, threshold in (("P", 1e15), ("T", 1e12), ("G", 1e9), ("M", 1e6), ("K", 1e3)):
        if n >= threshold:
            return f"{n / threshold:.2f}{unit}"
    return f"{n:.2f}"


def worker_status(age_seconds: float | None) -> str:
    if age_seconds is None:
        return "offline"
    if age_seconds < ONLINE_SECONDS:
        return "online"
    if age_seconds < STALE_SECONDS:
        return "stale"
    return "offline"


# ──────────────────────────── readers ────────────────────────────

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


def _load_json_loose(raw: str) -> dict | None:
    """Parse a ckpool status file: either a single JSON object or
    JSON-per-line where the last line is the freshest."""
    raw = raw.strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        for ln in reversed(raw.splitlines()):
            ln = ln.strip()
            if not ln:
                continue
            try:
                return json.loads(ln)
            except json.JSONDecodeError:
                continue
    return None


def get_pool_stats() -> dict:
    try:
        with open(POOL_STATUS_FILE, "r", encoding="utf-8") as f:
            data = _load_json_loose(f.read())
        if data is None:
            return {"ok": False, "reason": "empty"}

        last_update = int(data.get("lastupdate", 0) or 0)
        age = int(time.time()) - last_update if last_update else None
        return {
            "ok": True,
            "stale": age is not None and age > 120,
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
            "best_share": humanise_diff(data.get("bestshare", 0)),
            "diff": data.get("diff", 0),
        }
    except FileNotFoundError:
        return {"ok": False, "reason": "no-file"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": "error", "error": str(exc)}


def get_workers() -> list[dict]:
    """Return a list of worker dicts sorted by status (online → stale →
    offline) and within each group by hashrate-ish (most recent share first)."""
    if not os.path.isdir(WORKERS_DIR):
        return []

    now = int(time.time())
    out: list[dict] = []
    for path in glob(os.path.join(WORKERS_DIR, "*")):
        fname = os.path.basename(path)
        if not WORKER_FILENAME_RE.match(fname):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = _load_json_loose(f.read())
        except OSError:
            continue
        if not data:
            continue

        last = int(data.get("lastshare", 0) or 0)
        age = (now - last) if last else None
        out.append({
            "id": fname,                              # full <addr>.<name>
            "name": fname.split(".", 1)[1] if "." in fname else fname,
            "status": worker_status(age),
            "age_s": age,
            "age_human": humanise_age(age),
            "hashrate_1m": data.get("hashrate1m", "—"),
            "hashrate_1hr": data.get("hashrate1hr", "—"),
            "hashrate_24hr": data.get("hashrate1d") or data.get("hashrate24hr") or "—",
            "shares": data.get("shares", 0),
            "best_share": humanise_diff(data.get("bestshare", 0) or data.get("bestever", 0)),
        })

    status_rank = {"online": 0, "stale": 1, "offline": 2}
    out.sort(key=lambda w: (status_rank.get(w["status"], 3), w["age_s"] if w["age_s"] is not None else 1e12))
    return out


# ──────────────────────────── routes ────────────────────────────

@app.route("/", methods=["GET"])
def index():
    status = get_node_status()
    pool = get_pool_stats()
    workers = get_workers()
    payout = read_payout()
    host = request.host.split(":")[0] or socket.gethostname()
    stratum_url = f"stratum+tcp://{host}:{STRATUM_PORT}"
    return render_template(
        "index.html",
        status=status,
        pool=pool,
        workers=workers,
        payout=payout,
        stratum_url=stratum_url,
        stratum_port=STRATUM_PORT,
    )


@app.route("/api/stats", methods=["GET"])
def api_stats():
    return jsonify({
        "node": get_node_status(),
        "pool": get_pool_stats(),
        "workers": get_workers(),
        "payout": read_payout(),
        "now": int(time.time()),
    })


@app.route("/payout", methods=["POST"])
def set_payout():
    raw = (request.form.get("address") or "").strip()
    legacy, err = normalise_address(raw)
    if err or not legacy:
        flash(err or "Invalid address.", "error")
        return redirect(url_for("index"))
    write_payout(legacy)
    if legacy != raw:
        flash(f"Saved. Converted CashAddr → legacy: {legacy}.", "ok")
    else:
        flash("Payout address saved.", "ok")
    return redirect(url_for("index"))


@app.route("/workers/forget", methods=["POST"])
def forget_worker():
    name = unquote((request.form.get("name") or request.args.get("name") or "").strip())
    # Strict whitelist — name must match the <addr>.<worker> shape.
    # No path traversal, no slashes, no leading dots.
    if not name or not WORKER_FILENAME_RE.match(name):
        abort(400, "invalid worker name")
    target = os.path.join(WORKERS_DIR, name)
    # realpath check ensures the resolved path stays inside WORKERS_DIR
    real_target = os.path.realpath(target)
    real_dir = os.path.realpath(WORKERS_DIR)
    if not real_target.startswith(real_dir + os.sep):
        abort(400, "invalid worker path")
    try:
        os.remove(real_target)
        flash(f"Forgot worker {name.split('.', 1)[-1]}.", "ok")
    except FileNotFoundError:
        flash("Worker already gone.", "ok")
    except OSError as exc:
        flash(f"Could not remove worker file: {exc}", "error")
    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=FLASK_PORT)
