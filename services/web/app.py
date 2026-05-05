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
import logging
import logging.handlers
import os
import re
import socket
import sqlite3
import threading
import time
from functools import wraps
from glob import glob
from urllib.parse import unquote

import bcrypt
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
    session,
)

app = Flask(__name__)

# Module-level constants for the dashboard footer.
APP_STARTED_AT = int(time.time())
APP_VERSION = os.environ.get("APP_VERSION", "1.0")
GITHUB_URL = os.environ.get("GITHUB_URL", "https://github.com/ducksdev/ducky-installer")


@app.context_processor
def _inject_footer_globals():
    """Make footer values available to every template without each
    route having to pass them. Uptime is computed per-request so the
    footer ticks up live on each page load."""
    uptime_s = max(0, int(time.time()) - APP_STARTED_AT)
    return {
        "footer_uptime_s": uptime_s,
        "footer_uptime_human": humanise_duration(uptime_s),
        "footer_version": APP_VERSION,
        "footer_github": GITHUB_URL,
    }


def _load_or_create_flask_secret() -> str:
    """Persistent Flask secret. Stored in /shared so it survives container
    restarts and rebuilds — without it, every restart logs everyone out
    and breaks any flash() messages mid-flight. Generated once with
    cryptographic randomness; never logged or exposed.
    Honours FLASK_SECRET env var if set (for testing)."""
    env_secret = os.environ.get("FLASK_SECRET")
    if env_secret:
        return env_secret
    secret_path = os.path.join(
        os.environ.get("STATE_DIR", "/shared"), "flask.secret"
    )
    try:
        if os.path.exists(secret_path):
            with open(secret_path, "r", encoding="utf-8") as f:
                s = f.read().strip()
                if s:
                    return s
    except OSError:
        pass
    # Generate fresh and persist. 32 bytes hex = 256 bits of entropy.
    s = os.urandom(32).hex()
    try:
        os.makedirs(os.path.dirname(secret_path), exist_ok=True)
        with open(secret_path, "w", encoding="utf-8") as f:
            f.write(s)
        # Lock down — secret should not be world-readable.
        os.chmod(secret_path, 0o600)
    except OSError:
        # If we can't persist, fall back to in-memory secret. Sessions
        # won't survive restarts but the app keeps working.
        pass
    return s


app.secret_key = _load_or_create_flask_secret()
# 30-day "remember me" by default; renews on activity.
app.permanent_session_lifetime = 60 * 60 * 24 * 30
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

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
    "startdiff": 1000,
    "discord": {
        "webhook_url": "",
        "username": "Ducky Pool",
        "avatar_url": "",
        # Minimum share difficulty (raw int) before a webhook fires.
        # 0 means "no threshold" — every new best ever fires a webhook
        # (subject to first-sight + rate limit). Useful values:
        #   1_000_000   skip "Decent splash" tier spam
        #   10_000_000  skip everything below "Big splash"
        # User sets this via the Settings page; we accept shorthand
        # there and convert to raw int before storing.
        "min_share": 0,
    },
    # Duck-stage tiers. Each entry says "if a share is ≥ `min`, this is
    # the label/emoji/flavor to show on the dashboard and in webhook
    # embeds." Tiers are sorted by `min` ascending; the matching tier
    # is the highest one whose `min` is <= sdiff. Index 0 is always the
    # zero-share fallback. Users can edit, add, or remove tiers via
    # the Settings page; defaults restore on bad/missing data.
    # Colors are hex strings without #; rendered into Discord embed
    # decimals via int(color, 16). Optional — defaults applied if blank.
    "tiers": [
        {"min": 0,             "emoji": "🦆",  "label": "Empty pond",     "flavor": "Duck waiting patiently…",      "color": "5b6675"},
        {"min": 1,             "emoji": "💧",  "label": "Light ripples",   "flavor": "Tiny crumbs landing.",         "color": "5b8aa6"},
        {"min": 100_000,       "emoji": "🌊",  "label": "Decent splash",   "flavor": "The duck noticed!",            "color": "4a90a4"},
        {"min": 1_000_000,     "emoji": "🦆",  "label": "Big splash",      "flavor": "Other ducks paddling over.",   "color": "c77800"},
        {"min": 10_000_000,    "emoji": "🌪",  "label": "Huge wave",       "flavor": "Whole flock arriving!",        "color": "f9a825"},
        {"min": 100_000_000,   "emoji": "🍞",  "label": "Loaf alert",      "flavor": "Duck spotted a loaf!",         "color": "ff6b00"},
        {"min": 1_000_000_000, "emoji": "👑",  "label": "Royal duck",      "flavor": "Approaching the bakery!",      "color": "f9a825"},
    ],
    # Optional dashboard password protection. When disabled (default),
    # the dashboard and admin actions are open to anyone who can reach
    # the web port — fine for LAN-only setups, dangerous if exposed.
    # /public stays unauthenticated regardless. Password is stored as a
    # bcrypt hash; the plaintext never touches disk.
    "auth": {
        "enabled": False,
        "password_hash": "",
    },
}

ONLINE_SECONDS = 10 * 60
STALE_SECONDS = 30 * 60

WATCHER_INTERVAL = int(os.environ.get("WATCHER_INTERVAL", "15"))
WEBHOOK_MIN_INTERVAL = int(os.environ.get("WEBHOOK_MIN_INTERVAL", "15"))
WEBHOOK_TIMEOUT = 10

# ckpool with -L (--log-shares) writes one JSON line per accepted share to:
#   <CKPOOL_LOG_ROOT>/<block_height_in_hex>/<workinfo_id_hex>.sharelog
# Each line has fields: workername, sdiff (share difficulty), result, errn,
# createdate ("unix_ts,microseconds"). The web container mounts ckpool's
# /var/log/ckpool as /pool-logs (rw=true on the worker subdir for cleanup,
# but we only read share logs).
CKPOOL_LOG_ROOT = os.environ.get("CKPOOL_LOG_ROOT", "/pool-logs")
SHARE_TAIL_INTERVAL = int(os.environ.get("SHARE_TAIL_INTERVAL", "2"))

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


def humanise_duration(seconds: float | None) -> str:
    """Human-readable duration WITHOUT 'ago' suffix. Used for uptime
    displays where the value isn't a relative timestamp.
    Examples: 30s -> '30s', 95s -> '1m 35s', 7320s -> '2h 2m',
    400000s -> '4d 15h'. Falls through to '—' for None.
    """
    if seconds is None:
        return "—"
    s = int(seconds)
    if s < 0:
        s = 0
    if s < 60:
        return f"{s}s"
    if s < 3600:
        m, sec = divmod(s, 60)
        return f"{m}m {sec}s" if sec else f"{m}m"
    if s < 86400:
        h, rem = divmod(s, 3600)
        m = rem // 60
        return f"{h}h {m}m" if m else f"{h}h"
    d, rem = divmod(s, 86400)
    h = rem // 3600
    return f"{d}d {h}h" if h else f"{d}d"


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


def _normalise_tiers(raw) -> list:
    """Take user-supplied tiers, drop invalid rows, sort by min, and
    guarantee a level-0 fallback. Returns a fresh list every call so
    callers can mutate freely."""
    if not isinstance(raw, list) or not raw:
        return [dict(t) for t in DEFAULT_SETTINGS["tiers"]]

    cleaned = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        try:
            mn = int(entry.get("min", 0) or 0)
        except (TypeError, ValueError):
            continue
        if mn < 0:
            continue
        label = str(entry.get("label", "") or "").strip()
        if not label:
            continue  # label is required — empty rows aren't useful
        emoji = str(entry.get("emoji", "") or "").strip() or "🦆"
        flavor = str(entry.get("flavor", "") or "").strip()
        color = str(entry.get("color", "") or "").strip().lstrip("#")
        # Validate color is hex (or empty for default)
        if color:
            try:
                int(color, 16)
            except ValueError:
                color = ""
        cleaned.append({
            "min": mn,
            "emoji": emoji,
            "label": label,
            "flavor": flavor,
            "color": color,
        })

    if not cleaned:
        return [dict(t) for t in DEFAULT_SETTINGS["tiers"]]

    # Sort ascending by min so duck_stage can iterate in order.
    cleaned.sort(key=lambda t: t["min"])

    # Guarantee a level-0 fallback for sdiff <= 0. If the user dropped
    # the "Empty pond" row, prepend a minimal one so duck_stage always
    # has something to return.
    if cleaned[0]["min"] > 0:
        cleaned.insert(0, dict(DEFAULT_SETTINGS["tiers"][0]))

    return cleaned


def _merge_defaults(loaded: dict) -> dict:
    out = dict(DEFAULT_SETTINGS)
    out.update({k: v for k, v in loaded.items() if k not in ("discord", "auth", "tiers")})
    discord = dict(DEFAULT_SETTINGS["discord"])
    discord.update(loaded.get("discord") or {})
    out["discord"] = discord
    auth = dict(DEFAULT_SETTINGS["auth"])
    auth.update(loaded.get("auth") or {})
    # Defensive: enabled is meaningless without a hash. If someone hand-
    # edits settings.json and turns enabled=true with no hash, treat as
    # off so we never lock the user out.
    if not auth.get("password_hash"):
        auth["enabled"] = False
    out["auth"] = auth
    out["tiers"] = _normalise_tiers(loaded.get("tiers"))
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


