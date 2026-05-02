"""
Status + config UI for Ducky Pool.

Endpoints:
  GET  /                  dashboard
  GET  /api/stats         JSON snapshot for the dashboard's poll
  POST /payout            save payout address (form)
  POST /workers/forget    remove a worker file (form)
  POST /best/reset        reset best-share baseline (form)
  GET  /settings          settings page (difficulty + Discord)
  POST /settings/save     save settings (form)
  POST /webhook/test      send a test Discord webhook
"""

from __future__ import annotations

import json
import os
import re
import socket
import sqlite3
import threading
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
WORKERS_DIR = os.environ.get("WORKERS_DIR", "/pool-logs/users")
FLASK_PORT = int(os.environ.get("FLASK_PORT", "4568"))

STATE_DIR = os.environ.get("STATE_DIR", "/shared")
SETTINGS_FILE = os.path.join(STATE_DIR, "settings.json")
BASELINE_FILE = os.path.join(STATE_DIR, "best_baselines.json")
SEEN_BLOCKS_FILE = os.path.join(STATE_DIR, "seen_blocks.json")
HISTORY_DB = os.path.join(STATE_DIR, "history.db")
# When this file appears, ckpool's entrypoint stops ckpool, removes the
# marker, and starts ckpool fresh on the next loop iteration. We use this
# to clear ckpool's in-memory bestshare cache on "Wipe all stats".
RESTART_MARKER = os.path.join(STATE_DIR, ".restart_ckpool")

# ckpool writes one line per found block here.
BLOCKS_LOG = os.environ.get("BLOCKS_LOG", "/pool-logs/pool/blocks")

# History retention — older rows are pruned on write. 30 days at 60s
# resolution is ~43k rows, well under 5 MB.
HISTORY_RETENTION_DAYS = 30
HISTORY_SAMPLE_INTERVAL = 60   # seconds between recorded snapshots

# Defaults — these are also encoded in ckpool's entrypoint.sh and must
# match it. Don't drift the two.
DEFAULT_SETTINGS = {
    "mindiff": 1,
    "maxdiff": 0,        # 0 = unlimited
    "startdiff": 42,
    "discord": {
        "webhook_url": "",
        "username": "Ducky Pool",
        "avatar_url": "",
    },
}

ONLINE_SECONDS = 2 * 60
STALE_SECONDS = 10 * 60

WATCHER_INTERVAL = int(os.environ.get("WATCHER_INTERVAL", "15"))
WEBHOOK_MIN_INTERVAL = int(os.environ.get("WEBHOOK_MIN_INTERVAL", "60"))
WEBHOOK_TIMEOUT = 10

LEGACY_BCH_RE = re.compile(r"^[13][a-km-zA-HJ-NP-Z1-9]{25,34}$")
CASHADDR_RE = re.compile(r"^(bitcoincash:)?[qp][a-z0-9]{40,}$", re.IGNORECASE)
# File on disk in /users is just the Base58 BCH address.
USER_FILENAME_RE = re.compile(r"^[a-km-zA-HJ-NP-Z1-9]{25,40}$")
# Synthetic worker ID used in dashboard URLs and baseline keys: <address>.<workername>.
# Keep this in sync with how ckpool emits "workername" in the user file's `worker` array.
WORKER_ID_RE = re.compile(r"^[a-km-zA-HJ-NP-Z1-9]{25,40}\.[A-Za-z0-9_\-]+$")
# Backwards-compat alias so existing call sites still work.
WORKER_FILENAME_RE = WORKER_ID_RE

DIFF_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([KMGTP]?)\s*$", re.IGNORECASE)
DIFF_MULTIPLIER = {"": 1, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "P": 1e15}

DISCORD_WEBHOOK_PREFIXES = (
    "https://discord.com/api/webhooks/",
    "https://discordapp.com/api/webhooks/",
    "https://canary.discord.com/api/webhooks/",
    "https://ptb.discord.com/api/webhooks/",
)

_state_lock = threading.Lock()


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


def parse_diff(s: str) -> tuple[int | None, str | None]:
    """Parse '1', '1K', '100M', '1.5G' to an int. Returns (value, error)."""
    if s is None:
        return None, "missing"
    m = DIFF_RE.match(str(s))
    if not m:
        return None, f"could not parse '{s}' (try 1, 1000, 1K, 100M, 1G)"
    val = float(m.group(1)) * DIFF_MULTIPLIER[m.group(2).upper()]
    if val < 0:
        return None, "must be ≥ 0"
    return int(val), None


def worker_status(age_seconds: float | None) -> str:
    if age_seconds is None:
        return "offline"
    if age_seconds < ONLINE_SECONDS:
        return "online"
    if age_seconds < STALE_SECONDS:
        return "stale"
    return "offline"


def _load_json_loose(raw: str) -> dict | None:
    """Parse a single JSON object, falling back to the last valid JSON
    line if the whole text isn't a valid object (handles trailing junk)."""
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


def _load_json_merged(raw: str) -> dict | None:
    """ckpool writes pool.status as multiple JSON objects, one per line:
        {"runtime": ..., "Users": ..., "Workers": ...}
        {"hashrate1m": "...", "hashrate1hr": "...", ...}
        {"diff": ..., "accepted": ..., "bestshare": ...}
    Parse every valid line and merge them into one dict (later lines win
    on key conflict, but the schemas don't overlap so it doesn't matter)."""
    raw = raw.strip()
    if not raw:
        return None
    merged: dict = {}
    any_ok = False
    for ln in raw.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            obj = json.loads(ln)
            if isinstance(obj, dict):
                merged.update(obj)
                any_ok = True
        except json.JSONDecodeError:
            continue
    return merged if any_ok else None


