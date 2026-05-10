-- Phase 2 schema: editable universe + per-portfolio strategy overrides + backfill queue.
-- Run after 001_schema.sql + 002_timescale.sql:
--   psql -U paper -d paper_trading -f sql/003_universe.sql
--
-- Idempotent: every CREATE uses IF NOT EXISTS so re-running is safe.

------------------------------------------------------------------------
-- Snapshot of Angel One's instrument master. Refreshed weekly by the
-- paperaglo-instruments PM2 job (or manually via the dashboard button).
-- ~80k rows; the symbols page typeahead searches this table.
------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS instruments (
    token            TEXT PRIMARY KEY,
    symbol           TEXT NOT NULL,                 -- "RELIANCE-EQ"
    name             TEXT,                          -- "Reliance Industries Limited"
    exchange         TEXT NOT NULL,                 -- NSE | BSE | NFO | ...
    segment          TEXT,
    instrument_type  TEXT,                          -- EQ | FUT | OPT | INDEX | ...
    lot_size         INTEGER,
    tick_size        NUMERIC(18, 4),
    expiry           DATE,
    refreshed_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS instruments_symbol_lower   ON instruments (lower(symbol));
CREATE INDEX IF NOT EXISTS instruments_name_lower     ON instruments (lower(name));
CREATE INDEX IF NOT EXISTS instruments_exchange_type  ON instruments (exchange, instrument_type);

------------------------------------------------------------------------
-- The user-curated watch list, edited via /symbols. The poller and
-- trader read from this table at the start of each cycle, so add/remove
-- takes effect within ~60s — no restart needed.
------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS universe_symbols (
    symbol     TEXT NOT NULL,
    exchange   TEXT NOT NULL,
    token      TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'equity',      -- 'equity' | 'index'
    enabled    BOOLEAN NOT NULL DEFAULT TRUE,
    added_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, exchange)
);
CREATE INDEX IF NOT EXISTS universe_symbols_enabled ON universe_symbols (enabled, kind);

------------------------------------------------------------------------
-- Per-portfolio strategy parameter overrides. Empty / missing row =
-- portfolio uses the strategy file's defaults. The trader applies these
-- via dataclasses.replace() at the start of each replay, after type
-- coercion + semantic validation.
------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS portfolio_overrides (
    portfolio_id INTEGER PRIMARY KEY REFERENCES portfolios(id) ON DELETE CASCADE,
    overrides    JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

------------------------------------------------------------------------
-- Backfill queue. When the user adds a new symbol with "Backfill 200
-- days" checked, the API enqueues a row here and returns immediately.
-- The paperaglo-backfill-queue PM2 job drains it overnight, paced to
-- stay well below Angel's rate limit.
------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS backfill_queue (
    id          BIGSERIAL PRIMARY KEY,
    symbol      TEXT NOT NULL,
    exchange    TEXT NOT NULL,
    token       TEXT NOT NULL,
    interval    TEXT NOT NULL,                      -- '5m' | '1d' | ...
    days        INTEGER NOT NULL DEFAULT 200,
    state       TEXT NOT NULL DEFAULT 'pending',    -- pending | running | done | error
    error       TEXT,
    enqueued_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at  TIMESTAMPTZ,
    finished_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS backfill_queue_state ON backfill_queue (state, enqueued_at);

------------------------------------------------------------------------
-- Tiny key-value table for ad-hoc app metadata (last instrument refresh
-- time, last backfill cycle start, etc). Kept simple — JSONB value.
------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app_meta (
    key        TEXT PRIMARY KEY,
    value      JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
