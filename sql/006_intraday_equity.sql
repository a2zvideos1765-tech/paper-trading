-- 006_intraday_equity.sql — minute-resolution live equity for the dashboard.
--
-- The daily `equity_snapshots` table (one row per calendar day, stamped 00:00 IST)
-- powers the long-term equity curve. It is too coarse to watch a portfolio move
-- during the session, and its 00:00 timestamp made the dashboard's "as of" read
-- midnight even mid-session. This table records one row per trader tick during
-- market hours so the dashboard shows a true clock time and an intraday-moving curve.
--
-- It is intentionally short-lived: the trader prunes rows older than a few trading
-- days. The authoritative history stays in `equity_snapshots`.
--
-- Idempotent. Run once on the VPS:  psql ... -f sql/006_intraday_equity.sql

CREATE TABLE IF NOT EXISTS equity_intraday (
    portfolio_id   INTEGER     NOT NULL REFERENCES portfolios(id) ON DELETE CASCADE,
    ts             TIMESTAMPTZ NOT NULL,          -- minute-floored IST of the trader tick
    cash           NUMERIC     NOT NULL,
    holdings_value NUMERIC     NOT NULL,
    equity         NUMERIC     NOT NULL,
    open_positions INTEGER     NOT NULL,
    PRIMARY KEY (portfolio_id, ts)
);

CREATE INDEX IF NOT EXISTS equity_intraday_pid_ts
    ON equity_intraday (portfolio_id, ts DESC);
