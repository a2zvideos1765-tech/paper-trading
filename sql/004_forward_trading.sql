-- Phase 2 — forward-only paper trading.
-- Run after sql/003_universe.sql:
--   psql -U paper -d paper_trading -f sql/004_forward_trading.sql
--
-- Idempotent for the schema change; the cleanup section is one-time-destructive
-- (it wipes the trades / equity / positions that came from the 200-day historical
-- replay so the trader can start fresh from now()).

------------------------------------------------------------------------
-- Each portfolio records the exact moment paper trading "began". The
-- trader uses this as the floor of its candle window so historical bars
-- never produce backdated paper trades.
------------------------------------------------------------------------
ALTER TABLE portfolios
  ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ;

UPDATE portfolios
   SET started_at = now()
 WHERE started_at IS NULL;

ALTER TABLE portfolios
  ALTER COLUMN started_at SET NOT NULL,
  ALTER COLUMN started_at SET DEFAULT now();

------------------------------------------------------------------------
-- One-time cleanup of the historical-replay artefacts. Safe to re-run;
-- after the first time the `< started_at` predicate matches nothing.
------------------------------------------------------------------------
DELETE FROM trades t
 USING portfolios p
 WHERE t.portfolio_id = p.id
   AND t.ts < p.started_at;

DELETE FROM equity_snapshots e
 USING portfolios p
 WHERE e.portfolio_id = p.id
   AND e.ts < p.started_at;

-- Positions are derived state; clear so the trader rebuilds them from
-- post-start trades only.
DELETE FROM positions;

------------------------------------------------------------------------
-- Seed one equity_snapshot per portfolio at started_at so the equity
-- curve has a clean origin (capital, no holdings, zero open positions).
------------------------------------------------------------------------
INSERT INTO equity_snapshots (portfolio_id, ts, cash, holdings_value, equity, open_positions)
SELECT p.id, p.started_at, p.capital, 0, p.capital, 0
  FROM portfolios p
 WHERE NOT EXISTS (
       SELECT 1 FROM equity_snapshots e
        WHERE e.portfolio_id = p.id
          AND e.ts = p.started_at
 );