def humanise_hashrate(s: str | float | int | None) -> str:
    """Normalise ckpool's hashrate strings ('33T', '2.98G') to '33.00 TH/s'.
    Pass-through for already-normalised strings. Returns '—' if unparseable."""
    if s is None or s == "—":
        return "—"
    if isinstance(s, (int, float)):
        if s <= 0:
            return "—"
        units = [("EH/s", 1e18), ("PH/s", 1e15), ("TH/s", 1e12),
                 ("GH/s", 1e9), ("MH/s", 1e6), ("KH/s", 1e3)]
        for unit, threshold in units:
            if s >= threshold:
                return f"{s / threshold:.2f} {unit}"
        return f"{s:.0f} H/s"
    text = str(s).strip()
    if not text or text == "0":
        return "0 H/s"
    suffix_map = {"K": "KH/s", "M": "MH/s", "G": "GH/s", "T": "TH/s",
                  "P": "PH/s", "E": "EH/s"}
    if text and text[-1].upper() in suffix_map:
        unit = suffix_map[text[-1].upper()]
        try:
            value = float(text[:-1])
            return f"{value:.2f} {unit}"
        except ValueError:
            return text
    try:
        return humanise_hashrate(float(text))
    except ValueError:
        return text


# ──────────────────────────── settings ────────────────────────────

def _atomic_write_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def _merge_defaults(loaded: dict) -> dict:
    out = dict(DEFAULT_SETTINGS)
    out.update({k: v for k, v in loaded.items() if k != "discord"})
    discord = dict(DEFAULT_SETTINGS["discord"])
    discord.update(loaded.get("discord") or {})
    out["discord"] = discord
    return out


def load_settings() -> dict:
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return dict(DEFAULT_SETTINGS)
        return _merge_defaults(data)
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(DEFAULT_SETTINGS)


def save_settings(new: dict) -> None:
    with _state_lock:
        merged = _merge_defaults(new)
        _atomic_write_json(SETTINGS_FILE, merged)
        try:
            os.chmod(SETTINGS_FILE, 0o600)
        except OSError:
            pass


def _load_baselines() -> dict:
    """State file schema:
        {
          "workers": { "<addr>.<name>": {best, last_sent_ts, reset_snapshot} },
          "hidden":  { "<addr>.<name>": <unix_ts_when_hidden> }
        }
    The 'hidden' dict is populated by the Forget action. get_workers()
    filters out workers in this dict UNLESS their lastshare is more
    recent than the hidden_at timestamp (i.e. they came back online),
    in which case they're auto-unhidden."""
    try:
        with open(BASELINE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("workers"), dict):
            # Backfill hidden if the file predates this field.
            if not isinstance(data.get("hidden"), dict):
                data["hidden"] = {}
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return {"workers": {}, "hidden": {}}


def _save_baselines(data: dict) -> None:
    _atomic_write_json(BASELINE_FILE, data)


def _read_worker_current_best(worker_id: str) -> float:
    """Find a worker's current bestshare by reading its parent user file
    and locating the matching entry in the `worker` array. Returns 0 if
    not found (e.g. worker hasn't reported yet)."""
    if "." not in worker_id:
        return 0.0
    address = worker_id.split(".", 1)[0]
    path = os.path.join(WORKERS_DIR, address)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = _load_json_loose(f.read()) or {}
    except OSError:
        return 0.0
    for w in (data.get("worker") or []):
        if isinstance(w, dict) and w.get("workername") == worker_id:
            try:
                return float(w.get("bestshare", 0) or w.get("bestever", 0) or 0)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def _discover_workers_from_disk() -> list[str]:
    """Walk every user file and return all workernames found in their
    `worker` arrays. Used so a 'Reset all bests' picks up workers we
    might not have baselines for yet (first run, etc.)."""
    out: list[str] = []
    if not os.path.isdir(WORKERS_DIR):
        return out
    for path in glob(os.path.join(WORKERS_DIR, "*")):
        fname = os.path.basename(path)
        if not USER_FILENAME_RE.match(fname):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = _load_json_loose(f.read()) or {}
        except OSError:
            continue
        for w in (data.get("worker") or []):
            if isinstance(w, dict):
                wname = w.get("workername")
                if wname and WORKER_ID_RE.match(wname):
                    out.append(wname)
    return out


def reset_baseline(worker_id: str | None) -> int:
    """Soft reset for a worker (or all workers when worker_id is None /
    '__pool__'). Records ckpool's CURRENT bestshare as the
    `reset_snapshot` so the dashboard hides the Best column until ckpool
    reports a new value above the snapshot. Also resets the Discord
    webhook baseline so a fresh ATH can fire a notification.

    Does NOT restart ckpool or affect the miner connection. The hard
    pool-wide reset (which DOES restart ckpool) is in /stats/reset."""
    with _state_lock:
        data = _load_baselines()
        workers = data["workers"]

        if worker_id in (None, "__pool__"):
            targets = list(set(workers.keys()) | set(_discover_workers_from_disk()))
        else:
            targets = [worker_id]

        for wid in targets:
            current = _read_worker_current_best(wid)
            if current <= 0:
                # Worker hasn't reported a bestshare yet — fall back to
                # whatever baseline we have so we don't spuriously fire
                # a webhook when the first share arrives.
                current = float(workers.get(wid, {}).get("best", 0))
            workers[wid] = {
                "best": current,
                "last_sent_ts": 0,
                "reset_snapshot": current,  # hide Best until we beat this
            }

        _save_baselines(data)
        return len(targets)


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


