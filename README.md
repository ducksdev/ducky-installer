# Ducky Pool

Bitcoin Cash full node + solo Stratum pool + web dashboard, in one stack.
Runs anywhere Ubuntu 24.04+ runs.

## Install

On a fresh Ubuntu 24.04+ host:

```bash
curl -fsSL https://raw.githubusercontent.com/ducksdev/ducky-installer/main/install.sh | sudo bash
```

That's it. The installer:

1. Installs Docker if you don't have it
2. Drops the stack into `/opt/ducky-pool`
3. Stores chain data and logs under `/var/lib/ducky-pool`
4. Generates a strong random RPC password
5. Builds the web + ckpool images
6. Starts everything via Docker Compose
7. Installs a systemd unit so it boots with the host

When it finishes you'll see a URL for the dashboard and a stratum URL for miners.

## Requirements

- Ubuntu 24.04+ (or anything with apt + systemd)
- ~300 GB free disk for the BCH chain
- Open ports: `4567` (stratum), `4568` (web UI), `8333` (BCH p2p, optional)
- Run as root or with `sudo`

## Use

After install, open `http://<your-host>:4568` and set a BCH payout address.
Both legacy (`1...` / `3...`) and CashAddr (`q...` / `bitcoincash:q...`) are accepted.

Point miners at `stratum+tcp://<your-host>:4567`. Worker name doesn't matter
(it's solo, the address is what counts). Password `x` is fine.

The BCH node will sync first (~250 GB, hours-to-days). The dashboard shows
progress.

## Layout

```
/opt/ducky-pool/                      # config, compose file, build contexts
├── docker-compose.yml                #   generated, contains RPC password
└── services/
    ├── web/                          #   Flask dashboard
    └── ckpool/                       #   ckpool-solo, BCH-aware

/var/lib/ducky-pool/                  # all persistent data
├── bchnode/                          #   blockchain (~250 GB)
├── ckpool/                           #   pool logs + status
├── shared/                           #   payout address, exchanged web↔ckpool
└── rpc.pass                          #   generated, do not delete
```

## Manage

```bash
sudo systemctl status ducky-pool
sudo systemctl restart ducky-pool
sudo systemctl stop ducky-pool

# logs
sudo docker logs -f ducky-bchnode
sudo docker logs -f ducky-ckpool
sudo docker logs -f ducky-web

# update (re-run installer; safe and idempotent)
curl -fsSL https://raw.githubusercontent.com/ducksdev/ducky-installer/main/install.sh | sudo bash
```

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

## Customising

Override defaults with env vars before running the installer:

```bash
sudo STRATUM_PORT=3333 WEB_PORT=8080 BCHN_IMAGE=zquestz/bitcoin-cash-node:29.0.0 \
    bash install.sh
```

| Variable      | Default                                  | Notes                                  |
| ------------- | ---------------------------------------- | -------------------------------------- |
| `INSTALL_DIR` | `/opt/ducky-pool`                        | Where compose + sources live           |
| `DATA_DIR`    | `/var/lib/ducky-pool`                    | Where chain + logs live                |
| `STRATUM_PORT`| `4567`                                   | Miner-facing stratum port              |
| `WEB_PORT`    | `4568`                                   | Dashboard port                         |
| `P2P_PORT`    | `8333`                                   | BCH p2p (set to `0` to disable inbound)|
| `BCHN_IMAGE`  | `zquestz/bitcoin-cash-node:latest`       | Pin a specific tag for reproducibility |
| `REPO_RAW`    | `https://raw.githubusercontent.com/...`  | Where the installer fetches sources    |