# ──────────────────────────── auth ────────────────────────────
#
# Optional single-password dashboard auth. Off by default. When on, all
# admin routes + the main dashboard require a session cookie set by
# /login. /public and /api/public/stats are intentionally exempt — they
# exist so anyone can be shown a read-only view safely.
#
# Threat model: this stops casual snooping if the dashboard is exposed
# (port forward, accidental public binding). It does NOT defend against:
#   - eavesdropping over plain HTTP (passwords travel in clear) — pair
#     this with TLS via Tailscale, Cloudflare Tunnel, or nginx+Let's
#     Encrypt if exposing externally
#   - shell access to the host (settings.json is readable)
#   - sustained brute-force (there's no rate limiting; bcrypt's slowness
#     is the only defence)
#
# Recovery: if you forget the password, SSH in, edit
# /var/lib/ducky-pool/shared/settings.json and set
# `"auth": {"enabled": false, "password_hash": ""}`, then restart the
# stack. There is no password reset email — this is a self-hosted tool.

def set_password(plaintext: str) -> str:
    """Hash a plaintext password with bcrypt and persist into settings.
    Returns the hash for the caller's confirmation; the plaintext never
    leaves this function."""
    if not plaintext:
        raise ValueError("password cannot be empty")
    if len(plaintext) < 6:
        raise ValueError("password must be at least 6 characters")
    h = bcrypt.hashpw(plaintext.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    s = load_settings()
    s["auth"]["password_hash"] = h
    s["auth"]["enabled"] = True
    save_settings(s)
    return h


def check_password(plaintext: str) -> bool:
    """Constant-time compare against the stored hash. Returns False if
    auth is disabled or no hash is set (defensive — a missing hash
    should never authenticate any password)."""
    if not plaintext:
        return False
    s = load_settings()
    stored = s["auth"].get("password_hash") or ""
    if not stored:
        return False
    try:
        return bcrypt.checkpw(plaintext.encode("utf-8"), stored.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def auth_enabled() -> bool:
    s = load_settings()
    return bool(s["auth"].get("enabled") and s["auth"].get("password_hash"))


def is_logged_in() -> bool:
    """True if the session is authenticated, OR if auth is disabled
    entirely (in which case 'logged in' is the natural state for all
    private routes).

    The session_secret_version field lets us invalidate all existing
    sessions when the password changes — bumping it on save invalidates
    any outstanding cookies that referenced the old version."""
    if not auth_enabled():
        return True
    if not session.get("auth_ok"):
        return False
    s = load_settings()
    expected = s["auth"].get("password_hash", "")
    # Bind the session to the current password hash. If admin changes
    # password, all old sessions become invalid automatically.
    return session.get("auth_hash_token") == _hash_token(expected)


def _hash_token(password_hash: str) -> str:
    """Short stable token derived from the bcrypt hash. We don't put
    the bcrypt hash itself in the cookie — it's needlessly long and we
    don't want hashes echoing back to the browser. A truncated SHA-256
    of the hash is enough to detect password changes."""
    import hashlib
    if not password_hash:
        return ""
    return hashlib.sha256(password_hash.encode("utf-8")).hexdigest()[:16]


def login_required(f):
    """Decorator: redirect unauthenticated users to /login when auth
    is enabled. When auth is off, behaves as a no-op."""
    @wraps(f)
    def wrapped(*args, **kwargs):
        if is_logged_in():
            return f(*args, **kwargs)
        # Preserve the originally-requested URL so we can return there
        # after login. Only allow safe relative URLs (no offsite redirects).
        next_url = request.full_path if request.method == "GET" else url_for("index")
        if not next_url or not next_url.startswith("/") or next_url.startswith("//"):
            next_url = url_for("index")
        return redirect(url_for("login", next=next_url))
    return wrapped


def login_required_json(f):
    """Same as login_required but returns 401 JSON instead of redirecting.
    For /api/stats which the dashboard JS polls every 10s — a 302 to a
    login page would be confusing."""
    @wraps(f)
    def wrapped(*args, **kwargs):
        if is_logged_in():
            return f(*args, **kwargs)
        return jsonify({"error": "auth required", "auth_required": True}), 401
    return wrapped


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


def _read_worker_current_shares(worker_id: str) -> int:
    """Same idea as _read_worker_current_best but returns the shares
    count. Used by reset_baseline to record a "reset_shares" snapshot
    so the dashboard can detect when the worker has submitted a NEW
    share after reset (regardless of whether it beats the old best)."""
    if "." not in worker_id:
        return 0
    address = worker_id.split(".", 1)[0]
    path = os.path.join(WORKERS_DIR, address)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = _load_json_loose(f.read()) or {}
    except OSError:
        return 0
    for w in (data.get("worker") or []):
        if isinstance(w, dict) and w.get("workername") == worker_id:
            try:
                return int(w.get("shares", 0) or 0)
            except (TypeError, ValueError):
                return 0
    return 0


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
    '__pool__'). Records snapshots of both ckpool's current bestshare
    and shares count. The dashboard's get_workers() then:

      - Hides the Best column until the worker submits a new share
        (shares count > reset_shares).
      - Tracks the highest share submitted POST-reset in a separate
        post_reset_best field, so the displayed best climbs naturally
        from the first new share — not from "must beat the old record".

    This matches AxeBCH's behaviour: hit Reset, the next share counts
    as the new best regardless of magnitude, and subsequent shares
    only update the displayed best when they beat it.

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
            current_best = _read_worker_current_best(wid)
            current_shares = _read_worker_current_shares(wid)
            if current_best <= 0:
                # Worker hasn't reported a bestshare yet — fall back to
                # whatever baseline we have so we don't spuriously fire
                # a webhook when the first share arrives.
                current_best = float(workers.get(wid, {}).get("best", 0))
            workers[wid] = {
                "best": current_best,            # Discord baseline ATH
                "last_sent_ts": 0,
                "reset_snapshot": current_best,  # ckpool bestshare at reset
                "reset_shares": current_shares,  # ckpool shares count at reset
                "post_reset_best": 0.0,          # highest share seen post-reset
            }

        _save_baselines(data)
        return len(targets)


# ──────────────────────────── readers ────────────────────────────

# ──────────────────────────── health ────────────────────────────
#
# /health page data. Read-only system inspection: disk, RAM, load,
# uptime, BCH sync, plus best-effort container running detection.
# All paths come from env vars set by docker-compose so we can run
# the same code outside docker (tests) or with different mounts.

HOST_PROC = os.environ.get("HOST_PROC", "/proc")
HOST_ROOT = os.environ.get("HOST_ROOT", "/")
BCHNODE_LOG = os.environ.get("BCHNODE_LOG", "/bchnode-logs/debug.log")
WEB_LOG_FILE = os.environ.get("WEB_LOG_FILE", "/shared/logs/web.log")
CKPOOL_LOG_FILE = os.environ.get("CKPOOL_LOG_FILE", "/pool-logs/ckpool.log")


def _read_proc_file(rel: str) -> str | None:
    """Read a file under /host-proc, return its contents or None on error."""
    path = os.path.join(HOST_PROC, rel)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def get_system_health() -> dict:
    """Disk, RAM, load, uptime — best-effort, never raises."""
    out: dict = {}

    # Disk usage of the host root and the data directory
    disks = []
    for label, path in (
        ("Root (/)", HOST_ROOT),
        ("Data (/var/lib/ducky-pool)", os.path.join(HOST_ROOT, "var/lib/ducky-pool")),
    ):
        try:
            st = os.statvfs(path)
            total = st.f_blocks * st.f_frsize
            free = st.f_bavail * st.f_frsize
            used = total - free
            disks.append({
                "label": label,
                "total_bytes": total,
                "used_bytes": used,
                "free_bytes": free,
                "used_pct": round(100 * used / total, 1) if total > 0 else 0.0,
            })
        except OSError as exc:
            disks.append({"label": label, "error": str(exc)})
    out["disks"] = disks

    # Memory from /proc/meminfo
    mem_raw = _read_proc_file("meminfo")
    if mem_raw:
        mem = {}
        for line in mem_raw.splitlines():
            parts = line.split(":", 1)
            if len(parts) != 2:
                continue
            key = parts[0].strip()
            val = parts[1].strip().split()[0]
            try:
                mem[key] = int(val) * 1024  # /proc/meminfo is kB
            except ValueError:
                pass
        total = mem.get("MemTotal", 0)
        avail = mem.get("MemAvailable", mem.get("MemFree", 0))
        used = max(0, total - avail)
        out["memory"] = {
            "total_bytes": total,
            "available_bytes": avail,
            "used_bytes": used,
            "used_pct": round(100 * used / total, 1) if total > 0 else 0.0,
        }
        swap_total = mem.get("SwapTotal", 0)
        swap_free = mem.get("SwapFree", 0)
        swap_used = max(0, swap_total - swap_free)
        out["swap"] = {
            "total_bytes": swap_total,
            "used_bytes": swap_used,
            "used_pct": round(100 * swap_used / swap_total, 1) if swap_total > 0 else 0.0,
        }

    # Load average
    load_raw = _read_proc_file("loadavg")
    if load_raw:
        parts = load_raw.split()
        try:
            out["load"] = {
                "1m": float(parts[0]),
                "5m": float(parts[1]),
                "15m": float(parts[2]),
            }
        except (IndexError, ValueError):
            pass

    # Uptime
    uptime_raw = _read_proc_file("uptime")
    if uptime_raw:
        try:
            out["uptime_s"] = int(float(uptime_raw.split()[0]))
        except (IndexError, ValueError):
            pass

    # CPU count (informational — tells us load context)
    try:
        out["cpu_count"] = os.cpu_count() or 1
    except Exception:  # noqa: BLE001
        out["cpu_count"] = 1

    return out


def get_service_health() -> list[dict]:
    """Per-service status. Detected via sentinel files since the web
    container has no Docker access. A service is 'running' if its
    expected sentinel file has been touched recently; otherwise we
    flag it as down or unknown."""
    now = time.time()
    services = []

    # bchnode: debug.log gets appended-to constantly while running
    bch = {"name": "bchnode", "label": "BCH node"}
    try:
        st = os.stat(BCHNODE_LOG)
        age = now - st.st_mtime
        bch["running"] = age < 120  # touched in last 2 minutes
        bch["last_log_age_s"] = int(age)
    except OSError:
        bch["running"] = False
        bch["error"] = "debug.log not found (container may not be running)"
    # Layer in BCH sync info if RPC works
    node = get_node_status()
    if node.get("ok"):
        bch["chain"] = node.get("chain")
        bch["blocks"] = node.get("blocks")
        bch["headers"] = node.get("headers")
        bch["verification_progress"] = node.get("verification_progress")
        bch["ibd"] = node.get("ibd")
        bch["connections"] = node.get("connections")
        bch["version"] = node.get("version")
        bch["rpc_ok"] = True
    else:
        bch["rpc_ok"] = False
        bch["rpc_error"] = node.get("error")
    services.append(bch)

    # ckpool: pool.status is rewritten every 60s; ckpool.log is appended
    # to whenever a share lands. We use ckpool.log mtime for liveness.
    ck = {"name": "ckpool", "label": "ckpool"}
    try:
        st = os.stat(CKPOOL_LOG_FILE)
        age = now - st.st_mtime
        ck["running"] = age < 180  # 3 minutes — ckpool writes status every 60s
        ck["last_log_age_s"] = int(age)
    except OSError:
        ck["running"] = False
        ck["error"] = "ckpool.log not found"
    # Pool status snapshot
    pool = get_pool_stats()
    if pool.get("ok"):
        ck["pool_ok"] = True
        ck["pool_workers"] = pool.get("workers")
        ck["pool_users"] = pool.get("users")
    else:
        ck["pool_ok"] = False
    services.append(ck)

    # web: that's us. We're obviously running if we can answer this.
    services.append({
        "name": "web",
        "label": "Dashboard",
        "running": True,
    })

    return services


def health_payload() -> dict:
    """Combined payload for /api/health. Always returns even on partial
    failures — UI shows what it can."""
    return {
        "system": get_system_health(),
        "services": get_service_health(),
        "now": int(time.time()),
    }


# ──────────────────────────── log tailer ────────────────────────────
#
# Live log streaming for the Health page. Uses simple HTTP polling
# rather than SSE: simpler, works through any reverse proxy, easy to
# reason about. The client passes ?after_byte=<N>; we return everything
# from byte N onward (capped to the latest N lines or M bytes).

LOG_FILES = {
    "ckpool":  CKPOOL_LOG_FILE,
    "bchnode": BCHNODE_LOG,
    "web":     WEB_LOG_FILE,
}
LOG_TAIL_MAX_LINES = 500     # initial fetch caps at this many lines
LOG_TAIL_MAX_BYTES = 200_000  # never read more than this in one call


def tail_log(name: str, after_byte: int | None = None) -> dict:
    """Return new log content for the named service.

    If after_byte is None, return the last LOG_TAIL_MAX_LINES lines.
    Otherwise return everything from after_byte to the current EOF
    (capped at LOG_TAIL_MAX_BYTES). The next_byte field tells the
    client what to pass on its next poll."""
    path = LOG_FILES.get(name)
    if not path:
        return {"error": "unknown log"}
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        return {"error": f"cannot stat log: {exc}", "lines": [], "next_byte": 0}

    # If file shrank (log rotation, restart that truncated), reset cursor.
    if after_byte is not None and after_byte > size:
        after_byte = 0

    if after_byte is None:
        # Initial fetch — read tail of file. Read up to LOG_TAIL_MAX_BYTES
        # from EOF, then keep only the last LOG_TAIL_MAX_LINES lines.
        start = max(0, size - LOG_TAIL_MAX_BYTES)
        try:
            with open(path, "rb") as f:
                f.seek(start)
                blob = f.read(size - start)
        except OSError as exc:
            return {"error": str(exc), "lines": [], "next_byte": 0}
        text = blob.decode("utf-8", errors="replace")
        # Drop a possibly-partial first line if we didn't start at 0
        if start > 0:
            nl = text.find("\n")
            if nl >= 0:
                text = text[nl + 1:]
        lines = text.splitlines()[-LOG_TAIL_MAX_LINES:]
        return {"lines": lines, "next_byte": size}

    # Incremental fetch — read from after_byte to EOF, cap at MAX_BYTES.
    end = min(size, after_byte + LOG_TAIL_MAX_BYTES)
    if end <= after_byte:
        return {"lines": [], "next_byte": after_byte}
    try:
        with open(path, "rb") as f:
            f.seek(after_byte)
            blob = f.read(end - after_byte)
    except OSError as exc:
        return {"error": str(exc), "lines": [], "next_byte": after_byte}
    text = blob.decode("utf-8", errors="replace")
    lines = text.splitlines()
    # If the chunk doesn't end on a newline, the last line is partial —
    # drop it and rewind next_byte so we re-read it next poll.
    if blob and not blob.endswith(b"\n") and lines:
        partial = lines.pop()
        end -= len(partial.encode("utf-8"))
    return {"lines": lines, "next_byte": end}


# ──────────────────────────── web log file ────────────────────────────
#
# Write our own application log to /shared/logs/web.log so it shows up
# in the Health page log viewer alongside ckpool and bchnode.


def _setup_web_log_file() -> None:
    """Configure a rotating file handler in /shared/logs/web.log so the
    Health page log viewer has something to show for the 'web' service."""
    log_dir = os.path.dirname(WEB_LOG_FILE)
    try:
        os.makedirs(log_dir, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            WEB_LOG_FILE, maxBytes=2_000_000, backupCount=2, encoding="utf-8"
        )
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        # Attach to both Flask's logger and the root so any module-level
        # logging.info(...) calls get captured.
        app.logger.addHandler(handler)
        app.logger.setLevel(logging.INFO)
        logging.getLogger().addHandler(handler)
        app.logger.info("web log started → %s", WEB_LOG_FILE)
    except OSError as exc:
        # Logging infra is best-effort. If /shared isn't writable
        # somehow, the rest of the app keeps working.
        print(f"[web] could not configure file log: {exc}", flush=True)


# ──────────────────────────── restart markers ────────────────────────────
#
# Restart actions for each service drop a marker file into /shared.
# ckpool's container watches its own marker (/shared/.restart_ckpool)
# from inside the container. bchnode and web restarts are picked up
# by the host-side ducky-restart-watcher.service which calls
# `docker compose restart <name>`.

ALLOWED_RESTART_SERVICES = {"ckpool", "bchnode", "web"}
RESTART_MARKER_PREFIX = ".restart_"


def request_service_restart(service: str) -> tuple[bool, str]:
    """Drop a marker file the appropriate watcher picks up. Returns
    (ok, message). Idempotent — if a marker is already present we
    don't re-create it (the watcher will pick up the existing one)."""
    if service not in ALLOWED_RESTART_SERVICES:
        return False, f"unknown service '{service}'"
    marker = os.path.join(STATE_DIR, RESTART_MARKER_PREFIX + service)
    try:
        # Only create if it doesn't already exist (avoid double-restart).
        if not os.path.exists(marker):
            with open(marker, "w", encoding="utf-8") as f:
                f.write(str(int(time.time())))
        return True, f"{service} restart requested"
    except OSError as exc:
        return False, f"could not write marker: {exc}"


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
            # ckpool's "Workers" field is actually a count of TCP
            # connections — a single miner with 2 stratum sessions
            # registers as 2 here. The dashboard previously labelled
            # this as "Workers" which was confusing alongside the
            # per-worker rows. We now expose it explicitly as
            # `connections` and let the template render the count of
            # unique worker names separately (computed by get_workers()).
            "connections": data.get("Workers", 0),
            "workers": data.get("Workers", 0),  # legacy alias
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
            "best_share_raw": float(data.get("bestshare", 0) or 0),
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

    # Look up network difficulty once for the Royal-duck stage check.
    # get_network_difficulty() is cached so this is essentially free.
    net_diff = get_network_difficulty()

    # Tier count for progress01 calculation. The duck position is
    # tier-based: each tier maps to an evenly-spaced position from 0
    # (level 0) to 1 (highest tier). tier_count - 1 is the divisor so
    # the highest tier lands exactly at 1.0 (next to the bread emoji).
    s = load_settings()
    tier_count = max(2, len(s.get("tiers") or []))

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

            last_userfile = int(w.get("lastshare", 0) or 0)
            last_tailer = get_share_seen_ts(workername) or 0
            # Use whichever is more recent. The tailer fires ~2s after
            # an actual share lands; the user file lags up to 60s. The
            # tailer is therefore the dominant signal for active miners,
            # while the user file is the only signal we have for workers
            # that connected before the share-tail thread started up.
            last = max(last_userfile, last_tailer)
            age = (now - last) if last else None

            # Hidden filter: skip workers the user has Hidden, UNLESS
            # they've genuinely submitted a fresh share after being
            # hidden. The primary signal is the lastshare timestamp:
            # if the share-tail or the user file reports a lastshare
            # newer than hidden_at_ts, the worker has actually checked
            # in since the hide moment.
            #
            # We deliberately do NOT trust share-count growth as the
            # primary signal because ckpool can reload cached state on
            # restart that resurrects pre-hide share counts, and after a
            # wipe-reset (where shares_at_hide is set to 0) any worker
            # entry with shares >= 1 trivially passes the "grew" check —
            # which would defeat the hide for every inactive miner that
            # ckpool happens to remember. The timestamp comparison is
            # more honest: monotonic in real time, immune to file-cache
            # resurrection, can only succeed if a new share actually
            # landed after the hide. share-count check is kept as a
            # backup tie-breaker for boundary cases.
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
                came_back = False
                if hidden_at_ts is not None and last > hidden_at_ts:
                    came_back = True
                elif (
                    shares_at_hide is not None
                    and shares_at_hide > 0
                    and current_shares > shares_at_hide
                ):
                    # Backup: only trust share-count growth if hide-time
                    # baseline was non-zero. Avoids the post-wipe
                    # "0→1 = back online" trap.
                    came_back = True
                if came_back:
                    auto_unhidden.append(workername)
                else:
                    continue

            try:
                ckpool_best = float(w.get("bestshare", 0) or w.get("bestever", 0) or 0)
            except (TypeError, ValueError):
                ckpool_best = 0.0

            # Per-worker soft-reset display logic:
            #
            # We track three values per worker in best_baselines.json:
            #   reset_snapshot    : ckpool's bestshare at moment of reset
            #   reset_shares      : ckpool's shares count at moment of reset
            #   post_reset_best   : highest share difficulty observed
            #                       SINCE the reset (set by the log tailer)
            #
            # The displayed Best is decided in this priority order:
            #   1. If post_reset_best > 0  → show it (most accurate;
            #      reflects real per-share diffs from ckpool's log)
            #   2. Else if shares_count grew but log tailer hasn't seen
            #      anything yet (e.g. log tailer disabled) → show
            #      ckpool's bestshare if it now exceeds reset_snapshot
            #   3. Else → show "—" (no new shares since reset)
            entry = baselines.get(workername) or {}
            reset_snapshot = float(entry.get("reset_snapshot", 0) or 0)
            reset_shares = int(entry.get("reset_shares", 0) or 0)
            post_reset_best = float(entry.get("post_reset_best", 0) or 0)
            current_shares = int(w.get("shares", 0) or 0)

            if post_reset_best > 0:
                displayed_best = post_reset_best
            elif reset_snapshot > 0 and current_shares > reset_shares:
                # Log tailer didn't pick up the share, but the shares
                # count grew — fall back to ckpool's bestshare if it
                # exceeded the snapshot (which means a new ATH happened)
                displayed_best = ckpool_best if ckpool_best > reset_snapshot else 0.0
            elif reset_snapshot == 0:
                # Never reset — show ckpool's all-time bestshare directly
                displayed_best = ckpool_best
            else:
                # Reset happened, no new shares seen yet
                displayed_best = 0.0

            stage = duck_stage(displayed_best, net_diff)
            progress_pct = (
                (displayed_best / net_diff * 100.0)
                if (net_diff and net_diff > 0 and displayed_best > 0)
                else 0.0
            )

            # Tier-based duck position. Each tier index maps to an
            # evenly-spaced position 0..1 along the pond. Level 0 is
            # at the far left (no shares), the highest tier is at the
            # far right (next to the bread). The duck only moves when
            # a share crosses into a new tier — predictable and
            # matches the visible tier label.
            level = stage.get("level", 0)
            if tier_count > 1:
                progress01 = level / (tier_count - 1)
            else:
                progress01 = 0.0
            progress01 = max(0.0, min(1.0, progress01))

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
                "best_share_raw": displayed_best,
                "stage_level": stage["level"],
                "stage_label": stage["label"],
                "stage_flavor": stage["flavor"],
                "stage_emoji": stage["emoji"],
                "progress_pct": progress_pct,
                "progress01": progress01,
            })

    # Persist any auto-unhidden workers.
    if auto_unhidden:
        with _state_lock:
            state = _load_baselines()
            for wid in auto_unhidden:
                state.get("hidden", {}).pop(wid, None)
            _save_baselines(state)

    # Sort alphabetically by worker name, using natural ordering so
    # 'Nerd2' comes before 'Nerd10' (plain str sort would put 'Nerd10'
    # first because '1' < '2' lexicographically). Case-insensitive.
    def _natural_key(name: str) -> list:
        out_parts: list = []
        for chunk in re.split(r"(\d+)", name or ""):
            if not chunk:
                continue
            if chunk.isdigit():
                out_parts.append((0, int(chunk)))
            else:
                out_parts.append((1, chunk.lower()))
        return out_parts
    out.sort(key=lambda w: _natural_key(w["name"]))
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


