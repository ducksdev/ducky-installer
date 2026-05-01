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
WORKERS_DIR = os.environ.get("WORKERS_DIR", "/pool-logs/workers")
FLASK_PORT = int(os.environ.get("FLASK_PORT", "4568"))

STATE_DIR = os.environ.get("STATE_DIR", "/shared")
SETTINGS_FILE = os.path.join(STATE_DIR, "settings.json")
BASELINE_FILE = os.path.join(STATE_DIR, "best_baselines.json")

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
WORKER_FILENAME_RE = re.compile(r"^[A-Za-z0-9]+\.[A-Za-z0-9_\-]+$")

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


def reset_baseline(worker_id: str | None) -> int:
    with _state_lock:
        data = _load_baselines()
        workers = data["workers"]
        targets = list(workers.keys()) if worker_id in (None, "__pool__") else [worker_id]

        current = {}
        for wid in targets:
            path = os.path.join(WORKERS_DIR, wid)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    d = _load_json_loose(f.read()) or {}
                current[wid] = float(d.get("bestshare", 0) or d.get("bestever", 0) or 0)
            except OSError:
                current[wid] = workers.get(wid, {}).get("best", 0)

        for wid in targets:
            workers[wid] = {"best": current.get(wid, 0), "last_sent_ts": 0}

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
            "id": fname,
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

    rank = {"online": 0, "stale": 1, "offline": 2}
    out.sort(key=lambda w: (rank.get(w["status"], 3), w["age_s"] if w["age_s"] is not None else 1e12))
    return out


# ──────────────────────────── webhook ────────────────────────────

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
            if not WORKER_FILENAME_RE.match(fname):
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = _load_json_loose(f.read()) or {}
            except OSError:
                continue
            try:
                current = float(data.get("bestshare", 0) or data.get("bestever", 0) or 0)
            except (TypeError, ValueError):
                continue
            if current <= 0:
                continue

            entry = workers.get(fname)
            if entry is None:
                workers[fname] = {"best": current, "last_sent_ts": 0}
                changed = True
                continue

            baseline = float(entry.get("best", 0))
            last_sent = int(entry.get("last_sent_ts", 0))

            if current > baseline and (now_ts - last_sent) >= WEBHOOK_MIN_INTERVAL:
                worker_name = fname.split(".", 1)[1] if "." in fname else fname
                ok, _msg = _post_discord(
                    content=None,
                    embeds=[{
                        "title": "🦆 New best share!",
                        "color": 0xF9A825,
                        "fields": [
                            {"name": "Worker", "value": f"`{worker_name}`", "inline": True},
                            {"name": "Best share", "value": humanise_diff(current), "inline": True},
                            {"name": "Previous", "value": humanise_diff(baseline), "inline": True},
                            {"name": "1h hashrate", "value": str(data.get("hashrate1hr", "—")), "inline": True},
                        ],
                        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_ts)),
                    }],
                )
                workers[fname] = {
                    "best": current,
                    "last_sent_ts": now_ts if ok else last_sent,
                }
                changed = True
            elif current > baseline:
                workers[fname] = {"best": current, "last_sent_ts": last_sent}
                changed = True

        if changed:
            _save_baselines(state)


def _watcher_loop() -> None:
    while True:
        try:
            _check_and_fire(int(time.time()))
        except Exception as exc:  # noqa: BLE001
            app.logger.exception("watcher tick failed: %s", exc)
        time.sleep(WATCHER_INTERVAL)


def _start_watcher() -> None:
    t = threading.Thread(target=_watcher_loop, name="best-share-watcher", daemon=True)
    t.start()


# ──────────────────────────── routes ────────────────────────────

@app.route("/", methods=["GET"])
def index():
    settings = load_settings()
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
        webhook_set=bool(settings["discord"]["webhook_url"]),
    )


@app.route("/api/stats", methods=["GET"])
def api_stats():
    s = load_settings()
    return jsonify({
        "node": get_node_status(),
        "pool": get_pool_stats(),
        "workers": get_workers(),
        "payout": read_payout(),
        "webhook_set": bool(s["discord"]["webhook_url"]),
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
    if not name or not WORKER_FILENAME_RE.match(name):
        abort(400, "invalid worker name")
    target = os.path.join(WORKERS_DIR, name)
    real_target = os.path.realpath(target)
    real_dir = os.path.realpath(WORKERS_DIR)
    if not real_target.startswith(real_dir + os.sep):
        abort(400, "invalid worker path")
    try:
        os.remove(real_target)
        with _state_lock:
            state = _load_baselines()
            if state["workers"].pop(name, None) is not None:
                _save_baselines(state)
        flash(f"Forgot worker {name.split('.', 1)[-1]}.", "ok")
    except FileNotFoundError:
        flash("Worker already gone.", "ok")
    except OSError as exc:
        flash(f"Could not remove worker file: {exc}", "error")
    return redirect(url_for("index"))


@app.route("/best/reset", methods=["POST"])
def reset_best():
    name = (request.form.get("name") or "").strip()
    if name == "__pool__":
        n = reset_baseline("__pool__")
        flash(f"Best-share baseline reset for all {n} worker(s).", "ok")
    else:
        if not WORKER_FILENAME_RE.match(name):
            abort(400, "invalid worker name")
        reset_baseline(name)
        wname = name.split(".", 1)[1] if "." in name else name
        flash(f"Reset baseline for {wname}.", "ok")
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
    ok, msg = _post_discord(
        content=None,
        embeds=[{
            "title": "🦆 Ducky Pool test webhook",
            "description": "If you can see this, Ducky Pool can reach Discord.",
            "color": 0xF9A825,
        }],
    )
    flash("Test webhook sent." if ok else f"Test failed: {msg}", "ok" if ok else "error")
    return redirect(url_for("settings_page"))


_start_watcher()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=FLASK_PORT)
