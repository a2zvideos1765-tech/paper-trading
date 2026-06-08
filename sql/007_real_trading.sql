-- 007_real_trading.sql — real-money (Angel One) trading layer.
--
-- Adds a `live` flag to portfolios (so the paper trader and the real trader
-- never touch each other's portfolios) plus the broker-side bookkeeping the
-- real trader needs: a master kill switch, an idempotent real-order ledger,
-- an order-status audit log, and periodic fund / holdings / SIP-deposit
-- snapshots pulled from Angel.
--
-- Non-destructive and idempotent. Run after sql/006_intraday_equity.sql:
--   psql -U paper -d paper_trading -f sql/007_real_trading.sql

------------------------------------------------------------------------
-- Split paper vs live portfolios. The paper trader loads WHERE enabled
-- AND NOT live; the real trader loads WHERE enabled AND live.
------------------------------------------------------------------------
ALTER TABLE portfolios
  ADD COLUMN IF NOT EXISTS live BOOLEAN NOT NULL DEFAULT FALSE;

------------------------------------------------------------------------
-- Master kill switch + bot status. Single row (id = 1). Defaults OFF:
-- deploying the code never starts live trading — the user flips it on
-- from /bot when ready.
------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS real_bot_state (
    id          INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    enabled     BOOLEAN NOT NULL DEFAULT FALSE,
    note        TEXT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by  TEXT
);
INSERT INTO real_bot_state (id, enabled, note)
VALUES (1, FALSE, 'seeded OFF by sql/007')
ON CONFLICT (id) DO NOTHING;

------------------------------------------------------------------------
-- Real-order ledger. One row per engine-intended trade the bot acted on.
-- `intent_key` is the engine dedup key (ts|symbol|side|qty|price|reason);
-- its UNIQUE constraint is the idempotency guard against double-placing
-- the same intent across ticks or after a crash.
------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS real_orders (
    id              BIGSERIAL PRIMARY KEY,
    portfolio_id    INTEGER NOT NULL REFERENCES portfolios(id) ON DELETE CASCADE,
    intent_key      TEXT NOT NULL UNIQUE,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    qty             INTEGER NOT NULL,
    order_type      TEXT NOT NULL DEFAULT 'LIMIT',       -- LIMIT (priced at the engine's decided price)
    product         TEXT NOT NULL DEFAULT 'DELIVERY',    -- Angel's CNC/delivery (multi-day swing strategy)
    requested_price NUMERIC(18, 4) NOT NULL,           -- the engine's decided trade price
    angel_order_id  TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',   -- pending|open|complete|rejected|cancelled|error
    filled_qty      INTEGER NOT NULL DEFAULT 0,
    avg_fill_price  NUMERIC(18, 4),
    charges         NUMERIC(18, 4),
    reason          TEXT NOT NULL,                     -- engine trade reason (entry_scan_11:00, target_1_tier, ...)
    error           TEXT,
    requested_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS real_orders_pid_requested_desc
    ON real_orders (portfolio_id, requested_at DESC);
CREATE INDEX IF NOT EXISTS real_orders_status
    ON real_orders (status);

------------------------------------------------------------------------
-- Append-only status audit for each real order (every Angel order-book
-- transition is logged with the raw payload for debugging).
------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS real_order_events (
    id         BIGSERIAL PRIMARY KEY,
    order_id   BIGINT NOT NULL REFERENCES real_orders(id) ON DELETE CASCADE,
    status     TEXT NOT NULL,
    raw        JSONB,
    ts         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS real_order_events_order_ts
    ON real_order_events (order_id, ts DESC);

------------------------------------------------------------------------
-- Angel fund snapshots (one per sync tick). Powers the /bot balance
-- display and feeds SIP-deposit detection (a jump in available cash not
-- explained by a sell fill is logged as a deposit).
------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS real_funds (
    id              BIGSERIAL PRIMARY KEY,
    available_cash  NUMERIC(18, 4) NOT NULL,
    net             NUMERIC(18, 4),
    utilised        NUMERIC(18, 4),
    raw             JSONB,
    as_of           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS real_funds_as_of_desc
    ON real_funds (as_of DESC);

------------------------------------------------------------------------
-- Latest Angel holdings mirror (full-replace each sync, like positions).
-- Account-level — there is one live Angel account. Used for the holdings
-- table on /bot and for engine-vs-broker reconciliation.
------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS real_holdings (
    symbol      TEXT PRIMARY KEY,
    qty         INTEGER NOT NULL,
    avg_price   NUMERIC(18, 4) NOT NULL,
    ltp         NUMERIC(18, 4),
    pnl         NUMERIC(18, 4),
    as_of       TIMESTAMPTZ NOT NULL DEFAULT now()
);

------------------------------------------------------------------------
-- Detected SIP deposits (real money the user transferred into Angel).
-- Each is an XIRR cash-flow outflow and scales the engine's deposits map.
------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS real_deposits (
    id               BIGSERIAL PRIMARY KEY,
    ts               TIMESTAMPTZ NOT NULL DEFAULT now(),
    amount           NUMERIC(18, 4) NOT NULL,
    available_before NUMERIC(18, 4),
    available_after  NUMERIC(18, 4),
    note             TEXT
);
CREATE INDEX IF NOT EXISTS real_deposits_ts_desc
    ON real_deposits (ts DESC);

------------------------------------------------------------------------
-- Seed the single live portfolio: S404 with ₹20,000, forward-only from
-- now(). The ₹5,000 SIP min-entry-cash gate is applied as an override so
-- the S404 strategy file stays verbatim and the gate is editable on /bot.
------------------------------------------------------------------------
INSERT INTO portfolios (name, strategy_id, capital, enabled, live, started_at)
VALUES ('S404_live_sip_20k', 'S404_s392_side_only', 20000, TRUE, TRUE, now())
ON CONFLICT (name) DO NOTHING;

INSERT INTO portfolio_overrides (portfolio_id, overrides, updated_at)
SELECT p.id, '{"min_entry_cash": 5000}'::jsonb, now()
  FROM portfolios p
 WHERE p.name = 'S404_live_sip_20k'
ON CONFLICT (portfolio_id) DO NOTHING;