# ──────────────────────────── duck stages ────────────────────────────
#
# Maps a share's sdiff to a duck-themed stage ("how big is the splash?").
# Used both on the dashboard (per-worker label + SVG pond) and in the
# Discord webhook embed (replaces the generic "new best share" copy).
#
# Stage thresholds were picked for a typical NerdQAxe-class miner at ~5 TH/s
# under solo vardiff. At that scale you'd see "Decent splash" a few times
# per minute, "Big splash" every few minutes, and "Loaf alert" maybe once
# an hour. The very rare "Royal duck moment" is hit when a share crosses
# 1% of network difficulty.
#
# Each stage carries:
#   level   : 0..6, ascending (used for sorting / picking SVG stage art)
#   label   : short title for cards/embeds
#   flavor  : one-liner of duck microcopy
#   color   : RGB int for Discord embed border (warm pond colours)
#   emoji   : single character for inline use
_DEFAULT_TIER_COLOR = 0x5b6675  # neutral grey, used when a tier has no color set


def duck_stage(sdiff: float, net_diff: float | None = None) -> dict:
    """Find the matching tier for this share difficulty by walking the
    user-configured tier list from settings.json (or defaults if none).
    The tier list is sorted ascending by `min`; we pick the highest
    entry whose min is <= sdiff.

    The `net_diff` argument is preserved for backwards compatibility
    with callers that used to pass it for the "Royal duck on >= 1% of
    net diff" override. With user-defined tiers, that promotion is no
    longer automatic — set a high-min tier explicitly to get the same
    effect (e.g. min=10G with label="👑 Royal duck").
    """
    s = load_settings()
    tiers = s.get("tiers") or _normalise_tiers(None)

    if sdiff <= 0:
        # First entry is always the level-0 fallback (zero share).
        t = tiers[0]
    else:
        # Walk from the top down — first entry whose min is <= sdiff wins.
        t = tiers[0]
        for entry in tiers:
            if sdiff >= entry["min"]:
                t = entry
            else:
                break

    color_hex = t.get("color") or ""
    try:
        color_int = int(color_hex, 16) if color_hex else _DEFAULT_TIER_COLOR
    except ValueError:
        color_int = _DEFAULT_TIER_COLOR

    # `level` is the tier's index in the list — used by dashboard JS to
    # compute progress01 (how far along the duck has paddled).
    try:
        level = tiers.index(t)
    except ValueError:
        level = 0

    return {
        "level": level,
        "label": t.get("label", "Empty pond"),
        "flavor": t.get("flavor", ""),
        "color": color_int,
        "emoji": t.get("emoji", "🦆"),
    }


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
    """Build the duck-themed "new best share" embed.

    The title + description vary with stage (Empty pond → Royal duck);
    the structured fields preserve the actual numbers. Colour varies
    with stage so the embed visually escalates as splashes get bigger.
    """
    stage = duck_stage(current, net_diff)

    fields = [
        {"name": "🎯 Worker",     "value": f"**{worker_name}**", "inline": True},
        {"name": "💎 Best Share", "value": humanise_diff(current), "inline": True},
    ]

    description_lines = [
        f"*{stage['flavor']}*",
        "",
        f"**{worker_name}** just made a {humanise_diff(current)} share.",
    ]

    if net_diff and net_diff > 0:
        pct = (current / net_diff) * 100
        bar = progress_bar(current, net_diff)
        fields.append({
            "name": "📈 Block Diff",
            "value": humanise_diff(net_diff),
            "inline": True,
        })
        description_lines.append("")
        description_lines.append("🍞 **Progress to bread**")
        description_lines.append(f"`{bar}`  **{pct:.4f}%**")
    else:
        fields.append({
            "name": "📈 Block Diff",
            "value": "—",
            "inline": True,
        })

    return {
        "title": f"{stage['emoji']} {stage['label']}!",
        "description": "\n".join(description_lines),
        "color": stage["color"],
        "fields": fields,
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

                # Skip hidden workers UNLESS they've genuinely come back
                # online since being hidden. Primary signal is lastshare
                # timestamp vs hide-ts (immune to ckpool state-cache
                # resurrection); share-count growth is a backup that
                # only fires if the hide-time baseline was non-zero.
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
                    # Also consult the tailer-observed timestamp — it
                    # updates within ~2s of every share, so a worker
                    # actively mining will get past the hide before the
                    # user file's lastshare catches up (which can lag
                    # ~60s). This avoids a flicker where freshly-mining
                    # workers stay hidden for a minute after restart.
                    last_tailer = get_share_seen_ts(workername) or 0
                    last_effective = max(last, last_tailer)
                    came_back = False
                    if hidden_at_ts is not None and last_effective > hidden_at_ts:
                        came_back = True
                    elif (
                        shares_at_hide is not None
                        and shares_at_hide > 0
                        and cur_shares > shares_at_hide
                    ):
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

                # Apply the user-configured minimum share threshold.
                # Shares below the floor are still tracked (we update
                # the in-memory `best` value silently) so when a share
                # above the floor finally arrives, we don't fire on
                # every micro-improvement that happened in between.
                # We just don't WAKE Discord for them.
                min_share_raw = s["discord"].get("min_share", 0)
                try:
                    min_share = int(min_share_raw or 0)
                except (TypeError, ValueError):
                    min_share = 0

                if min_share > 0 and current < min_share:
                    if current > baseline:
                        entry["best"] = current
                        workers[workername] = entry
                        changed = True
                    continue

                # Rate-limit nuance: if a worker beats its record while
                # the rate limit is active, we used to silently advance
                # the baseline — which meant later, when the limit
                # cleared, there was nothing pending to announce because
                # baseline already matched the new record. So a flurry
                # of records inside the rate-limit window produced ZERO
                # webhooks instead of one summary webhook.
                #
                # The fix: when above-min AND above-baseline AND rate-
                # limited, store the new record as `pending_announce`
                # but DO NOT advance baseline. On the next tick, if the
                # rate limit has cleared, fire with whatever the highest
                # pending value is. This guarantees the user always
                # learns about new records — they just may be batched
                # to one webhook per rate-limit interval.
                rate_limit_ok = (now_ts - last_sent) >= WEBHOOK_MIN_INTERVAL
                pending = float(entry.get("pending_announce", 0) or 0)

                if current > baseline:
                    if rate_limit_ok:
                        # Fire now. Use the larger of `current` or any
                        # `pending_announce` (in case a bigger share
                        # came earlier but was rate-limited).
                        announce_value = max(current, pending)
                        display = workername.split(".", 1)[1] if "." in workername else workername
                        net_diff = get_network_difficulty()
                        ok, _msg = _post_discord(
                            content=None,
                            embeds=[_build_best_share_embed(
                                worker_name=display,
                                current=announce_value,
                                net_diff=net_diff,
                                ts=now_ts,
                            )],
                        )
                        entry["best"] = announce_value
                        entry["pending_announce"] = 0  # clear, we just announced it
                        if ok:
                            entry["last_sent_ts"] = now_ts
                        workers[workername] = entry
                        changed = True
                    else:
                        # Rate-limited. Track the highest record seen
                        # since the last fire, but don't move baseline
                        # — that way the next non-rate-limited tick
                        # still sees `current > baseline` and fires.
                        if current > pending:
                            entry["pending_announce"] = current
                            workers[workername] = entry
                            changed = True
                elif pending > 0 and rate_limit_ok:
                    # Edge case: worker stopped beating records but had
                    # a pending value queued during a previous rate-limit
                    # window. Fire that now.
                    display = workername.split(".", 1)[1] if "." in workername else workername
                    net_diff = get_network_difficulty()
                    ok, _msg = _post_discord(
                        content=None,
                        embeds=[_build_best_share_embed(
                            worker_name=display,
                            current=pending,
                            net_diff=net_diff,
                            ts=now_ts,
                        )],
                    )
                    entry["best"] = pending
                    entry["pending_announce"] = 0
                    if ok:
                        entry["last_sent_ts"] = now_ts
                    workers[workername] = entry
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

# ──────────────────────────── share log tailer ────────────────────────────
#
# ckpool's -L flag writes one JSON line per accepted share to files under
# CKPOOL_LOG_ROOT. The tree shape is:
#
#   /pool-logs/<block_height_hex>/<workinfo_id_hex>.sharelog
#
# Each line contains: workername, sdiff (the actual share difficulty),
# result (true/false), createdate ("unix_ts,microseconds"), and others.
#
# This tailer:
#   - Discovers the active block-height directory by mtime
#   - Tracks an offset per file so we resume from where we left off
#   - When ckpool rolls over to a new block, the new directory appears;
#     we just start tailing the new one and forget the old offsets
#   - For each share line, updates post_reset_best for the worker if
#     sdiff exceeds it. Fires a Discord webhook for each new high
#     (rate-limited via the existing WEBHOOK_MIN_INTERVAL).
#
# State on disk (in /shared/best_baselines.json) per worker:
#   post_reset_best : highest sdiff seen since the last reset_baseline.
#                     0 means no shares observed since reset.
# This is the value get_workers prefers when deciding what to display.

# In-memory file-tail offsets. Keyed by absolute path. Reset whenever
# we detect a directory roll-over (new block height).
_share_tail_offsets: dict[str, int] = {}
_share_tail_dir: str | None = None  # currently-active height dir
_share_tail_lock = threading.Lock()

# Per-worker last-share-seen timestamp populated by the tailer. The
# user-file approach (reading lastshare from /users/<addr>) lags by up
# to 60 seconds because ckpool only rewrites that file every minute.
# This dict gets updated within ~2 seconds of a share landing — making
# Online/Stale/Offline status accurate in near-real-time. Particularly
# helpful for MRR-style proxied connections where the user file's
# lastshare can easily fall behind reality.
# Keyed by full worker id "addr.workername".
_share_seen_ts: dict[str, int] = {}
_share_seen_lock = threading.Lock()


def get_share_seen_ts(worker_id: str) -> int | None:
    """Return the most recent timestamp at which the share tailer saw
    a share for this worker, or None if we've never seen one."""
    with _share_seen_lock:
        return _share_seen_ts.get(worker_id)


def _find_active_share_dir() -> str | None:
    """Return the path of the height-directory that ckpool is currently
    writing into, or None if no share dir exists yet. Heuristic: it's
    the immediate subdirectory of CKPOOL_LOG_ROOT whose mtime is newest,
    EXCLUDING the well-known ckpool-internal dirs (pool, users)."""
    if not os.path.isdir(CKPOOL_LOG_ROOT):
        return None
    best_path = None
    best_mtime = -1.0
    for name in os.listdir(CKPOOL_LOG_ROOT):
        if name in ("pool", "users") or name.startswith("."):
            continue
        full = os.path.join(CKPOOL_LOG_ROOT, name)
        if not os.path.isdir(full):
            continue
        try:
            mtime = os.path.getmtime(full)
        except OSError:
            continue
        if mtime > best_mtime:
            best_mtime = mtime
            best_path = full
    return best_path


def _tail_share_line(line: str, now_ts: int) -> None:
    """Parse one share-log JSON line and update post_reset_best +
    fire a Discord webhook if the share is a new post-reset high.
    Silently ignores malformed lines, rejected shares, and shares
    for workers we don't have a baseline for yet (those baselines
    come from the existing minute-aggregate watcher loop)."""
    line = line.strip()
    if not line or not line.startswith("{"):
        return
    try:
        share = json.loads(line)
    except json.JSONDecodeError:
        return

    # Only care about accepted shares. Rejected shares (errn != 0 or
    # result != true) shouldn't bump the user's best.
    if not share.get("result"):
        return
    if share.get("errn", 0) != 0:
        return

    workername = share.get("workername", "")
    if not workername or not WORKER_ID_RE.match(workername):
        return

    try:
        sdiff = float(share.get("sdiff", 0) or 0)
    except (TypeError, ValueError):
        return
    if sdiff <= 0:
        return

    # Record share-seen timestamp for accurate Online/Stale status.
    # We do this before the baseline check below — even workers we
    # haven't seen before should mark as freshly-active here.
    with _share_seen_lock:
        _share_seen_ts[workername] = now_ts

    # Update the worker's post_reset_best. We do this transactionally
    # under the existing _state_lock so we don't race with the user
    # clicking Reset (which writes a fresh entry) or the minute watcher
    # (which updates `best`).
    with _state_lock:
        state = _load_baselines()
        workers = state["workers"]
        hidden = state.get("hidden", {})
        entry = workers.get(workername)
        if entry is None:
            # Unknown worker — let the minute watcher seed its baseline
            # first. We'll start tracking on the next share after that.
            return

        # If hidden, stay hidden until they come back online (which the
        # minute watcher decides). Don't fire webhooks meanwhile.
        if workername in hidden:
            return

        prev_post = float(entry.get("post_reset_best", 0) or 0)
        prev_best = float(entry.get("best", 0) or 0)
        last_sent = int(entry.get("last_sent_ts", 0) or 0)

        # Only act if this share is a new post-reset high.
        if sdiff <= prev_post:
            return

        entry["post_reset_best"] = sdiff
        # Also track all-time best so the minute watcher's first-sight
        # rule continues to work (and so duplicate webhooks are avoided
        # if the post_reset best happens to equal the all-time best).
        if sdiff > prev_best:
            entry["best"] = sdiff

        # Fire webhook if rate limit allows AND share meets the
        # user-configured minimum threshold. The min-share check has to
        # happen here too — the share tailer is a SEPARATE firing path
        # from the minute watcher (_check_and_fire) and was previously
        # missing the threshold check, so 1K-100K shares fired through
        # this path even with min_share=100K configured. The threshold
        # is a notification filter, not a tracking filter — we still
        # update post_reset_best above so the dashboard's Best column
        # reflects this share, we just don't WAKE Discord.
        s = load_settings()
        webhook_url = s["discord"]["webhook_url"]
        try:
            min_share = int(s["discord"].get("min_share", 0) or 0)
        except (TypeError, ValueError):
            min_share = 0
        will_fire = (
            bool(webhook_url)
            and (now_ts - last_sent) >= WEBHOOK_MIN_INTERVAL
            and (min_share <= 0 or sdiff >= min_share)
        )

        workers[workername] = entry
        _save_baselines(state)

    # Webhook outside the state lock so we don't hold it during HTTP.
    if will_fire:
        display = workername.split(".", 1)[1] if "." in workername else workername
        net_diff = get_network_difficulty()
        ok, _msg = _post_discord(
            content=None,
            embeds=[_build_best_share_embed(
                worker_name=display,
                current=sdiff,
                net_diff=net_diff,
                ts=now_ts,
            )],
        )
        if ok:
            with _state_lock:
                state = _load_baselines()
                e = state["workers"].get(workername)
                if e is not None:
                    e["last_sent_ts"] = now_ts
                    state["workers"][workername] = e
                    _save_baselines(state)


def _share_tail_tick() -> None:
    """One iteration of the share-log tailer. Discovers the active
    block-height directory, tails any new bytes appended to the
    .sharelog files in it, and pushes each line through
    _tail_share_line. Resets offsets if the active dir changes."""
    global _share_tail_dir, _share_tail_offsets

    active = _find_active_share_dir()
    if active is None:
        return

    with _share_tail_lock:
        if active != _share_tail_dir:
            # Block changed (or first run) — drop offsets for the old dir
            # so we start each new block fresh. This is safe: shares from
            # the previous block were already processed during their
            # block's lifetime; we don't replay history.
            _share_tail_offsets = {}
            _share_tail_dir = active

    now_ts = int(time.time())
    try:
        names = os.listdir(active)
    except OSError:
        return

    for name in names:
        if not name.endswith(".sharelog"):
            continue
        path = os.path.join(active, name)
        try:
            size = os.path.getsize(path)
        except OSError:
            continue

        offset = _share_tail_offsets.get(path, 0)
        if size <= offset:
            continue   # nothing new

        # Read from offset to current end. ckpool writes one JSON line
        # per share; we only act on complete lines (ending with \n).
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(offset)
                chunk = f.read(size - offset)
        except OSError:
            continue
        if not chunk:
            continue

        # Find the last newline so we don't process a partial trailing line.
        last_nl = chunk.rfind("\n")
        if last_nl < 0:
            # No complete line yet — wait for more.
            continue
        complete = chunk[: last_nl + 1]
        new_offset = offset + len(complete.encode("utf-8"))

        for raw in complete.splitlines():
            try:
                _tail_share_line(raw, now_ts)
            except Exception as exc:  # noqa: BLE001
                app.logger.exception("share tail line failed: %s", exc)

        with _share_tail_lock:
            _share_tail_offsets[path] = new_offset


def _share_tail_loop() -> None:
    while True:
        try:
            _share_tail_tick()
        except Exception as exc:  # noqa: BLE001
            app.logger.exception("share tail tick failed: %s", exc)
        time.sleep(SHARE_TAIL_INTERVAL)


def _start_share_tailer() -> None:
    t = threading.Thread(target=_share_tail_loop, name="ducky-share-tail", daemon=True)
    t.start()


# ──────────────────────────── main watcher loop ────────────────────────────


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

def _update_pool_best_trackers(workers: list[dict], blocks_count: int,
                               network_height: int | None) -> dict:
    """Maintain the three pool-wide best-share counters used by the
    Best Share card's three display modes:

      ever            — ckpool's persistent all-time bestshare from
                        pool.status. Read directly elsewhere; not
                        managed here. Survives soft reset.
      since_block_found — highest share submitted since OUR pool last
                        won a block. Resets when get_blocks_summary's
                        count grows. If we've never won, this is the
                        same as "since install".
      current_block   — highest share submitted against the current
                        NETWORK block height. Resets when network
                        height grows (~every 10 minutes for BCH).

    All three are derived from the per-worker post_reset_best maxes
    on this tick. We compare the current max to the stored anchor
    and reset when a watched event has happened.

    State is persisted to best_baselines.json under 'pool_bests' so
    values survive web container restarts.
    """
    cur_max = 0.0
    for w in workers:
        try:
            cur_max = max(cur_max, float(w.get("best_share_raw", 0) or 0))
        except (TypeError, ValueError):
            continue

    out = {"since_block_found": cur_max, "current_block": cur_max}

    with _state_lock:
        state = _load_baselines()
        pb = state.setdefault("pool_bests", {})

        # since_block_found: reset when our blocks count goes up.
        sbf = pb.setdefault("since_block_found", {"value": 0.0, "anchor_blocks": 0})
        if blocks_count > int(sbf.get("anchor_blocks", 0) or 0):
            # New block found! Start fresh.
            sbf["value"] = cur_max
            sbf["anchor_blocks"] = blocks_count
        else:
            sbf["value"] = max(float(sbf.get("value", 0.0) or 0.0), cur_max)
        out["since_block_found"] = float(sbf["value"])

        # current_block: reset when network height advances.
        if network_height is not None and network_height > 0:
            cb = pb.setdefault("current_block", {"value": 0.0, "anchor_height": 0})
            if network_height > int(cb.get("anchor_height", 0) or 0):
                cb["value"] = cur_max
                cb["anchor_height"] = network_height
            else:
                cb["value"] = max(float(cb.get("value", 0.0) or 0.0), cur_max)
            out["current_block"] = float(cb["value"])

        _save_baselines(state)

    return out


def _build_stats_payload(public: bool = False) -> dict:
    """Shared stats payload for /api/stats and /api/public/stats. The
    public variant strips fields a stranger shouldn't see."""
    pool = get_pool_stats()
    blocks = get_blocks_summary()
    net_diff = get_network_difficulty()
    pool_hashrate_hs = _hashrate_str_to_float(pool.get("hashrate_1m")) if pool.get("ok") else None
    eta = block_eta(pool_hashrate_hs, net_diff)
    node = get_node_status()

    workers = get_workers()

    # Three best-share modes shown in the Best Share card. The user
    # cycles through them by clicking the value. See
    # _update_pool_best_trackers for what each one means.
    pool_bests_raw = _update_pool_best_trackers(
        workers,
        blocks_count=int(blocks.get("count", 0) or 0),
        network_height=int(node.get("blocks", 0) or 0) if node.get("ok") else None,
    )
    # ckpool's all-time bestshare from pool.status survives soft reset.
    # If pool.status isn't readable (early startup), fall back to the
    # since-reset max so we don't show "—" forever.
    ever_raw = 0.0
    if pool.get("ok"):
        try:
            ever_raw = float(pool.get("best_share_raw", 0) or 0)
        except (TypeError, ValueError):
            ever_raw = 0.0

    # Override pool best_share (the default mode shown) with the max
    # of per-worker post_reset_best — same behaviour as before, so
    # Reset still clears the displayed value. The other two modes are
    # available via the new pool["best_shares"] dict.
    if pool.get("ok"):
        since_reset_raw = max(
            (float(w.get("best_share_raw", 0) or 0) for w in workers),
            default=0.0,
        )
        pool["best_share"] = humanise_diff(since_reset_raw) if since_reset_raw > 0 else "—"
        pool["best_shares"] = {
            "ever": humanise_diff(ever_raw) if ever_raw > 0 else "—",
            "since_block_found": (
                humanise_diff(pool_bests_raw["since_block_found"])
                if pool_bests_raw["since_block_found"] > 0 else "—"
            ),
            "current_block": (
                humanise_diff(pool_bests_raw["current_block"])
                if pool_bests_raw["current_block"] > 0 else "—"
            ),
        }

    payload = {
        "node": node,
        "pool": pool,
        "workers": workers,
        "blocks": blocks,
        "eta": eta,
        "now": int(time.time()),
        "net_diff": net_diff,
        "net_diff_human": humanise_diff(net_diff) if net_diff else "—",
    }
    if not public:
        s = load_settings()
        payload["payout"] = read_payout()
        payload["webhook_set"] = bool(s["discord"]["webhook_url"])
    return payload


@app.route("/login", methods=["GET", "POST"])
def login():
    """Login page. If auth is disabled, redirect to dashboard — there's
    nothing to log into. If already logged in, also redirect."""
    if not auth_enabled():
        return redirect(url_for("index"))
    if is_logged_in():
        return redirect(url_for("index"))

    error = None
    if request.method == "POST":
        password = request.form.get("password") or ""
        if check_password(password):
            session.permanent = True
            session["auth_ok"] = True
            s = load_settings()
            session["auth_hash_token"] = _hash_token(s["auth"]["password_hash"])
            # Honour ?next= for post-login redirect, but only if it's a
            # safe same-origin path. Reject anything starting with //
            # (protocol-relative URL → could redirect off-site).
            next_url = request.args.get("next") or request.form.get("next") or url_for("index")
            if not next_url.startswith("/") or next_url.startswith("//"):
                next_url = url_for("index")
            return redirect(next_url)
        # Generic failure — don't tell attackers whether they got close.
        error = "Incorrect password."
        # Tiny artificial delay to discourage automated guessing on top of
        # bcrypt's natural slowness. bcrypt is the real defence.
        time.sleep(0.5)

    next_url = request.args.get("next") or url_for("index")
    return render_template("login.html", error=error, next_url=next_url)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("Signed out.", "ok")
    return redirect(url_for("login"))


@app.route("/", methods=["GET"])
@login_required
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
        net_diff_human=payload["net_diff_human"],
        payout=payout,
        stratum_url=stratum_url,
        stratum_port=STRATUM_PORT,
        webhook_set=bool(settings["discord"]["webhook_url"]),
        auth_enabled=auth_enabled(),
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
        net_diff_human=payload["net_diff_human"],
        payout="",                # never send to public template
        stratum_url=stratum_url,
        stratum_port=STRATUM_PORT,
        webhook_set=False,
    )


