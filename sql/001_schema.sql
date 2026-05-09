-- Paper-trading schema. Run once after creating the database:
--   psql -U paper -d paper_trading -f sql/001_schema.sql
--
-- Idempotent: every CREATE uses IF NOT EXISTS so re-running is safe.

------------------------------------------------------------------------
-- Price history. Every minute the poller upserts the latest bar here.
-- The trader reads a rolling window from this table and replays the
-- vendored engine_v2 against it.
------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS candles (
    symbol      TEXT        NOT NULL,
    interval    TEXT        NOT NULL,        -- '1m' | '5m' | '1d'
    ts          TIMESTAMPTZ NOT NULL,
    open        NUMERIC(18, 4) NOT NULL,
    high        NUMERIC(18, 4) NOT NULL,
    low         NUMERIC(18, 4) NOT NULL,
    close       NUMERIC(18, 4) NOT NULL,
    volume      BIGINT NOT NULL,
    PRIMARY KEY (symbol, interval, ts)
);
CREATE INDEX IF NOT EXISTS candles_symbol_interval_ts_desc
    ON candles (symbol, interval, ts DESC);

------------------------------------------------------------------------
-- One row per paper portfolio. Bootstrapped from config/portfolios.yaml
-- on trader startup (UPSERT by name).
------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS portfolios (
    id           SERIAL PRIMARY KEY,
    name         TEXT UNIQUE NOT NULL,        -- e.g. 'S1_user_pyramid_50k'
    strategy_id  TEXT NOT NULL,               -- key into strategies registry
    capital      NUMERIC(18, 2) NOT NULL,
    enabled      BOOLEAN NOT NULL DEFAULT TRUE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

------------------------------------------------------------------------
-- Current open positions per portfolio. Written by the trader after
-- each replay; mirrors engine_v2's holdings dict so the dashboard can
-- read state without re-running the engine.
------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS positions (
    portfolio_id      INTEGER NOT NULL REFERENCES portfolios(id) ON DELETE CASCADE,
    symbol            TEXT NOT NULL,
    qty               INTEGER NOT NULL,
    avg_price         NUMERIC(18, 4) NOT NULL,
    entry_price       NUMERIC(18, 4) NOT NULL,
    entry_date        DATE NOT NULL,
    peak_price        NUMERIC(18, 4) NOT NULL,
    pyramid_adds_hit  INTEGER NOT NULL DEFAULT 0,
    tiers_hit         INTEGER NOT NULL DEFAULT 0,
    trail_armed       BOOLEAN NOT NULL DEFAULT FALSE,
    entry_atr         NUMERIC(18, 4),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (portfolio_id, symbol)
);

------------------------------------------------------------------------
-- Append-only trade ledger. The trader replays the engine on each tick
-- and INSERTs new trades with ON CONFLICT DO NOTHING; the unique index
-- below makes each engine-emitted trade idempotent.
------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS trades (
    id            BIGSERIAL PRIMARY KEY,
    portfolio_id  INTEGER NOT NULL REFERENCES portfolios(id) ON DELETE CASCADE,
    symbol        TEXT NOT NULL,
    side          TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    qty           INTEGER NOT NULL,
    price         NUMERIC(18, 4) NOT NULL,
    turnover      NUMERIC(18, 4) NOT NULL,
    charges       NUMERIC(18, 4) NOT NULL,
    cash_after    NUMERIC(18, 4) NOT NULL,
    ts            TIMESTAMPTZ NOT NULL,
    reason        TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS trades_portfolio_dedupe
    ON trades (portfolio_id, ts, symbol, side, qty, price, reason);
CREATE INDEX IF NOT EXISTS trades_portfolio_ts_desc
    ON trades (portfolio_id, ts DESC);

------------------------------------------------------------------------
-- Daily equity snapshot per portfolio. One row per trading day per
-- portfolio (UPSERT). The dashboard's equity curve queries this.
------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS equity_snapshots (
    portfolio_id    INTEGER NOT NULL REFERENCES portfolios(id) ON DELETE CASCADE,
    ts              TIMESTAMPTZ NOT NULL,
    cash            NUMERIC(18, 4) NOT NULL,
    holdings_value  NUMERIC(18, 4) NOT NULL,
    equity          NUMERIC(18, 4) NOT NULL,
    open_positions  INTEGER NOT NULL,
    PRIMARY KEY (portfolio_id, ts)
);

------------------------------------------------------------------------
-- Process heartbeats — the dashboard's /health endpoint reads this and
-- the top bar shows a red dot if any runner's last_beat is stale.
------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS runs (
    app          TEXT PRIMARY KEY,            -- 'poller' | 'trader' | 'backfill'
    last_beat    TIMESTAMPTZ NOT NULL,
    status       TEXT NOT NULL,               -- 'ok' | 'error' | 'sleeping'
    detail       TEXT
);
