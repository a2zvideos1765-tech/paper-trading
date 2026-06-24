-- Symbols hard-blocked by the broker (exchange surveillance / cautionary listing, e.g. the
-- Angel rejection code AB4036) are benched here for 3 months so the live bot stops firing a
-- guaranteed-failed order on every signal. The bench lifts AUTOMATICALLY after `expires_at`
-- -- if the scrip leaves surveillance it becomes tradeable again with no manual cleanup.
-- Quarantine blocks BUYs only (a genuinely held position can still be exited).
CREATE TABLE IF NOT EXISTS real_quarantine (
    symbol         TEXT PRIMARY KEY,           -- engine symbol (no -EQ suffix)
    reason_code    TEXT NOT NULL,              -- broker rejection code, e.g. 'AB4036'
    reason_text    TEXT,                       -- full rejection message (truncated)
    quarantined_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at     TIMESTAMPTZ NOT NULL,       -- bench lifts automatically after this
    hits           INTEGER NOT NULL DEFAULT 1, -- times re-hit while benched
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS real_quarantine_expires_idx ON real_quarantine (expires_at);