@app.route("/api/stats", methods=["GET"])
@login_required_json
def api_stats():
    return jsonify(_build_stats_payload(public=False))


@app.route("/api/public/stats", methods=["GET"])
def api_public_stats():
    return jsonify(_build_stats_payload(public=True))


@app.route("/api/history", methods=["GET"])
@login_required_json
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
@login_required
def set_payout():
    raw = (request.form.get("address") or "").strip()
    legacy, err = normalise_address(raw)
    if err or not legacy:
        flash(err or "Invalid address.", "error")
        return redirect(url_for("index"))

    # Detect whether this is a real change before writing — only restart
    # ckpool if the address actually changed (avoid unnecessary miner
    # reconnects if user just clicks Save on the same value).
    try:
        prev = read_payout()
    except Exception:  # noqa: BLE001
        prev = ""
    write_payout(legacy)

    if legacy != prev:
        # Re-render ckpool.conf with the new btcaddress and restart.
        request_service_restart("ckpool")
        if legacy != raw:
            flash(
                f"Saved. Converted CashAddr → legacy: {legacy}. "
                "ckpool will restart within ~10s to pick up the new address.",
                "ok",
            )
        else:
            flash(
                "Payout address saved. ckpool will restart within ~10s "
                "to pick up the new address.",
                "ok",
            )
    else:
        flash("Payout address unchanged.", "ok")
    return redirect(url_for("index"))