def get_pool_stats() -> dict:
    try:
        with open(POOL_STATUS_FILE, "r", encoding="utf-8") as f:
            data = _load_json_merged(f.read())
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
            "hashrate_1m": humanise_hashrate(data.get("hashrate1m")),
            "hashrate_1hr": humanise_hashrate(data.get("hashrate1hr")),
            "hashrate_24hr": humanise_hashrate(
                data.get("hashrate1d") or data.get("hashrate24hr")
            ),
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
    """Walk /users/* (one file per BCH address), expand each file's
    nested `worker` array into one dashboard row per worker.

    Per-worker reset is implemented as a "soft" reset — when the user
    clicks Reset on a worker row, we record ckpool's current bestshare
    for that worker as a `reset_snapshot` in best_baselines.json. The
    displayed Best is then 0 until ckpool reports a new value above the
    snapshot. This avoids restarting ckpool (no miner blip).
    """
    if not os.path.isdir(WORKERS_DIR):
        return []
    now = int(time.time())
    out: list[dict] = []

    # Load baselines once per call so we can apply the reset_snapshot
    # filter and the hidden-workers filter without hammering disk.
    with _state_lock:
        state = _load_baselines()
        baselines = state.get("workers", {})
        hidden = dict(state.get("hidden", {}))  # copy — we may auto-unhide

    auto_unhidden: list[str] = []  # collected so we can persist after the loop

    for path in glob(os.path.join(WORKERS_DIR, "*")):
        fname = os.path.basename(path)
        if not USER_FILENAME_RE.match(fname):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = _load_json_loose(f.read())
        except OSError:
            continue
        if not data:
            continue

        worker_list = data.get("worker") or []
        if not isinstance(worker_list, list):
            continue

        for w in worker_list:
            if not isinstance(w, dict):
                continue
            workername = w.get("workername", "")
            if not workername or not WORKER_ID_RE.match(workername):
                continue
            display = workername.split(".", 1)[1] if "." in workername else workername

            last = int(w.get("lastshare", 0) or 0)
            age = (now - last) if last else None

            # Hidden filter: skip workers the user has Hidden, UNLESS
            # they've submitted at least one new share since being hidden.
            # We compare ckpool's current shares count against the count
            # captured at hide-time. This works regardless of whether
            # the worker was online, stale, or offline when hidden:
            # the only signal that matters is "did they submit a NEW
            # share after the user clicked Hide?"
            hidden_meta = hidden.get(workername)
            if hidden_meta is not None:
                shares_at_hide = (
                    hidden_meta.get("shares")
                    if isinstance(hidden_meta, dict)
                    else None
                )
                hidden_at_ts = (
                    hidden_meta.get("ts")
                    if isinstance(hidden_meta, dict)
                    else hidden_meta  # legacy: scalar ts
                )
                current_shares = int(w.get("shares", 0) or 0)
                # Two unhide signals (either is enough):
                #   1. shares_count grew since hide  (most reliable)
                #   2. lastshare timestamp is newer than hide_at_ts
                #      (handles legacy entries that don't have shares_at_hide)
                came_back = False
                if shares_at_hide is not None and current_shares > shares_at_hide:
                    came_back = True
                elif hidden_at_ts is not None and last > hidden_at_ts:
                    came_back = True
                if came_back:
                    auto_unhidden.append(workername)
                else:
                    continue

            try:
                ckpool_best = float(w.get("bestshare", 0) or w.get("bestever", 0) or 0)
            except (TypeError, ValueError):
                ckpool_best = 0.0

            # Apply soft-reset: if the worker has a baseline with a
            # reset_snapshot, only show ckpool's bestshare if it's now
            # greater than the snapshot. Otherwise the displayed Best
            # is 0 — the user's reset hasn't been "beaten" yet.
            entry = baselines.get(workername) or {}
            snap = float(entry.get("reset_snapshot", 0) or 0)
            displayed_best = ckpool_best if ckpool_best > snap else 0.0

            out.append({
                "id": workername,
                "name": display,
                "status": worker_status(age),
                "age_s": age,
                "age_human": humanise_age(age),
                "hashrate_1m": humanise_hashrate(w.get("hashrate1m")),
                "hashrate_1hr": humanise_hashrate(w.get("hashrate1hr")),
                "hashrate_24hr": humanise_hashrate(
                    w.get("hashrate1d") or w.get("hashrate24hr")
                ),
                "shares": w.get("shares", 0),
                "best_share": humanise_diff(displayed_best) if displayed_best > 0 else "—",
            })

    # Persist any auto-unhidden workers.
    if auto_unhidden:
        with _state_lock:
            state = _load_baselines()
            for wid in auto_unhidden:
                state.get("hidden", {}).pop(wid, None)
            _save_baselines(state)

    rank = {"online": 0, "stale": 1, "offline": 2}
    out.sort(key=lambda w: (rank.get(w["status"], 3), w["age_s"] if w["age_s"] is not None else 1e12))
    return out


# ──────────────────────────── blocks ────────────────────────────

