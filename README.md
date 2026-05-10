# Paper Trading

Live paper-trading rig for the strategies developed in the companion
[backtester project](../i-want-to-build-an-algo). Runs multiple strategies in
parallel against live Angel One SmartAPI data, persists everything to
PostgreSQL + TimescaleDB, and serves a Kite-like, mobile-first dashboard.

- **Engine**: `src/engine/v2_engine.py` is vendored from the backtester so the
  live trader and the backtester share one codebase. Parity by construction.
- **Strategies**: drop a single file in `src/strategies/` exporting a
  `STRATEGY = StrategyV2(...)` constant. Reference its name in
  `config/portfolios.yaml`. Restart the trader. Done.
- **Process model**: PM2 manages four Python apps — `web`, `poller`, `trader`,
  `backfill`. See `ecosystem.config.js`.
- **Why a separate repo from the backtester?** The backtester is research code;
  this is a deployable rig. Different lifecycles. Engine is vendored, not
  imported.

## Architecture (one minute)

```
yourdomain ──► Caddy :443 ─► uvicorn :8000 (FastAPI dashboard)
                              │
                              ▼
                       Postgres + TimescaleDB ◄─ poller (REST every minute, market hours)
                              ▲                ◄─ trader (replay every minute, market hours)
                              └──────────────── ◄─ backfill (16:30 IST weekdays, PM2 cron)
```

The trader **replays the full vendored engine** on a 200-day rolling window of
candles every minute. New trades are diffed against `trades` table and inserted
idempotently. This is brute-force but parity-perfect; it takes ~1–2 seconds per
portfolio at our scale.

## First-time setup on the VPS

```bash
# 1. Postgres + TimescaleDB
sudo apt install -y postgresql postgresql-contrib
# Add Timescale repo per https://docs.timescale.com/install/latest/self-hosted/
sudo apt install -y timescaledb-2-postgresql-16
sudo timescaledb-tune --quiet --yes
sudo systemctl restart postgresql
sudo -u postgres psql -c "CREATE USER paper WITH PASSWORD 'change-me';"
sudo -u postgres psql -c "CREATE DATABASE paper_trading OWNER paper;"

# 2. App
git clone <your-public-repo-url> ~/paper-trading
cd ~/paper-trading
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# 3. Secrets
cp .env.example .env
$EDITOR .env   # fill in Angel creds, dashboard password, session secret

# 4. Schema
psql -U paper -d paper_trading -h 127.0.0.1 -f sql/001_schema.sql
psql -U paper -d paper_trading -h 127.0.0.1 -f sql/002_timescale.sql   # optional

# 5. One-shot history bulk-load
#    Either: copy CSVs onto the VPS first, or upload from your dev box (next section)
mkdir -p data/angel_symbols
cp ~/i-want-to-build-an-algo/data/angel_symbols/*.csv data/angel_symbols/
python -m tools.load_history --src ./data/angel_symbols --interval 5m

# 6. Caddy (or whatever proxy you use)
sudo cp Caddyfile.example /etc/caddy/Caddyfile
$EDITOR /etc/caddy/Caddyfile   # set your domain
sudo systemctl reload caddy

# 7. PM2
pm2 start ecosystem.config.js
pm2 save
pm2 startup    # enable auto-start on reboot

# 8. Verify
python -m tools.verify_setup
```

## Uploading historical CSVs from your Windows dev box

If your CSVs live on your local machine (the backtester runs there), push them
straight to the VPS Postgres over an SSH tunnel — no need to expose 5432 to the
internet. The wrapper opens the tunnel, runs the loader, tears the tunnel down:

```powershell
# from the paper-trading repo root, on Windows:
.\tools\upload_to_vps.ps1 -VpsUser ubuntu -VpsHost 203.0.113.10
# prompts for the Postgres password (paper user)

# explicit options:
.\tools\upload_to_vps.ps1 `
    -VpsUser ubuntu -VpsHost paper.studiohappens.tech `
    -Src ..\i-want-to-build-an-algo\data\angel_symbols `
    -Interval 5m `
    -PgUser paper -PgDb paper_trading
```