@app.route("/workers/forget", methods=["POST"])
@login_required
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
@login_required
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
    """Drop the plain restart marker file. The host-side ducky-restart-
    watcher service sees it next loop tick (~2s), re-renders ckpool.conf
    from settings.json, and runs `docker compose restart ckpool`. ckpool
    keeps its bestshare/user-file state across this kind of restart —
    use it for config changes (mindiff/maxdiff/payout) only. Returns
    True on success."""
    try:
        with open(RESTART_MARKER, "w", encoding="utf-8") as f:
            f.write(str(int(time.time())))
        return True
    except OSError as exc:
        app.logger.warning("could not write restart marker: %s", exc)
        return False


def _request_ckpool_wipe() -> bool:
    """Drop the WIPE marker. The host watcher stops ckpool, deletes
    user files (per-worker share counts + bestshare) AND pool.status
    (pool-wide bestshare cache), then starts ckpool fresh. This is the
    only honest way to reset because ckpool keeps state in memory
    while running — deleting files under a live ckpool just gets them
    rewritten on the next flush. Returns True on success."""
    marker_path = os.path.join(STATE_DIR, ".restart_ckpool_wipe")
    try:
        with open(marker_path, "w", encoding="utf-8") as f:
            f.write(str(int(time.time())))
        return True
    except OSError as exc:
        app.logger.warning("could not write wipe marker: %s", exc)
        return False


