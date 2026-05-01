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
    try:
        with open(BASELINE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("workers"), dict):
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return {"workers": {}}


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


def reset_baseline(worker_id: str | None) -> int:
    with _state_lock:
        data = _load_baselines()
        workers = data["workers"]
        targets = list(workers.keys()) if worker_id in (None, "__pool__") else [worker_id]

        for wid in targets:
            current = _read_worker_current_best(wid)
            if current <= 0:
                # Fall back to whatever baseline we already have, so the
                # user doesn't end up with a zeroed baseline that fires a
                # webhook the moment ANY share comes in.
                current = float(workers.get(wid, {}).get("best", 0))
            workers[wid] = {"best": current, "last_sent_ts": 0}

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
    """Walk /users/* (one file per BCH address), expand each file's
    nested `worker` array into one dashboard row per worker."""
    if not os.path.isdir(WORKERS_DIR):
        return []
    now = int(time.time())
    out: list[dict] = []
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
            best = w.get("bestshare", 0) or w.get("bestever", 0) or 0
            out.append({
                "id": workername,           # full address.workername; used in form actions
                "name": display,            # short name for display
                "status": worker_status(age),
                "age_s": age,
                "age_human": humanise_age(age),
                "hashrate_1m": w.get("hashrate1m", "—"),
                "hashrate_1hr": w.get("hashrate1hr", "—"),
                "hashrate_24hr": w.get("hashrate1d") or w.get("hashrate24hr") or "—",
                "shares": w.get("shares", 0),
                "best_share": humanise_diff(best),
            })

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
    """Parse ckpool-style hashrate strings like '1.74T' to a float in H/s.
    Returns None if unparseable."""
    if s is None or s == "—":
        return None
    if isinstance(s, (int, float)):
        return float(s)
    s = str(s).strip()
    if not s or s == "0":
        return 0.0
    units = {"K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "P": 1e15, "E": 1e18}
    suffix = s[-1].upper()
    if suffix in units:
        try:
            return float(s[:-1]) * units[suffix]
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
    """In solo mode, ckpool keeps one file per BCH address with a nested
    worker array. There's no per-worker file to delete — workers come and
    go inside that array. So 'forget' here drops just our baseline entry
    for that workername; the worker disappears from the dashboard until
    it submits another share (which writes a new entry into the user
    file's worker array)."""
    name = unquote((request.form.get("name") or request.args.get("name") or "").strip())
    if not name or not WORKER_ID_RE.match(name):
        abort(400, "invalid worker name")

    with _state_lock:
        state = _load_baselines()
        had_baseline = state["workers"].pop(name, None) is not None
        if had_baseline:
            _save_baselines(state)

    wname = name.split(".", 1)[1] if "." in name else name
    flash(
        f"Forgot worker {wname}'s baseline. ckpool still tracks it inside its address's "
        "user file — it'll reappear on the dashboard if it submits more shares. "
        "Use 'Wipe all stats' on the pool card if you want to fully reset.",
        "ok",
    )
    return redirect(url_for("index"))


@app.route("/best/reset", methods=["POST"])
def reset_best():
    name = (request.form.get("name") or "").strip()
    if name == "__pool__":
        n = reset_baseline("__pool__")
        flash(f"Best-share baseline reset for all {n} worker(s).", "ok")
    else:
        if not WORKER_ID_RE.match(name):
            abort(400, "invalid worker name")
        reset_baseline(name)
        wname = name.split(".", 1)[1] if "." in name else name
        flash(f"Reset baseline for {wname}.", "ok")
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


@app.route("/stats/reset", methods=["POST"])
def reset_stats():
    """Hard reset — deletes ckpool user file(s) so shares/hashrate/bestshare
    all go back to zero. ckpool recreates the file on the next submitted
    share. For the pool-wide variant we also blank pool.status so the
    pool-level bestshare reads 0; ckpool overwrites it on its next update
    tick (within ~30s).

    Per-worker wipe in this layout deletes the worker's parent address
    user file, which also clears all OTHER workers under the same address.
    In solo mode that's typically just the one worker so it's fine, but the
    confirmation dialog warns about it.

    Miners stay connected the whole time; nothing in this route stops or
    restarts ckpool. The cost is just the lost stats history.
    """
    name = (request.form.get("name") or "").strip()

    if name == "__pool__":
        user_count = 0
        if os.path.isdir(WORKERS_DIR):
            for path in glob(os.path.join(WORKERS_DIR, "*")):
                fname = os.path.basename(path)
                if not USER_FILENAME_RE.match(fname):
                    continue
                try:
                    if _safe_remove(path, WORKERS_DIR):
                        user_count += 1
                except (OSError, ValueError) as exc:
                    app.logger.warning("could not remove %s: %s", path, exc)

        pool_status_reset = False
        try:
            if os.path.exists(POOL_STATUS_FILE):
                with open(POOL_STATUS_FILE, "w", encoding="utf-8") as f:
                    f.write("")
                pool_status_reset = True
        except OSError as exc:
            app.logger.warning("could not truncate pool.status: %s", exc)

        with _state_lock:
            state = _load_baselines()
            state["workers"] = {}
            _save_baselines(state)

        bits = [f"Cleared stats for {user_count} address file(s)"]
        if pool_status_reset:
            bits.append("reset pool best")
        bits.append("baselines cleared")
        flash(" · ".join(bits) + ". ckpool will rebuild stats on the next share.", "ok")

    else:
        if not WORKER_ID_RE.match(name):
            abort(400, "invalid worker name")
        # Wipe is per-user-file in this layout — clears every worker that
        # mines to the same payout address, not just this one. The
        # confirmation dialog in the template warns about this.
        address = name.split(".", 1)[0]
        target = os.path.join(WORKERS_DIR, address)
        try:
            removed = _safe_remove(target, WORKERS_DIR)
        except (OSError, ValueError) as exc:
            flash(f"Could not reset stats: {exc}", "error")
            return redirect(url_for("index"))

        with _state_lock:
            state = _load_baselines()
            # Drop baselines for every worker under this address.
            dropped = [k for k in state["workers"] if k.startswith(address + ".")]
            for k in dropped:
                state["workers"].pop(k, None)
            if dropped:
                _save_baselines(state)

        wname = name.split(".", 1)[1] if "." in name else name
        if removed:
            flash(
                f"Stats wiped for {wname} (and any siblings under the same address). "
                "ckpool will rebuild from the next share.",
                "ok",
            )
        else:
            flash(
                f"No stats file to wipe for {wname}'s address (baselines still cleared).",
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