# ckpool's blocks file is one line per found block. Format varies a bit by
# fork — common layout is space-separated:
#   <unix_ts> <height> <hash> <diff> <reward_satoshis> <username>
# We parse defensively.
def parse_blocks_log() -> list[dict]:
    if not os.path.exists(BLOCKS_LOG):
        return []
    out: list[dict] = []
    try:
        with open(BLOCKS_LOG, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                parts = line.split()
                # Try to extract what we can; skip lines we can't make sense of.
                rec = {"raw": line}
                # Field 0 is usually unix timestamp
                try:
                    rec["ts"] = int(float(parts[0]))
                except (ValueError, IndexError):
                    rec["ts"] = None
                # Find anything that looks like a 64-char hex hash
                for p in parts:
                    if len(p) == 64 and all(c in "0123456789abcdefABCDEF" for c in p):
                        rec["hash"] = p.lower()
                        break
                # Find anything that looks like a height (small integer between 100k and 100m)
                for p in parts:
                    try:
                        n = int(p)
                        if 100_000 < n < 100_000_000 and "height" not in rec:
                            rec["height"] = n
                            break
                    except ValueError:
                        continue
                # Find a username/worker (usually the last part if not numeric)
                if parts and not parts[-1].replace(".", "", 1).isdigit():
                    rec["worker"] = parts[-1]
                out.append(rec)
    except OSError:
        return []
    return out


def get_blocks_summary() -> dict:
    blocks = parse_blocks_log()
    latest = blocks[-1] if blocks else None
    return {
        "count": len(blocks),
        "latest": latest,
    }


# ──────────────────────────── history ────────────────────────────

_db_lock = threading.Lock()


def _hashrate_str_to_float(s: str | float | int | None) -> float | None:
    """Parse a hashrate string back to a float in H/s. Handles both the
    raw ckpool format ('1.74T') and the humanised format from
    humanise_hashrate ('1.74 TH/s'). Returns None if unparseable."""
    if s is None or s == "—":
        return None
    if isinstance(s, (int, float)):
        return float(s)
    s = str(s).strip()
    if not s or s == "0":
        return 0.0
    # Strip the unit-rate suffix (case-insensitive). After this, '1.74 TH/s'
    # becomes '1.74 T', '500 GH/s' becomes '500 G', and raw '1.74T' is
    # untouched.
    upper = s.upper()
    for tail in ("H/S", "H/s", "HZ"):
        if upper.endswith(tail.upper()):
            s = s[: -len(tail)].rstrip()
            upper = s.upper()
            break
    if not s:
        return None
    units = {"K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "P": 1e15, "E": 1e18}
    suffix = s[-1].upper()
    if suffix in units:
        try:
            return float(s[:-1].strip()) * units[suffix]
        except ValueError:
            return None
    try:
        return float(s)
    except ValueError:
        return None


def _open_db() -> sqlite3.Connection:
    os.makedirs(STATE_DIR, exist_ok=True)
    conn = sqlite3.connect(HISTORY_DB, timeout=5)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hashrate (
            ts INTEGER PRIMARY KEY,
            hashrate_1m REAL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_hashrate_ts ON hashrate(ts)")
    return conn


def record_history_sample(now_ts: int) -> None:
    """Record a snapshot of pool hashrate. Called from the watcher thread."""
    pool = get_pool_stats()
    if not pool.get("ok"):
        return
    rate = _hashrate_str_to_float(pool.get("hashrate_1m"))
    if rate is None:
        return

    with _db_lock:
        conn = _open_db()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO hashrate(ts, hashrate_1m) VALUES (?, ?)",
                (now_ts, rate),
            )
            # Prune anything older than retention window
            cutoff = now_ts - HISTORY_RETENTION_DAYS * 86400
            conn.execute("DELETE FROM hashrate WHERE ts < ?", (cutoff,))
            conn.commit()
        finally:
            conn.close()


def read_history(window_seconds: int) -> list[tuple[int, float]]:
    """Returns (timestamp, hashrate) pairs covering the last `window_seconds`."""
    since = int(time.time()) - window_seconds
    with _db_lock:
        conn = _open_db()
        try:
            rows = conn.execute(
                "SELECT ts, hashrate_1m FROM hashrate WHERE ts >= ? ORDER BY ts",
                (since,),
            ).fetchall()
        finally:
            conn.close()
    return [(int(r[0]), float(r[1])) for r in rows]


# ──────────────────────────── ETA ────────────────────────────

def block_eta(pool_hashrate_hs: float | None, net_diff: float | None) -> dict:
    """Given the pool's current hashrate (in H/s) and network difficulty,
    estimate the expected time to find a block. Returns a dict with both a
    seconds value and a human-readable form."""
    if not pool_hashrate_hs or pool_hashrate_hs <= 0 or not net_diff or net_diff <= 0:
        return {"ok": False}
    # Standard formula: expected seconds = diff * 2^32 / hashrate
    seconds = net_diff * (2 ** 32) / pool_hashrate_hs
    blocks_per_year = (365 * 86400) / seconds if seconds > 0 else 0.0

    if seconds < 60:
        human = f"~{seconds:.0f}s"
    elif seconds < 3600:
        human = f"~{seconds / 60:.0f}m"
    elif seconds < 86400:
        human = f"~{seconds / 3600:.1f}h"
    elif seconds < 86400 * 365:
        human = f"~{seconds / 86400:.1f}d"
    else:
        human = f"~{seconds / 86400 / 365:.1f}y"

    return {
        "ok": True,
        "seconds": int(seconds),
        "human": human,
        "blocks_per_year": blocks_per_year,
        "per_year_human": (
            f"{blocks_per_year:.2f}/yr" if blocks_per_year >= 0.01 else f"{blocks_per_year:.4f}/yr"
        ),
    }


# ──────────────────────────── webhook ────────────────────────────

# Cache the BCH network difficulty so we don't hit RPC for every webhook tick.
# 30s TTL is plenty: BCH retargets per-block and the difficulty barely moves.
_diff_cache: dict = {"value": None, "fetched_at": 0}
_DIFF_CACHE_TTL = 30


def get_network_difficulty() -> float | None:
    """Returns the current BCH network difficulty, or None if RPC fails.
    Cached for ~30 seconds."""
    now = int(time.time())
    if _diff_cache["value"] is not None and (now - _diff_cache["fetched_at"]) < _DIFF_CACHE_TTL:
        return _diff_cache["value"]
    try:
        info = rpc("getmininginfo")
        diff = float(info.get("difficulty", 0))
        if diff > 0:
            _diff_cache["value"] = diff
            _diff_cache["fetched_at"] = now
            return diff
    except Exception:  # noqa: BLE001
        pass
    return _diff_cache["value"]   # may still be a stale value, better than None


def progress_bar(current: float, target: float, width: int = 14) -> str:
    """Render a Unicode block-meter showing progress towards `target`.
    Filled cells use ▰, empty use ▱. Capped at 100%."""
    if target <= 0:
        return "▱" * width
    pct = max(0.0, min(1.0, current / target))
    filled = int(round(pct * width))
    return "▰" * filled + "▱" * (width - filled)


def _post_discord(content: str | None, embeds: list[dict] | None = None) -> tuple[bool, str]:
    s = load_settings()
    url = s["discord"]["webhook_url"]
    if not url:
        return False, "No webhook URL configured."
    payload: dict = {"username": s["discord"]["username"] or "Ducky Pool"}
    if s["discord"]["avatar_url"]:
        payload["avatar_url"] = s["discord"]["avatar_url"]
    if content:
        payload["content"] = content
    if embeds:
        payload["embeds"] = embeds
    try:
        r = requests.post(url, json=payload, timeout=WEBHOOK_TIMEOUT)
        if r.status_code in (200, 204):
            return True, "ok"
        return False, f"Discord returned {r.status_code}: {r.text[:200]}"
    except requests.RequestException as exc:
        return False, f"Network error: {exc}"


def _build_best_share_embed(
    worker_name: str,
    current: float,
    net_diff: float | None,
    ts: int,
) -> dict:
    """Build the rich AxeBCH-style "new best share" embed."""
    # Bitcoin Cash logo. crypto-logo.com is a CDN built for embedding,
    # serves 128x128 transparent PNG which is the ideal Discord thumbnail size.
    BCH_LOGO = "https://cdn.crypto-logo.com/logos/bitcoin-cash-bch/128x128/transparent.png"

    fields = [
        {"name": "🎯 Worker",     "value": f"**{worker_name}**", "inline": True},
        {"name": "💎 Best Share", "value": humanise_diff(current), "inline": True},
    ]
    description_lines = [f"**{worker_name}** just hit a new best share!"]

    if net_diff and net_diff > 0:
        pct = (current / net_diff) * 100
        bar = progress_bar(current, net_diff)
        fields.append({
            "name": "📈 Block Diff",
            "value": humanise_diff(net_diff),
            "inline": True,
        })
        description_lines.append("")
        description_lines.append("📊 **Progress to Block**")
        description_lines.append(f"`{bar}`  **{pct:.2f}%**")
    else:
        # No network diff available — still show a friendly note instead of
        # a broken progress bar.
        fields.append({
            "name": "📈 Block Diff",
            "value": "—",
            "inline": True,
        })

    return {
        "title": "🦆 New best share! (BCH)",
        "description": "\n".join(description_lines),
        "color": 0xF9A825,
        "fields": fields,
        "thumbnail": {"url": BCH_LOGO},
        "footer": {"text": "Ducky Pool · Solo BCH"},
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)),
    }


