-- TimescaleDB extension + hypertables. Run AFTER 001_schema.sql:
--   psql -U paper -d paper_trading -f sql/002_timescale.sql
--
-- If TimescaleDB is not installed, the system still works on plain Postgres —
-- it just won't get the time-series query optimizations. Only run this file
-- if `CREATE EXTENSION timescaledb` succeeds on your VPS.

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- Convert candles into a hypertable partitioned by ts (default 7-day chunks).
-- if_not_exists + migrate_data handles re-running on a populated table.
SELECT create_hypertable(
    'candles',
    'ts',
    if_not_exists => TRUE,
    migrate_data  => TRUE
);

-- Convert equity_snapshots too (smaller, but consistent treatment).
SELECT create_hypertable(
    'equity_snapshots',
    'ts',
    if_not_exists => TRUE,
    migrate_data  => TRUE
);
