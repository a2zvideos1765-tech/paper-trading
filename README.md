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

### Candle interval — one source of truth

Everything that touches the `candles` table speaks **5-minute bars** for equities.
The engine was tuned on 5m, the historical CSVs are 5m, the live poller fetches
5m, the trader queries 5m. Mixing intervals would make the trader blind during
market hours (it would query `5m` and only see yesterday's backfill).

| Producer | Interval written | Where it's pinned |
|---|---|---|
| `tools/load_history.py` (CSV bulk-load) | `5m` (default `--interval`) | [load_history.py:103](tools/load_history.py) |
| `runners/poller.py` (live, every minute) | `5m` | [poller.py INTERVAL](src/runners/poller.py) |
| `runners/backfill.py` (nightly) | `5m` and `1m` for equities; `1d` for indices | [backfill.py EQUITY_INTERVALS](src/runners/backfill.py) |
| `runners/trader.py` (engine replay) | reads `5m` | [trader.py CANDLE_INTERVAL](src/runners/trader.py) |
| `web/routes/health.py` (freshness check) | reads `5m` | [health.py](src/web/routes/health.py) |

Why does the backfill *also* keep `1m`? Free side effect — it costs nothing
extra at backfill time and gives the dashboard the option to render a finer
candle chart later. The trader and engine never look at it. **Don't** wire it
into the trader without re-tuning every strategy first.

If you ever do want to switch the engine to 1-minute bars: change all four
pinned values together, re-run the full backtest grid to re-pick strategies,
then redeploy. There is no path that keeps both intervals live in the engine.

---

## First-time setup on the VPS

> **Before you start**: you need SSH access to your VPS (Ubuntu 22.04 or 24.04
> recommended). Log in with `ssh ubuntu@your-server-ip` and follow these steps
> one section at a time. Each section ends with a quick sanity check so you
> know it worked before moving on.

---

### Step 1 — Install PostgreSQL and TimescaleDB

PostgreSQL is the database. TimescaleDB is a PostgreSQL extension that makes
time-series queries (like "last 200 days of candles") very fast. We need both.

```bash
# Install PostgreSQL 16 and its contrib modules (required by TimescaleDB)
sudo apt update
sudo apt install -y postgresql postgresql-contrib
```

> **What to expect**: apt will download and install several packages. At the
> end you should see "Setting up postgresql-16 ..." without any errors.

Next, add the TimescaleDB repository and install the extension. The exact
commands are on the [TimescaleDB install page](https://docs.timescale.com/install/latest/self-hosted/).
For Ubuntu 22.04/24.04 it looks like this:

```bash
# Add the TimescaleDB apt repository
echo "deb https://packagecloud.io/timescale/timescaledb/ubuntu/ $(lsb_release -c -s) main" \
  | sudo tee /etc/apt/sources.list.d/timescaledb.list
curl -L https://packagecloud.io/timescale/timescaledb/gpgkey | sudo apt-key add -
sudo apt update

# Install the extension matching your PostgreSQL version
sudo apt install -y timescaledb-2-postgresql-16

# Let TimescaleDB tune PostgreSQL memory settings for your server
sudo timescaledb-tune --quiet --yes

# Restart PostgreSQL so the tuning takes effect
sudo systemctl restart postgresql
```

> **Sanity check**: run `sudo systemctl status postgresql` — it should say
> "active (running)". If it says "failed", run
> `sudo journalctl -u postgresql -n 40` to see what went wrong.

Now create the database user and the database itself:

```bash
# Create a Postgres user called "paper" with a password you choose
# (replace 'change-me' with something real — you'll put this in .env later)
sudo -u postgres psql -c "CREATE USER paper WITH PASSWORD 'change-me';"

# Create the database, owned by that user
sudo -u postgres psql -c "CREATE DATABASE paper_trading OWNER paper;"
```

> **Sanity check**: run `psql -U paper -d paper_trading -h 127.0.0.1 -c "\l"`
> — it should list `paper_trading` without asking for credentials if you set
> `PG_PASSWORD` in your shell, or prompt for the password you set above.

---

### Step 2 — Clone the repo and install Python dependencies

```bash
# Clone the repo into your home directory
git clone <your-public-repo-url> ~/paper-trading

# Move into the project folder — all future commands assume you're here
cd ~/paper-trading

# Create a Python virtual environment so dependencies don't conflict
# with anything else on the server
python3 -m venv .venv

# Activate the virtual environment
# (you'll need to run this again whenever you open a new SSH session)
source .venv/bin/activate

# Install all required Python packages
pip install -r requirements.txt
```

> **What to expect**: pip will download and install ~20 packages. The last line
> should be "Successfully installed ...". If you see a red error about a missing
> system library, install it with `sudo apt install <libname>-dev` and retry.

> **Note**: the virtual environment stays on the server. PM2 is configured in
> `ecosystem.config.js` to use `.venv/bin/python` automatically, so you don't
> need to activate it for the running services — only for manual commands.

---

### Step 3 — Fill in your secrets

The app reads all credentials from a `.env` file. We ship an example file with
placeholder values; you copy it and fill in the real values.

```bash
# Copy the example file
cp .env.example .env

# Open it in a text editor (nano is easiest if you're not familiar with vim)
nano .env
```

The file looks like this — fill in every value marked `CHANGE_ME`:

```
# Angel One credentials (from your SmartAPI dashboard at smartapi.angelbroking.com)
ANGEL_API_KEY=CHANGE_ME
ANGEL_CLIENT_CODE=CHANGE_ME        # your Angel One login ID
ANGEL_PASSWORD=CHANGE_ME           # your Angel One login password
ANGEL_TOTP_SECRET=CHANGE_ME        # the TOTP secret (not the OTP code itself)

# Database (use the password you set in Step 1)
PG_HOST=127.0.0.1
PG_PORT=5432
PG_DB=paper_trading
PG_USER=paper
PG_PASSWORD=CHANGE_ME

# Dashboard login (pick any password — this is what you type at /login)
DASHBOARD_PASSWORD=CHANGE_ME

# A long random string used to sign session cookies
# Generate one with: python3 -c "import secrets; print(secrets.token_hex(32))"
SESSION_SECRET=CHANGE_ME
```

> **How to get the TOTP secret**: in the Angel One app, when you enable TOTP
> 2FA, you're shown a QR code and also a text string (looks like
> `JBSWY3DPEHPK3PXP`). That text string is the `ANGEL_TOTP_SECRET`. Save it
> now — you can't retrieve it later without resetting 2FA.

Save and close the file (in nano: `Ctrl+O`, `Enter`, `Ctrl+X`).

> **Security note**: `.env` is in `.gitignore` so it will never be committed to
> the public repo. Never share this file or print its contents in logs.

---

### Step 4 — Apply the database schema

Now we create all the tables, indexes, and TimescaleDB hypertables. This is a
one-time step (re-running is safe because all statements use `IF NOT EXISTS`).

```bash
# Create tables, indexes, and the runs/trades/positions/signals tables
psql -U paper -d paper_trading -h 127.0.0.1 -f sql/001_schema.sql

# Convert the candles and equity_snapshots tables into TimescaleDB hypertables
# (makes time-range queries much faster)
psql -U paper -d paper_trading -h 127.0.0.1 -f sql/002_timescale.sql
```

> Both commands will prompt for the `paper` user's password unless you set
> `export PGPASSWORD=your-password` first.

> **Sanity check**: after running both files, run:
> ```bash
> psql -U paper -d paper_trading -h 127.0.0.1 -c "\dt"
> ```
> You should see tables: `candles`, `equity_snapshots`, `portfolios`, `positions`,
> `runs`, `signals`, `trades`.

---

### Step 5 — Load historical price data

The strategies need ~200 days of 5-minute candles to work. You have two options:

**Option A — Upload from your Windows dev box** (recommended if the CSVs are on
your local machine — see the next section for full details):

```powershell
# Run this on your Windows machine, from the paper-trading folder
.\tools\upload_to_vps.ps1 -VpsUser ubuntu -VpsHost your-server-ip
```

**Option B — Copy the CSVs to the VPS first, then load**:

```bash
# If the backtester repo is also on the VPS:
mkdir -p data/angel_symbols
cp ~/i-want-to-build-an-algo/data/angel_symbols/*.csv data/angel_symbols/

# Then load (takes a few minutes for a full history load)
python -m tools.load_history --src ./data/angel_symbols --interval 5m
```

> **What to expect**: the loader prints one line per CSV file, e.g.
> `Loaded RELIANCE.csv → 48230 rows upserted`. Running it again is safe —
> duplicate rows are silently ignored (`ON CONFLICT DO NOTHING`).

> **How long does it take?** With ~200 symbols and 200 days of 5m bars each,
> expect 3–8 minutes depending on your server's disk speed.

---

### Step 6 — Configure Caddy (HTTPS reverse proxy)

Caddy automatically gets a free TLS certificate from Let's Encrypt and forwards
HTTPS traffic to the FastAPI app running on port 8000.

```bash
# Copy the example Caddy config
sudo cp Caddyfile.example /etc/caddy/Caddyfile

# Edit it to set your real domain name
sudo nano /etc/caddy/Caddyfile
```

Change `paper.yourdomain.com` to your actual domain (the one whose DNS A-record
points to this server's IP). Save and reload:

```bash
sudo systemctl reload caddy
```

> **Prerequisite**: Caddy must already be installed (`sudo apt install caddy`
> if not). Your domain's DNS A record must point to this server's public IP
> before the certificate can be issued — Let's Encrypt verifies it.

> **Sanity check**: `curl -I https://paper.yourdomain.com/health` should return
> `HTTP/2 200` within 30 seconds of the first request (that's when Caddy fetches
> the certificate). If it times out, check that port 443 is open in your
> firewall (`sudo ufw allow 443`).

---

### Step 7 — Start everything with PM2

PM2 is a process manager that keeps all four apps running, restarts them if they
crash, and starts them automatically after a server reboot.

```bash
# Start all four apps (web, poller, trader, backfill) defined in ecosystem.config.js
pm2 start ecosystem.config.js

# Save the current process list so PM2 remembers it after a reboot
pm2 save

# Register PM2 to start on boot (the command prints one more command — run it too)
pm2 startup
```

> **What `pm2 startup` prints**: something like
> `sudo env PATH=$PATH:... pm2 startup systemd -u ubuntu --hp /home/ubuntu`
> Copy and run that exact command — it registers the systemd service.

> **Sanity check**: run `pm2 status`. You should see four rows, all with
> status `online`:
> ```
> ┌─────────────────────┬────────┬─────────┐
> │ name                │ status │ uptime  │
> ├─────────────────────┼────────┼─────────┤
> │ paperaglo-web       │ online │ 10s     │
> │ paperaglo-poller    │ online │ 10s     │
> │ paperaglo-trader    │ online │ 10s     │
> │ paperaglo-backfill  │ online │ 10s     │
> └─────────────────────┴────────┴─────────┘
> ```
> If any app shows `errored`, run `pm2 logs <appname> --lines 30` to see why.

---

### Step 8 — Verify everything works

```bash
# Run the automated verification script (works even when markets are closed)
python -m tools.verify_setup
```

This script checks:
- Database is reachable and all tables exist
- Angel One credentials are valid (login attempt, no candle fetch needed)
- All symbols in `universe.yaml` have a known Angel token
- The `/health` endpoint returns 200
- The dashboard login page works

> **If a check fails**, the script tells you which one and why. Fix it, then
> re-run — it's safe to run repeatedly.

---

## Uploading historical CSVs from your Windows dev box

If your CSVs live on your local machine (the backtester runs there), push them
straight to the VPS Postgres over an SSH tunnel — no need to expose port 5432
to the internet. The wrapper opens the tunnel, runs the loader, tears the
tunnel down automatically.

```powershell
# From the paper-trading repo root on your Windows machine:
.\tools\upload_to_vps.ps1 -VpsUser ubuntu -VpsHost 203.0.113.10
# It will prompt once for your Postgres password (the "paper" user's password)

# With all options spelled out:
.\tools\upload_to_vps.ps1 `
    -VpsUser ubuntu -VpsHost paper.studiohappens.tech `
    -Src ..\i-want-to-build-an-algo\data\angel_symbols `
    -Interval 5m `
    -PgUser paper -PgDb paper_trading
```

**What this script does, step by step:**
1. Opens a background SSH connection that forwards `localhost:6543` on your
   Windows machine to `127.0.0.1:5432` on the VPS (so Postgres is reachable
   locally without being exposed to the internet).
2. Runs `python -m tools.load_history` pointing at that local port.
3. Kills the SSH tunnel when the load is done (or if it errors).

**Requirements on the Windows side:**
- OpenSSH client — built into Windows 10 and 11. Open PowerShell and run
  `ssh -V` to confirm.
- An SSH key registered on the VPS. If you normally log in with a password,
  run `ssh-keygen` on Windows, then `ssh-copy-id ubuntu@your-server-ip`.
- Python + `pip install -r requirements.txt` in a local venv — only `asyncpg`,
  `pandas`, and `python-dotenv` are used by the loader. You do **not** need
  Angel API keys or dashboard secrets on your upload machine.

**Re-running is safe**: every row goes through `ON CONFLICT DO NOTHING`, so
duplicate candles are silently skipped.

If you'd rather skip the wrapper and manage the tunnel yourself:

```powershell
# Open the tunnel in a separate PowerShell window:
ssh -L 6543:127.0.0.1:5432 ubuntu@your-server-ip

# Then in your main window:
$env:PG_PASSWORD = "your-paper-user-password"
python -m tools.load_history `
    --src .\data\angel_symbols --interval 5m `
    --pg-host 127.0.0.1 --pg-port 6543 --pg-user paper --pg-db paper_trading
```

---

## Daily operations

These are the commands you'll use most often once the system is running.

```bash
# Are all four apps healthy?
pm2 status

# Watch live logs for a specific app (Ctrl+C to stop)
pm2 logs paperaglo-trader --lines 50
pm2 logs paperaglo-poller --lines 50

# Quick heartbeat check from anywhere (works from your laptop too)
curl https://paper.studiohappens.tech/health | python3 -m json.tool

# How many trades per portfolio?
psql -U paper paper_trading -c \
  "SELECT count(*), portfolio_id FROM trades GROUP BY portfolio_id ORDER BY portfolio_id"

# Restart a single app after editing its config (e.g. adding a strategy)
pm2 restart paperaglo-trader

# Restart everything (e.g. after a server update)
pm2 reload ecosystem.config.js
```

---

## Default portfolios

The 5 strategies in `config/portfolios.yaml` (each at ₹50k and ₹100k = 10
portfolios) are: **S6_tiered_exit**, **S14_concentrated**, **S23_s20_equity8**,
**S29_s23_sensex**, **S31_s24_persist**. Swap them after the backtest finishes
by editing the YAML and `pm2 restart paperaglo-trader`.

---

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

---

## Testing

```bash
pytest                          # parity, executor charges, registry, smoke
python -m tools.verify_setup    # end-to-end live checks (DB, Angel, runners)
```

The parity test (`tests/test_parity.py`) is the single most important check —
it verifies the vendored engine's outputs are deterministic and structurally
correct. **Run this every time you re-vendor `src/engine/v2_engine.py`.**

---

## Diagnosing problems

### Dashboard indicators
The dashboard top bar shows a red dot if any runner's heartbeat is older than
5 minutes during market hours. Click it to go to `/health` and see which runner
is stale.

### Per-trade replay
`/diagnose/{portfolio_id}/{symbol}/{ts}` shows the trade, the surrounding
candles, and the strategy parameters in effect at that moment. Use this to
answer "why did the strategy buy RELIANCE at 11:23?"

### Log files
Every runner writes structured JSON logs to `logs/{date}/{app}.log`. PM2
captures stdout/stderr to `logs/pm2/{app}.{out,err}.log`.

Useful patterns:

```bash
# See the last 50 lines from the trader
pm2 logs paperaglo-trader --lines 50

# Search for errors in today's trader log
grep '"level":"error"' logs/$(date +%Y-%m-%d)/trader.log | tail -20

# Check if the poller is actually writing candles (should update every minute)
psql -U paper paper_trading -c \
  "SELECT max(ts) FROM candles WHERE interval = '5m'"
```

### Common issues

| Symptom | Likely cause | Fix |
|---|---|---|
| Trader shows no new trades during market hours | Poller not writing candles | Check `pm2 logs paperaglo-poller` for Angel auth errors |
| Angel login fails at startup | TOTP secret wrong or expired | Verify `ANGEL_TOTP_SECRET` in `.env`; it must be the base32 seed, not a generated OTP |
| Dashboard shows "db error" on `/health` | Wrong `PG_*` env vars or Postgres not running | `sudo systemctl status postgresql` and check `.env` |
| `pm2 status` shows `errored` for an app | Python crash at startup | `pm2 logs <appname> --lines 50` to see the traceback |
| Caddy returns 502 on first request | Web app not started or crashed | `pm2 status` and `pm2 logs paperaglo-web` |

---

## Re-syncing the engine from upstream

When the backtester project's `engine_v2.py` evolves, replace
`src/engine/v2_engine.py` with the new version, then re-apply the two
`# === paper-trading patch ===` blocks (the in-memory regime cache).
**Run `pytest tests/test_parity.py` to confirm the engine still produces
deterministic output before deploying.**

---

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