def _check_and_fire(now_ts: int) -> None:
    s = load_settings()
    if not s["discord"]["webhook_url"] or not os.path.isdir(WORKERS_DIR):
        return

    with _state_lock:
        state = _load_baselines()
        workers = state["workers"]
        hidden = state.get("hidden", {})
        changed = False

        for path in glob(os.path.join(WORKERS_DIR, "*")):
            fname = os.path.basename(path)
            if not USER_FILENAME_RE.match(fname):
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    user_data = _load_json_loose(f.read()) or {}
            except OSError:
                continue

            worker_list = user_data.get("worker") or []
            if not isinstance(worker_list, list):
                continue

            for w in worker_list:
                if not isinstance(w, dict):
                    continue
                workername = w.get("workername", "")
                if not workername or not WORKER_ID_RE.match(workername):
                    continue

                # Skip hidden workers UNLESS they've come back online
                # since being hidden. Same logic as get_workers (shares
                # count growth, or lastshare past hide-ts as a fallback).
                hidden_meta = hidden.get(workername)
                if hidden_meta is not None:
                    shares_at_hide = (
                        hidden_meta.get("shares")
                        if isinstance(hidden_meta, dict)
                        else None
                    )
                    hidden_at_ts = (
                        hidden_meta.get("ts")
                        if isinstance(hidden_meta, dict)
                        else hidden_meta
                    )
                    cur_shares = int(w.get("shares", 0) or 0)
                    last = int(w.get("lastshare", 0) or 0)
                    came_back = False
                    if shares_at_hide is not None and cur_shares > shares_at_hide:
                        came_back = True
                    elif hidden_at_ts is not None and last > hidden_at_ts:
                        came_back = True
                    if not came_back:
                        continue

                try:
                    current = float(w.get("bestshare", 0) or w.get("bestever", 0) or 0)
                except (TypeError, ValueError):
                    continue
                if current <= 0:
                    continue

                entry = workers.get(workername)
                if entry is None:
                    # First-sight rule: record current best as baseline silently,
                    # so we don't fire on the historical best the moment we start watching.
                    workers[workername] = {"best": current, "last_sent_ts": 0}
                    changed = True
                    continue

                baseline = float(entry.get("best", 0))
                last_sent = int(entry.get("last_sent_ts", 0))

                if current > baseline and (now_ts - last_sent) >= WEBHOOK_MIN_INTERVAL:
                    display = workername.split(".", 1)[1] if "." in workername else workername
                    net_diff = get_network_difficulty()
                    ok, _msg = _post_discord(
                        content=None,
                        embeds=[_build_best_share_embed(
                            worker_name=display,
                            current=current,
                            net_diff=net_diff,
                            ts=now_ts,
                        )],
                    )
                    workers[workername] = {
                        "best": current,
                        "last_sent_ts": now_ts if ok else last_sent,
                    }
                    changed = True
                elif current > baseline:
                    workers[workername] = {"best": current, "last_sent_ts": last_sent}
                    changed = True

        if changed:
            _save_baselines(state)


def _load_seen_blocks() -> set:
    try:
        with open(SEEN_BLOCKS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("hashes"), list):
            return set(data["hashes"])
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return set()