@app.route("/stats/reset", methods=["POST"])
@login_required
def reset_stats():
    """Soft reset — clears the displayed Best column for every worker
    without disturbing ckpool, the worker list, hashrate history, or
    miner connections.

    Mirrors AxeBCH's reset behaviour: every worker stays visible with
    its current hashrate / share count / last-seen, but the Best column
    shows — until a fresh share lands. The next share counts as the
    new best regardless of magnitude (the dashboard tracks
    post_reset_best separately from ckpool's all-time bestshare),
    and Discord webhooks fire fresh from there.

    Pool-wide bestshare on the dashboard is computed from per-worker
    post_reset_best values, so it also goes to — until fresh shares
    arrive. ckpool's own all-time bestshare lives in pool.status and
    is unaffected — that's deliberate, since the user often wants the
    historical record preserved while the dashboard's "since reset"
    view starts fresh.

    Per-worker Reset buttons (currently behind /stats/reset with a
    name=) are deprecated in favour of a single pool-wide reset to
    keep the UI clean. The route still accepts an individual worker
    name for backward compatibility but treats it the same as a
    pool-wide reset (since in solo mode all workers share one address).
    """
    n = reset_baseline(None)

    # Clear the tailer's per-worker share-seen cache so post_reset_best
    # gets rebuilt cleanly from the next real share — otherwise an
    # in-flight share that the tailer just observed could pre-populate
    # post_reset_best before the dashboard repaints.
    with _share_seen_lock:
        _share_seen_ts.clear()

    flash(
        f"Reset complete. Best column cleared for {n} worker"
        f"{'s' if n != 1 else ''}. The next share submitted by each "
        "worker counts as the new best — Discord webhooks resume from there. "
        "Miners stay connected, no shares are lost.",
        "ok",
    )
    return redirect(url_for("index"))


