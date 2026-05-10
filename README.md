# 🦆 Ducky Pool

**Solo-mine Bitcoin Cash from your own hardware. One command to install, a clean dashboard, no fees, no middlemen.**

If you find a block, the entire reward lands in your wallet. If you don't, you've spent some electricity and learned how mining works. Either way, the software is free.

```bash
curl -fsSL https://raw.githubusercontent.com/ducksdev/ducky-installer/main/install.sh | sudo bash
```

Runs on any Ubuntu 24.04+ machine. Spin it up on a spare laptop, a home server, a £20-a-month VPS — wherever you want a BCH solo pool of your own.

---

## What you get

- **A full BCH node** that validates the chain independently — no trust, no third-party RPC.
- **A solo stratum pool** that hands work to your miners and submits found blocks directly to the network.
- **A web dashboard** that shows hashrate, best shares, miner status, blocks found, and per-worker progress in real time.
- **Discord webhook integration** so you get a notification the moment a worker hits a milestone share.
- **No fees, no donations skimmed, no hidden cuts.** 100% of every block reward goes to your address.
- **Donate-driven, not paywall-gated.** Every feature is free. If the project is useful to you, throw a few sats — but you're never blocked.

## Who it's for

- Hobbyists running a Bitaxe, NerdMiner, or low-power ASIC at home
- Anyone who wants to learn how a mining pool actually works under the hood
- BCH supporters who want to contribute hashrate independently
- Tinkerers who'd rather run their own node than point at someone else's

It's **not** for industrial farms — there's no SPLNS, no payout splitting, no operator dashboard. This is solo by design.

## Requirements

- Ubuntu 24.04+ (or any modern apt + systemd Linux)
- ~300 GB free disk (BCH chain is ~250 GB and growing)
- A decent internet connection for the initial sync (4-24 hours)
- One miner (Bitaxe, NerdMiner, S9, S19, etc) and a BCH wallet address
- Run as root, or with `sudo`

Open ports: `4567` (stratum, miners connect here), `4568` (web dashboard), `8333` (optional, BCH p2p — accept inbound peers).

## Using it

Right after install, the script prints two URLs:

- **Dashboard:** `http://<your-ip>:4568` — set your payout address here
- **Stratum:** `stratum+tcp://<your-ip>:4567` — point your miners at this

Set the payout address to wherever you want block rewards sent. Both formats work:

- Legacy (`1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa`)
- CashAddr (`bitcoincash:qq3557shr2wwpy2ryyswhmpwaaxnw9m7aqjhv6hgjf`)

Point your miners at the stratum URL. Worker name can be anything (it's solo — the address is what matters). Password is `x`.

The BCH node will sync first. The dashboard shows progress and tells you when mining can start. Once synced, point miners and watch shares come in.

## Manage

```bash
# status / start / stop / restart
sudo systemctl status ducky-pool
sudo systemctl restart ducky-pool
sudo systemctl stop ducky-pool

# follow logs for any container
sudo docker logs -f ducky-bchnode
sudo docker logs -f ducky-ckpool
sudo docker logs -f ducky-web

# update (re-run installer — safe, idempotent)
curl -fsSL https://raw.githubusercontent.com/ducksdev/ducky-installer/main/install.sh | sudo bash
```

The dashboard's Health page shows readiness checks for every component. If something's wrong, look there first.

## Uninstall

```bash
sudo systemctl disable --now ducky-pool
sudo rm /etc/systemd/system/ducky-pool.service
sudo systemctl daemon-reload

cd /opt/ducky-pool && sudo docker compose down -v
sudo rm -rf /opt/ducky-pool

# WARNING: this also deletes the synced chain. Only do it if you really mean it.
sudo rm -rf /var/lib/ducky-pool
```

## Layout

```
/opt/ducky-pool/                  # generated config + compose, safe to inspect
├── docker-compose.yml            #   contains a generated random RPC password
└── services/
    ├── web/                      #   the Flask dashboard
    └── ckpool/                   #   ckpool config template

/var/lib/ducky-pool/              # persistent data
├── bchnode/                      #   blockchain (~250 GB)
├── ckpool/                       #   pool logs + status
├── ckpool-config/                #   rendered ckpool.conf
├── shared/                       #   settings, baselines, history.db
└── rpc.pass                      #   generated; do not delete
```

## Customising

Set env vars before running the installer:

```bash
sudo STRATUM_PORT=3333 WEB_PORT=8080 \
    bash install.sh
```

| Variable      | Default                                  | Notes                                   |
| ------------- | ---------------------------------------- | --------------------------------------- |
| `INSTALL_DIR` | `/opt/ducky-pool`                        | Where compose + sources live            |
| `DATA_DIR`    | `/var/lib/ducky-pool`                    | Where chain + logs live                 |
| `STRATUM_PORT`| `4567`                                   | Miner-facing stratum port               |
| `WEB_PORT`    | `4568`                                   | Dashboard port                          |
| `P2P_PORT`    | `8333`                                   | BCH p2p (set to `0` to disable inbound) |
| `BCHN_IMAGE`  | `zquestz/bitcoin-cash-node:latest`       | Pin a specific tag for reproducibility  |

## How it works

Three Docker containers talk to each other:

- **`ducky-bchnode`** — Bitcoin Cash Node, the full validating node
- **`ducky-ckpool`** — solo stratum pool, hands work to miners and submits blocks
- **`ducky-web`** — Flask dashboard, reads pool stats and gives you a UI

The pool engine is built on community-maintained `ckpool` (BCH-adapted lineage). Full credits in [`ckpool/NOTICE.md`](./ckpool/NOTICE.md). The web dashboard is original work — written from scratch as a clean, minimal interface for solo miners.

## Support development

Ducky Pool is **free and open source**.

If it brings you joy, helps you find a block, or just feels useful — a few sats keeps the project going:

> **`bitcoincash:qq3557shr2wwpy2ryyswhmpwaaxnw9m7aqjhv6hgjf`**

There's a donate button in the dashboard footer too — clicks open a QR + copy-address modal so you can support from any device.

## Bugs, ideas, contact

- **Bugs:** [open an issue](https://github.com/ducksdev/ducky-installer/issues)
- **Ideas / questions:** [Discussions](https://github.com/ducksdev/ducky-installer/discussions) tab
- **The dashboard breaking?** Check the Health page first; it has self-diagnostics

## License

GPL-3.0-or-later. Inherited from upstream ckpool. See [`ckpool/NOTICE.md`](./ckpool/NOTICE.md) for the lineage and credits.

---

*Made with 🦆 by ducksdev. Powered by community-maintained ckpool.*