def _save_seen_blocks(seen: set) -> None:
    _atomic_write_json(SEEN_BLOCKS_FILE, {"hashes": sorted(seen)})


def _check_blocks_and_fire(now_ts: int) -> None:
    """Walk the ckpool blocks log. For any block we haven't sent yet, fire
    a celebratory webhook and record the hash so we don't replay."""
    s = load_settings()
    if not s["discord"]["webhook_url"]:
        return

    blocks = parse_blocks_log()
    if not blocks:
        return

    with _state_lock:
        seen = _load_seen_blocks()
        first_run = not seen
        new_blocks = []
        for b in blocks:
            h = b.get("hash")
            if not h:
                continue
            if h not in seen:
                new_blocks.append(b)
                seen.add(h)

        if not new_blocks:
            return

        # First-sight rule: on the very first run after install, record
        # everything silently rather than firing webhooks for historical
        # blocks (e.g. after a backup-restore).
        if first_run:
            _save_seen_blocks(seen)
            return

        for b in new_blocks:
            ok, _msg = _post_discord(
                content=None,
                embeds=[_build_block_found_embed(b, now_ts)],
            )
            if not ok:
                # Roll this hash out so we retry next tick.
                seen.discard(b.get("hash"))

        _save_seen_blocks(seen)


def _build_block_found_embed(block: dict, ts: int) -> dict:
    """Big celebratory embed for a found block."""
    BCH_LOGO = "https://cdn.crypto-logo.com/logos/bitcoin-cash-bch/128x128/transparent.png"
    EXPLORER = "https://blockchair.com/bitcoin-cash/block/{hash}"

    height = block.get("height")
    h = block.get("hash")
    worker = block.get("worker")

    fields = []
    if height is not None:
        fields.append({"name": "📦 Height", "value": f"`{height:,}`", "inline": True})
    if worker:
        fields.append({"name": "⛏️ Found by", "value": f"`{worker}`", "inline": True})
    if h:
        fields.append({
            "name": "🔗 Hash",
            "value": f"[`{h[:8]}…{h[-8:]}`]({EXPLORER.format(hash=h)})",
            "inline": False,
        })

    description = "**🎉 BLOCK FOUND! 🎉**\n\nDucky Pool just solved a Bitcoin Cash block."
    if not fields:
        description += "\n\n*ckpool didn't include parseable block details — check the log.*"

    return {
        "title": "🟢 BLOCK FOUND (BCH)",
        "description": description,
        "color": 0x4CD964,   # green for celebration
        "fields": fields,
        "thumbnail": {"url": BCH_LOGO},
        "footer": {"text": "Ducky Pool · Solo BCH"},
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)),
    }


_last_history_ts = 0


def _watcher_loop() -> None:
    global _last_history_ts
    while True:
        now = int(time.time())
        try:
            _check_and_fire(now)
        except Exception as exc:  # noqa: BLE001
            app.logger.exception("best-share watcher tick failed: %s", exc)
        try:
            _check_blocks_and_fire(now)
        except Exception as exc:  # noqa: BLE001
            app.logger.exception("blocks watcher tick failed: %s", exc)
        # Record a history sample on its own cadence (every minute by default)
        if now - _last_history_ts >= HISTORY_SAMPLE_INTERVAL:
            try:
                record_history_sample(now)
                _last_history_ts = now
            except Exception as exc:  # noqa: BLE001
                app.logger.exception("history sample failed: %s", exc)
        time.sleep(WATCHER_INTERVAL)


def _start_watcher() -> None:
    t = threading.Thread(target=_watcher_loop, name="ducky-watcher", daemon=True)
    t.start()


# ──────────────────────────── routes ────────────────────────────

def _build_stats_payload(public: bool = False) -> dict:
    """Shared stats payload for /api/stats and /api/public/stats. The
    public variant strips fields a stranger shouldn't see."""
    pool = get_pool_stats()
    blocks = get_blocks_summary()
    net_diff = get_network_difficulty()
    pool_hashrate_hs = _hashrate_str_to_float(pool.get("hashrate_1m")) if pool.get("ok") else None
    eta = block_eta(pool_hashrate_hs, net_diff)

    payload = {
        "node": get_node_status(),
        "pool": pool,
        "workers": get_workers(),
        "blocks": blocks,
        "eta": eta,
        "now": int(time.time()),
    }
    if not public:
        s = load_settings()
        payload["payout"] = read_payout()
        payload["webhook_set"] = bool(s["discord"]["webhook_url"])
    return payload


@app.route("/", methods=["GET"])
def index():
    settings = load_settings()
    payload = _build_stats_payload(public=False)
    payout = read_payout()
    host = request.host.split(":")[0] or socket.gethostname()
    stratum_url = f"stratum+tcp://{host}:{STRATUM_PORT}"
    return render_template(
        "index.html",
        public=False,
        status=payload["node"],
        pool=payload["pool"],
        workers=payload["workers"],
        blocks=payload["blocks"],
        eta=payload["eta"],
        payout=payout,
        stratum_url=stratum_url,
        stratum_port=STRATUM_PORT,
        webhook_set=bool(settings["discord"]["webhook_url"]),
    )


@app.route("/public", methods=["GET"])
def public_view():
    """Read-only public dashboard. Hides payout address, settings link,
    and admin actions (reset/wipe/forget). Always reachable."""
    payload = _build_stats_payload(public=True)
    host = request.host.split(":")[0] or socket.gethostname()
    stratum_url = f"stratum+tcp://{host}:{STRATUM_PORT}"
    return render_template(
        "index.html",
        public=True,
        status=payload["node"],
        pool=payload["pool"],
        workers=payload["workers"],
        blocks=payload["blocks"],
        eta=payload["eta"],
        payout="",                # never send to public template
        stratum_url=stratum_url,
        stratum_port=STRATUM_PORT,
        webhook_set=False,
    )