@app.route("/settings", methods=["GET"])
@login_required
def settings_page():
    s = load_settings()
    min_share_raw = int(s["discord"].get("min_share") or 0)
    # Humanise per-tier min so the form shows shorthand values
    # ('400G' not '400000000000'). Index-aligned with settings.tiers.
    tier_min_h = [
        humanise_diff(int(t.get("min", 0) or 0)) if int(t.get("min", 0) or 0) > 0 else "0"
        for t in s["tiers"]
    ]
    # Serialize defaults for the JS "Reset to defaults" button. Add a
    # min_h field so the form input gets the shorthand version.
    default_tiers = []
    for t in DEFAULT_SETTINGS["tiers"]:
        default_tiers.append({
            "min_h":  humanise_diff(int(t["min"])) if t["min"] > 0 else "0",
            "emoji":  t["emoji"],
            "label":  t["label"],
            "flavor": t["flavor"],
            "color":  t["color"],
        })
    return render_template(
        "settings.html",
        settings=s,
        mindiff_h=humanise_diff(s["mindiff"]) if s["mindiff"] else "1",
        maxdiff_h=humanise_diff(s["maxdiff"]) if s["maxdiff"] else "0",
        startdiff_h=humanise_diff(s["startdiff"]) if s["startdiff"] else "1",
        min_share_h=humanise_diff(min_share_raw) if min_share_raw > 0 else "",
        tier_min_h=tier_min_h,
        default_tiers_json=json.dumps(default_tiers),
    )


