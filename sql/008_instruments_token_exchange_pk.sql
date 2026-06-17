-- 008_instruments_token_exchange_pk.sql — fix silent loss of NSE equities.
--
-- Angel One's instrument `token` is unique only WITHIN an exchange segment, not
-- globally — the same numeric token is reused across NSE / BSE / NFO / BFO / NCO /
-- MCX / CDS. The original `PRIMARY KEY (token)` (sql/003) therefore made
-- refresh_instruments' `ON CONFLICT (token) DO UPDATE` overwrite an NSE cash
-- equity row whenever a later row in the master reused its token (the derivative
-- and commodity segments have 60k+/50k+/37k+ rows). The result: hundreds of NSE
-- `-EQ` equities (AUROPHARMA, BEL, BIOCON, NHPC, TATAPOWER, …) were clobbered and
-- went missing, which is why migrate_universe_to_nse couldn't find their NSE
-- listing.
--
-- The natural key is (token, exchange). After this runs, re-run
-- tools.refresh_instruments so the previously-overwritten NSE rows are restored.
-- Non-destructive: the existing PK guarantees one row per token, so adding
-- exchange to the key can't violate uniqueness.

ALTER TABLE instruments DROP CONSTRAINT IF EXISTS instruments_pkey;
ALTER TABLE instruments ADD PRIMARY KEY (token, exchange);

-- Token is no longer unique on its own; keep it indexed for the by-token lookups.
CREATE INDEX IF NOT EXISTS instruments_token_idx ON instruments (token);