@app.route("/api/stats", methods=["GET"])
def api_stats():
    return jsonify(_build_stats_payload(public=False))


@app.route("/api/public/stats", methods=["GET"])
def api_public_stats():
    return jsonify(_build_stats_payload(public=True))


@app.route("/api/history", methods=["GET"])
def api_history():
    """Hashrate history for the sparkline. ?range=1h|24h|7d (default 24h)."""
    rng = (request.args.get("range") or "24h").lower()
    seconds = {"1h": 3600, "24h": 86400, "7d": 86400 * 7}.get(rng, 86400)
    rows = read_history(seconds)
    return jsonify({
        "range": rng,
        "points": [{"ts": t, "hashrate": h} for t, h in rows],
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
    """Hide a worker from the dashboard. Stores the worker name + the
    shares-count at hide time + the unix ts in best_baselines.json.
    The dashboard's get_workers() filters this worker out on every poll
    UNTIL the worker submits at least one new share (shares count grows
    above the hide-time count) — at which point it auto-unhides.

    Why not just delete it: ckpool stores workers nested inside the
    user file (one file per BCH address). There's no per-worker file
    to delete; ckpool would just rewrite it on its next flush. The
    only honest implementation is dashboard-side filtering."""
    name = unquote((request.form.get("name") or request.args.get("name") or "").strip())
    if not name or not WORKER_ID_RE.match(name):
        abort(400, "invalid worker name")

    now = int(time.time())
    # Capture the worker's current shares count so auto-unhide knows
    # what counts as "a new share submitted after hiding".
    current_shares = 0
    if "." in name:
        addr = name.split(".", 1)[0]
        path = os.path.join(WORKERS_DIR, addr)
        try:
            with open(path, "r", encoding="utf-8") as f:
                user_data = _load_json_loose(f.read()) or {}
            for w in (user_data.get("worker") or []):
                if isinstance(w, dict) and w.get("workername") == name:
                    current_shares = int(w.get("shares", 0) or 0)
                    break
        except OSError:
            pass

    with _state_lock:
        state = _load_baselines()
        # Drop baseline so a returning worker is treated as fresh.
        state["workers"].pop(name, None)
        # Mark as hidden with both the ts and the share count snapshot.
        state.setdefault("hidden", {})[name] = {
            "ts": now,
            "shares": current_shares,
        }
        _save_baselines(state)

    wname = name.split(".", 1)[1] if "." in name else name
    flash(
        f"Hid worker {wname} from the dashboard. "
        "It'll automatically reappear if it submits another share.",
        "ok",
    )
    return redirect(url_for("index"))


@app.route("/best/reset", methods=["POST"])
def reset_best():
    """Soft reset for a single worker — no miner blip, no ckpool restart.

    Records ckpool's current bestshare for this worker as the
    `reset_snapshot`, which makes the dashboard hide the Best column
    until ckpool reports a value above the snapshot. The miner stays
    connected; ckpool's underlying state is not touched.

    For pool-wide reset (clearing all workers AND restarting ckpool to
    zero its in-memory accepted-shares + bestshare counters), use
    /stats/reset with name=__pool__.
    """
    name = (request.form.get("name") or "").strip()
    if not name or not WORKER_ID_RE.match(name):
        abort(400, "invalid worker name")

    reset_baseline(name)

    wname = name.split(".", 1)[1] if "." in name else name
    flash(
        f"Reset best for {wname}. The Best column will stay blank until a new high arrives.",
        "ok",
    )
    return redirect(url_for("index"))


def _safe_remove(path: str, base_dir: str) -> bool:
    """Remove a file only if it resolves inside base_dir. Returns True if
    the file existed and was removed, False if it didn't exist. Raises on
    other OS errors."""
    real = os.path.realpath(path)
    real_base = os.path.realpath(base_dir)
    if not real.startswith(real_base + os.sep):
        raise ValueError(f"refusing to remove {path}: outside {base_dir}")
    try:
        os.remove(real)
        return True
    except FileNotFoundError:
        return False


def _request_ckpool_restart() -> bool:
    """Drop the restart marker file. The ckpool entrypoint sees it next
    loop tick (~5s), kills ckpool, deletes the marker, and restarts ckpool
    fresh. Used to clear ckpool's in-memory bestshare cache after a wipe.
    Returns True on success."""
    try:
        # Touch creates the file with no content; only its existence matters.
        with open(RESTART_MARKER, "w", encoding="utf-8") as f:
            f.write(str(int(time.time())))
        return True
    except OSError as exc:
        app.logger.warning("could not write restart marker: %s", exc)
        return False


@app.route("/stats/reset", methods=["POST"])
def reset_stats():
    """Hard reset — deletes ckpool user file(s) AND requests a ckpool
    restart so its in-memory bestshare cache is cleared. Without the
    restart, ckpool would just rewrite the bestshare it remembers as soon
    as it next ticks — making "Wipe" feel cosmetic.

    Per-worker wipe in this layout deletes the worker's parent address
    user file, which also clears all OTHER workers under the same address.
    In solo mode that's typically just the one worker so it's fine, but the
    confirmation dialog warns about it.

    Miners briefly disconnect during the ckpool restart (~10s) and reconnect
    automatically.
    """
    name = (request.form.get("name") or "").strip()

    if name == "__pool__":
        # Note: we no longer delete user files or pool.status here.
        # Doing so while ckpool is still alive is racey — ckpool's flush
        # would just rewrite them with current state, and on restart it
        # would re-load that "fresh" file. The entrypoint now wipes those
        # files AFTER ckpool exits, which is the only safe time. See
        # services/ckpool/entrypoint.sh restart-marker block.

        with _state_lock:
            state = _load_baselines()
            state["workers"] = {}
            _save_baselines(state)

        # Wipe the hashrate history DB too. Without this, the graph keeps
        # showing the pre-reset period — often noisy/inflated while vardiff
        # was still climbing — and the y-axis stays stretched, making fresh
        # data look like a flat line at the bottom.
        history_rows_cleared = 0
        try:
            with _db_lock:
                conn = _open_db()
                try:
                    cur = conn.execute("DELETE FROM hashrate")
                    history_rows_cleared = cur.rowcount or 0
                    conn.commit()
                finally:
                    conn.close()
        except Exception as exc:  # noqa: BLE001
            app.logger.warning("could not clear history db: %s", exc)

        restart_ok = _request_ckpool_restart()

        bits = ["baselines cleared"]
        if history_rows_cleared:
            bits.append(f"history wiped ({history_rows_cleared} samples)")
        if restart_ok:
            bits.append("ckpool restart queued (state wipe will run after kill)")
        flash(
            " · ".join(bits)
            + ". Miners will reconnect within ~10s; the dashboard will rebuild stats from the next share.",
            "ok",
        )

    else:
        if not WORKER_ID_RE.match(name):
            abort(400, "invalid worker name")
        # In solo mode the per-worker reset is functionally identical to
        # the pool-wide one — ckpool's restart wipes everything. We keep
        # the per-worker button so the UI stays consistent and gives the
        # user a clear "this row's reset" affordance.
        address = name.split(".", 1)[0]

        with _state_lock:
            state = _load_baselines()
            # Drop baselines for every worker under this address.
            dropped = [k for k in state["workers"] if k.startswith(address + ".")]
            for k in dropped:
                state["workers"].pop(k, None)
            if dropped:
                _save_baselines(state)

        _request_ckpool_restart()

        wname = name.split(".", 1)[1] if "." in name else name
        flash(
            f"Reset queued for {wname}. ckpool will restart and rebuild stats "
            "from the next share. Miner will reconnect within ~10s.",
            "ok",
        )

    return redirect(url_for("index"))


@app.route("/settings", methods=["GET"])
def settings_page():
    s = load_settings()
    return render_template(
        "settings.html",
        settings=s,
        mindiff_h=humanise_diff(s["mindiff"]) if s["mindiff"] else "1",
        maxdiff_h=humanise_diff(s["maxdiff"]) if s["maxdiff"] else "0",
        startdiff_h=humanise_diff(s["startdiff"]) if s["startdiff"] else "1",
    )


@app.route("/settings/save", methods=["POST"])
def settings_save():
    s = load_settings()
    errors: list[str] = []

    mindiff, err = parse_diff(request.form.get("mindiff", "1"))
    if err:
        errors.append(f"mindiff: {err}")
    maxdiff, err = parse_diff(request.form.get("maxdiff", "0"))
    if err:
        errors.append(f"maxdiff: {err}")
    startdiff, err = parse_diff(request.form.get("startdiff", "1"))
    if err:
        errors.append(f"startdiff: {err}")

    if mindiff is not None and mindiff < 1:
        errors.append("mindiff must be ≥ 1")
    if startdiff is not None and mindiff is not None and startdiff < mindiff:
        errors.append(f"startdiff ({humanise_diff(startdiff)}) must be ≥ mindiff ({humanise_diff(mindiff)})")
    if maxdiff is not None and maxdiff != 0 and mindiff is not None and maxdiff < mindiff:
        errors.append(f"maxdiff ({humanise_diff(maxdiff)}) must be 0 (unlimited) or ≥ mindiff ({humanise_diff(mindiff)})")

    webhook_url = (request.form.get("webhook_url") or "").strip()
    if webhook_url and not webhook_url.startswith(DISCORD_WEBHOOK_PREFIXES):
        errors.append("Discord webhook URL must start with https://discord.com/api/webhooks/...")
    username = (request.form.get("username") or "").strip() or DEFAULT_SETTINGS["discord"]["username"]
    if len(username) > 80:
        errors.append("Discord username must be ≤ 80 chars (Discord's limit).")
    avatar_url = (request.form.get("avatar_url") or "").strip()
    if avatar_url and not avatar_url.startswith(("http://", "https://")):
        errors.append("Avatar URL must start with http:// or https://")
    if avatar_url and len(avatar_url) > 500:
        errors.append("Avatar URL is too long.")

    if errors:
        for e in errors:
            flash(e, "error")
        return redirect(url_for("settings_page"))

    new = {
        "mindiff": int(mindiff or 1),
        "maxdiff": int(maxdiff or 0),
        "startdiff": int(startdiff or 1),
        "discord": {
            "webhook_url": webhook_url,
            "username": username,
            "avatar_url": avatar_url,
        },
    }
    save_settings(new)

    diff_changed = (
        new["mindiff"]   != s["mindiff"] or
        new["maxdiff"]   != s["maxdiff"] or
        new["startdiff"] != s["startdiff"]
    )
    if diff_changed:
        flash("Settings saved. ckpool will restart within ~10s — connected miners will reconnect automatically.", "ok")
    else:
        flash("Settings saved.", "ok")
    return redirect(url_for("settings_page"))


@app.route("/webhook/test", methods=["POST"])
def test_webhook():
    # Send a sample of the actual embed style so users see what real
    # best-share notifications will look like.
    net_diff = get_network_difficulty()
    sample_current = (net_diff * 0.05) if net_diff else 1e9   # 5% of block as a sane sample
    sample = _build_best_share_embed(
        worker_name="test-worker",
        current=sample_current,
        net_diff=net_diff,
        ts=int(time.time()),
    )
    sample["title"] = "🦆 Test webhook (this is what new best shares will look like)"
    ok, msg = _post_discord(content=None, embeds=[sample])
    flash("Test webhook sent." if ok else f"Test failed: {msg}", "ok" if ok else "error")
    return redirect(url_for("settings_page"))


_start_watcher()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=FLASK_PORT)