@app.route("/settings/save", methods=["POST"])
@login_required
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

    # Discord min-share threshold. Empty / 0 = "fire on every new best"
    # (default behaviour). Larger values silence small-share noise.
    # parse_diff handles '1M', '500K', '2.5G' shorthand; we accept raw
    # integers too. Anything we can't parse becomes a validation error.
    min_share_raw = (request.form.get("min_share") or "").strip()
    if min_share_raw == "":
        min_share = 0
    else:
        min_share, err = parse_diff(min_share_raw)
        if err:
            errors.append(f"Discord min share: {err}")
            min_share = 0
        elif min_share is None or min_share < 0:
            errors.append("Discord min share must be ≥ 0 (use 0 to disable threshold).")
            min_share = 0

    # Tier rows come in as parallel arrays from the dynamic editor:
    # tier_min[], tier_emoji[], tier_label[], tier_flavor[], tier_color[].
    # We zip them by index, validate each row, and drop invalid ones.
    # _normalise_tiers handles sorting + level-0 fallback and falls
    # back to defaults if everything is invalid (never leaves the
    # dashboard with zero tiers).
    raw_mins    = request.form.getlist("tier_min")
    raw_emojis  = request.form.getlist("tier_emoji")
    raw_labels  = request.form.getlist("tier_label")
    raw_flavors = request.form.getlist("tier_flavor")
    raw_colors  = request.form.getlist("tier_color")
    tier_rows = []
    for i, raw_min in enumerate(raw_mins):
        raw_min = (raw_min or "").strip()
        label = (raw_labels[i] if i < len(raw_labels) else "").strip()
        if not label:
            continue  # blank rows are silently dropped (lets users delete via clear)
        # Parse min via parse_diff so '400G', '1.5M', '500000' all work.
        if raw_min == "":
            mn_val = 0
        else:
            mn_val, err = parse_diff(raw_min)
            if err:
                errors.append(f"Tier '{label}' threshold: {err}")
                continue
            if mn_val is None or mn_val < 0:
                errors.append(f"Tier '{label}' threshold must be ≥ 0.")
                continue
        tier_rows.append({
            "min":    int(mn_val),
            "emoji":  (raw_emojis[i] if i < len(raw_emojis) else "").strip(),
            "label":  label,
            "flavor": (raw_flavors[i] if i < len(raw_flavors) else "").strip(),
            "color":  (raw_colors[i]  if i < len(raw_colors)  else "").strip().lstrip("#"),
        })
    # If the user submitted no valid rows at all, fall back to current
    # tiers (so an accidental "delete all" doesn't wipe the config).
    if not tier_rows:
        tier_rows = list(s.get("tiers") or [])

    # ── auth handling ──
    # Determine target state and apply transitions:
    #   off → off  : nothing
    #   off → on   : require new password
    #   on  → on   : optional password change (require current if changing)
    #   on  → off  : require current password
    auth_was_enabled = bool(s["auth"].get("enabled") and s["auth"].get("password_hash"))
    auth_want_enabled = bool(request.form.get("auth_enabled"))
    auth_current = request.form.get("auth_current_password") or ""
    auth_new = request.form.get("auth_new_password") or ""
    auth_new_confirm = request.form.get("auth_new_password_confirm") or ""

    new_auth = dict(s["auth"])  # carries current hash by default

    if not auth_want_enabled:
        # Disable. If auth was on, require the current password so a
        # malicious actor can't silently turn auth off.
        if auth_was_enabled:
            if not auth_current:
                errors.append("Current password required to disable auth.")
            elif not check_password(auth_current):
                errors.append("Current password is incorrect.")
            else:
                new_auth["enabled"] = False
                # Keep the hash — disabling but leaving the hash means
                # re-enabling later doesn't require setting a new pw.
        else:
            # Already off, stays off. Ignore any password fields silently.
            pass
    else:
        # Want auth on.
        if auth_new or auth_new_confirm:
            # Setting/changing password
            if auth_new != auth_new_confirm:
                errors.append("New password and confirmation don't match.")
            elif len(auth_new) < 6:
                errors.append("New password must be at least 6 characters.")
            else:
                # If auth was on, require current password to change it.
                if auth_was_enabled and not check_password(auth_current):
                    errors.append("Current password is incorrect.")
                else:
                    # Hash and set
                    new_auth["password_hash"] = bcrypt.hashpw(
                        auth_new.encode("utf-8"), bcrypt.gensalt()
                    ).decode("utf-8")
                    new_auth["enabled"] = True
        else:
            # No new password. OK only if there's already a hash to use.
            if not s["auth"].get("password_hash"):
                errors.append("Set a password to enable auth.")
            else:
                new_auth["enabled"] = True

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
            "min_share": int(min_share or 0),
        },
        "tiers": _normalise_tiers(tier_rows),
        "auth": new_auth,
    }
    save_settings(new)

    # If auth password just changed, refresh our own session token so we
    # don't immediately log ourselves out by hash mismatch.
    if new_auth.get("enabled") and new_auth.get("password_hash"):
        session.permanent = True
        session["auth_ok"] = True
        session["auth_hash_token"] = _hash_token(new_auth["password_hash"])

    diff_changed = (
        new["mindiff"]   != s["mindiff"] or
        new["maxdiff"]   != s["maxdiff"] or
        new["startdiff"] != s["startdiff"]
    )
    auth_changed = (
        new_auth.get("enabled") != s["auth"].get("enabled") or
        new_auth.get("password_hash") != s["auth"].get("password_hash")
    )

    # When diff settings change, drop the marker so the host-side
    # restart watcher re-renders ckpool.conf and bounces the container.
    # Previously this was implicit because the old in-container entrypoint
    # polled for config-hash changes; the new pre-built ckpool image has
    # no such entrypoint, so the trigger has to be explicit.
    if diff_changed:
        request_service_restart("ckpool")

    msgs = []
    if diff_changed:
        msgs.append("ckpool will restart within ~10s — connected miners will reconnect automatically.")
    if auth_changed:
        if new_auth.get("enabled"):
            msgs.append("Dashboard auth is now ON.")
        else:
            msgs.append("Dashboard auth is now OFF.")
    if msgs:
        flash("Settings saved. " + " ".join(msgs), "ok")
    else:
        flash("Settings saved.", "ok")
    return redirect(url_for("settings_page"))


@app.route("/webhook/test", methods=["POST"])
@login_required
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


# ──────────────────────────── /health ────────────────────────────


@app.route("/health", methods=["GET"])
@login_required
def health_page():
    return render_template(
        "health.html",
        public=False,
        auth_enabled=auth_enabled(),
    )


@app.route("/api/health", methods=["GET"])
@login_required_json
def api_health():
    return jsonify(health_payload())


@app.route("/api/check-updates", methods=["POST"])
@login_required_json
def api_check_updates():
    """Compare the install-time commit SHA against the current head of
    the same branch on GitHub. Returns a JSON dict the dashboard renders
    inline. All failure paths return 200 with ok=False + an error
    string — never raise — because this is a best-effort feature.
    """
    meta_path = os.path.join(STATE_DIR, "install_meta.json")
    if not os.path.exists(meta_path):
        return jsonify({
            "ok": False,
            "error": "install metadata missing — re-run install.sh to enable update checks",
        })
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, ValueError) as exc:
        return jsonify({"ok": False, "error": f"could not read install metadata: {exc}"})

    installed_sha = (meta.get("sha") or "").strip()
    branch = (meta.get("branch") or "main").strip()
    repo = (meta.get("repo") or "ducksdev/ducky-installer").strip()
    if not installed_sha:
        return jsonify({
            "ok": False,
            "error": "install SHA not recorded — re-run install.sh on a network with GitHub access",
            "branch": branch,
        })

    # Hit GitHub's commits API. Unauthenticated, 60/hr per IP — enough.
    api_url = f"https://api.github.com/repos/{repo}/commits/{branch}"
    try:
        resp = requests.get(api_url, timeout=8, headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "ducky-pool-update-check",
        })
        resp.raise_for_status()
        data = resp.json()
    except requests.HTTPError as exc:
        return jsonify({
            "ok": False,
            "error": f"GitHub API error: {exc.response.status_code} {exc.response.reason}",
            "branch": branch,
        })
    except (requests.RequestException, ValueError) as exc:
        return jsonify({"ok": False, "error": f"network error: {exc}", "branch": branch})

    latest_sha = (data.get("sha") or "").strip()
    if not latest_sha:
        return jsonify({
            "ok": False,
            "error": "GitHub response missing SHA",
            "branch": branch,
        })

    up_to_date = installed_sha == latest_sha

    # Count commits behind. /compare endpoint gives total_commits.
    # Skip the call if up_to_date — saves a request and a rate slot.
    behind = 0
    if not up_to_date:
        compare_url = f"https://api.github.com/repos/{repo}/compare/{installed_sha}...{latest_sha}"
        try:
            r2 = requests.get(compare_url, timeout=8, headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "ducky-pool-update-check",
            })
            if r2.ok:
                behind = int(r2.json().get("total_commits", 0) or 0)
        except (requests.RequestException, ValueError):
            # Non-fatal — we can still tell the user there's an update.
            behind = 0

    return jsonify({
        "ok": True,
        "up_to_date": up_to_date,
        "installed_sha": installed_sha[:8],
        "latest_sha": latest_sha[:8],
        "installed_sha_full": installed_sha,
        "latest_sha_full": latest_sha,
        "branch": branch,
        "repo": repo,
        "behind": behind,
        "compare_url": (
            f"https://github.com/{repo}/compare/{installed_sha[:12]}...{latest_sha[:12]}"
            if not up_to_date else None
        ),
    })


@app.route("/api/logs/<service>", methods=["GET"])
@login_required_json
def api_logs(service: str):
    after = request.args.get("after_byte")
    after_byte: int | None
    try:
        after_byte = int(after) if after is not None else None
    except (TypeError, ValueError):
        after_byte = None
    return jsonify(tail_log(service, after_byte))


@app.route("/health/restart/<service>", methods=["POST"])
@login_required
def health_restart(service: str):
    ok, msg = request_service_restart(service)
    if ok:
        app.logger.info("restart requested for %s by user", service)
        flash(
            f"{service.capitalize()} restart requested. "
            f"It should come back online within a few seconds.",
            "ok",
        )
    else:
        flash(f"Restart failed: {msg}", "error")
    return redirect(url_for("health_page"))


_setup_web_log_file()
_start_watcher()
_start_share_tailer()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=FLASK_PORT)
