-- Phase 2 — India VIX in the universe (needed by multi-regime strategies S228/S283).
-- Run after sql/004_forward_trading.sql:
--   psql -U paper -d paper_trading -f sql/005_india_vix.sql
--
-- The engine's regime classifier looks up VIX under the exact symbol 'INDIA_VIX'
-- (see src/engine/v2_engine.py classify_regime_by_date). So we store it under that
-- name regardless of how Angel's instrument master spells it. The Angel token is
-- looked up from `instruments` (populated by tools/refresh_instruments.py).
--
-- IMPORTANT: do NOT add India VIX from the dashboard /symbols search — that would
-- store it under Angel's spelling ("India VIX") and the engine wouldn't find it.

INSERT INTO universe_symbols (symbol, exchange, token, kind, enabled)
SELECT 'INDIA_VIX', i.exchange, i.token, 'index', TRUE
  FROM instruments i
 WHERE upper(i.symbol) IN ('INDIA VIX', 'INDIAVIX', 'INDIA_VIX')
    OR upper(i.name)   LIKE '%INDIA VIX%'
 ORDER BY (i.exchange = 'NSE') DESC, length(i.symbol)
 LIMIT 1
ON CONFLICT (symbol, exchange) DO UPDATE
  SET enabled = TRUE, token = EXCLUDED.token, kind = 'index';

-- Verify: this should return exactly one row. If it returns zero, the instrument
-- master has no India VIX entry under a recognised name — run
--   SELECT token, symbol, name, exchange FROM instruments WHERE name ILIKE '%vix%';
-- find the right token, and INSERT the universe_symbols row by hand.
SELECT symbol, exchange, token, kind, enabled
  FROM universe_symbols
 WHERE symbol = 'INDIA_VIX';
