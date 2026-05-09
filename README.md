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

# 5. One-shot history bulk-load (copy CSVs from the backtester project first)
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

## Daily operations

```bash
pm2 status                                    # are all 4 apps healthy?
pm2 logs paperaglo-trader --lines 50          # what's the trader doing?
curl https://paper.studiohappens.tech/health | jq       # heartbeat check from anywhere
psql -U paper paper_trading -c \
  "SELECT count(*), portfolio_id FROM trades GROUP BY portfolio_id ORDER BY portfolio_id"
```

## Adding a new strategy

```python
# src/strategies/s14_concentrated.py
from src.engine.v2_engine import StrategyV2

STRATEGY = StrategyV2(
    name="S14_concentrated",
    fall_threshold=-0.05,
    exit_tiers=((0.35, 1.0),),
    max_new_buys_per_day=1,
    allocation_per_trade=25000.0,
)
DESCRIPTION = "Max 1 buy/day on deepest drop, ₹25k allocation, +35% exit."
```

```yaml
# config/portfolios.yaml
- name: S14_concentrated_50k
  strategy: S14_concentrated
  capital: 50000
  enabled: true
- name: S14_concentrated_100k
  strategy: S14_concentrated
  capital: 100000
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