Requirements on the Windows side:
- OpenSSH client (built in to Windows 10/11) and an SSH key registered on the VPS
- `pip install -r requirements.txt` in a local venv (only `asyncpg`, `pandas`,
  `python-dotenv` are actually used by the loader — Angel/dashboard secrets are
  not required on the upload machine)

Re-running is safe: every row goes through `ON CONFLICT (symbol, interval, ts) DO NOTHING`.

If you'd rather skip the wrapper, you can call the loader directly with explicit
DB flags after opening your own tunnel (`ssh -L 6543:127.0.0.1:5432 ubuntu@vps`):

```powershell
$env:PG_PASSWORD = "..."
python -m tools.load_history `
    --src .\data\angel_symbols --interval 5m `
    --pg-host 127.0.0.1 --pg-port 6543 --pg-user paper --pg-db paper_trading
```

## Daily operations

```bash
pm2 status                                    # are all 4 apps healthy?
pm2 logs paperaglo-trader --lines 50          # what's the trader doing?
curl https://paper.studiohappens.tech/health | jq       # heartbeat check from anywhere
psql -U paper paper_trading -c \
  "SELECT count(*), portfolio_id FROM trades GROUP BY portfolio_id ORDER BY portfolio_id"
```

## Default portfolios

The 5 strategies in `config/portfolios.yaml` (each at ₹50k and ₹100k = 10
portfolios) are: **S6_tiered_exit**, **S14_concentrated**, **S23_s20_equity8**,
**S29_s23_sensex**, **S31_s24_persist**. Swap them after the backtest finishes
by editing the YAML and `pm2 restart paperaglo-trader`.

## Adding a new strategy

```python
# src/strategies/s99_my_idea.py
from src.engine.v2_engine import StrategyV2

STRATEGY = StrategyV2(
    name="S99_my_idea",
    fall_threshold=-0.06,
    exit_tiers=((0.20, 1.0),),
    allocation_per_trade=10000.0,
)
DESCRIPTION = "Single buy on -6%, sell all at +20%."
```

```yaml
# config/portfolios.yaml
- name: S99_my_idea_50k
  strategy: S99_my_idea
  capital: 50000
  enabled: true
```

```bash
pm2 restart paperaglo-trader   # picks up the new portfolios from YAML
```

## Testing

```bash
pytest                          # parity, executor charges, registry, smoke
python -m tools.verify_setup    # end-to-end live checks (DB, Angel, runners)
```

The parity test (`tests/test_parity.py`) is the single most important check —
it verifies the vendored engine's outputs are deterministic and structurally
correct. **Run this every time you re-vendor `src/engine/v2_engine.py`.**

## Diagnosing problems

The dashboard's `/diagnose/{portfolio_id}/{symbol}/{ts}` page shows the trade,
the surrounding candles, and the strategy parameters in effect at that moment.
For deeper triage, every runner writes JSON-line logs to `logs/{date}/{app}.log`
and PM2 captures stdout to `logs/pm2/{app}.{out,err}.log`.

`/health` returns the per-runner heartbeat freshness; the dashboard top bar
shows a red dot if any runner's last heartbeat is older than 5 minutes during
market hours.

## Re-syncing the engine from upstream

When the backtester project's `engine_v2.py` evolves, replace
`src/engine/v2_engine.py` with the new version, then re-apply the two
`# === paper-trading patch ===` blocks (the in-memory regime cache).
**Run `pytest tests/test_parity.py` to confirm the engine still produces
deterministic output before deploying.**

## Layout

```
src/
  core/        — db, angel client, config, time/IST, logging, universe loader
  engine/      — vendored engine_v2 + the live `replay` glue
  strategies/  — one file per strategy + auto-discovering registry
  runners/     — poller, trader, backfill, web (uvicorn)
  web/         — FastAPI app, routes, templates, static
sql/           — 001_schema.sql + 002_timescale.sql
config/        — universe.yaml, portfolios.yaml
tools/         — load_history.py, verify_setup.py
tests/         — parity, executor, registry
```
