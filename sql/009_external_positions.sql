-- 009_external_positions.sql — adopt broker positions the engine didn't create.
--
-- The live trader makes the BROKER the single source of truth: any equity the
-- account actually holds that the engine isn't already managing (a manual buy, or
-- an orphaned fill of an already-closed position) is recorded here, then injected
-- into the engine replay (run_backtest_v2's `external_positions=`) so the S404
-- exit ladder manages it. Rows are removed once the broker no longer holds them.
--
-- Non-destructive and idempotent. Run after sql/008_instruments_token_exchange_pk.sql:
--   psql -h 127.0.0.1 -U paper -d paper_trading -f sql/009_external_positions.sql

CREATE TABLE IF NOT EXISTS real_external_positions (
    symbol           TEXT PRIMARY KEY,          -- engine symbol (no -EQ suffix)
    first_seen_date  DATE NOT NULL,             -- IST trading day first observed at the broker
    entry_price      NUMERIC(18, 4) NOT NULL,   -- broker average price at first sight (= engine entry)
    qty              INTEGER NOT NULL,           -- broker quantity at first sight
    note             TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
