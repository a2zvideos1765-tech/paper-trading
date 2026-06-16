"""Pure helpers for translating engine intent into real broker orders.

Kept free of DB / Angel I/O so the dedup logic — the load-bearing guard against
double-placing a real order — is unit-testable in isolation.

The engine (`run_backtest_v2`) is replayed forward-only every tick and re-emits the
*full* deterministic trade list. Each trade maps to one `intent_key`; we place a real
order only for intents we haven't already acted on (tracked in `real_orders`), and only
for intents within a small recent window (`max_age_days`) so a bot that was off for a
long stretch never fires a burst of ancient orders at stale prices when it's switched
back on — while still letting a signal whose candle arrived late (after market close)
get placed the next session.
"""

from __future__ import annotations

from datetime import date, timedelta


def intent_key(trade: dict) -> str:
    """Stable dedup key for one engine trade.

    Mirrors the paper `trades` dedup tuple (ts, symbol, side, qty, price, reason) so the
    same engine output always maps to the same key across ticks and process restarts.
    """
    return "|".join([
        f"{trade['date']} {trade['time']}",
        str(trade["symbol"]),
        str(trade["side"]),
        str(int(trade["qty"])),
        f"{float(trade['price']):.4f}",
        str(trade["reason"]),
    ])


def select_new_intents(
    trades: list[dict],
    existing_keys: set[str],
    today_str: str,
    max_age_days: int = 1,
) -> list[tuple[str, dict]]:
    """Return [(intent_key, trade), …] for trades that should be placed as real orders.

    A trade qualifies only if:
      * its date is within the window [today - max_age_days, today] — never in the
        future, never older than the window (so a long outage can't fire a burst of
        ancient orders), and
      * its intent_key is not already in `existing_keys` (not already acted on).

    `max_age_days` is in *calendar* days. The default of 1 means "today or yesterday",
    which absorbs a signal whose candle landed after market close. Set 0 for the
    strict today-only behaviour; bump to 3 to also bridge a Mon-after-Fri-signal gap.

    Order is preserved (chronological as the engine emitted them).
    """
    today = date.fromisoformat(today_str)
    min_date = today - timedelta(days=max(0, max_age_days))
    out: list[tuple[str, dict]] = []
    for t in trades:
        try:
            d = date.fromisoformat(str(t.get("date")))
        except (TypeError, ValueError):
            continue
        if d < min_date or d > today:
            continue
        key = intent_key(t)
        if key in existing_keys:
            continue
        out.append((key, t))
    return out


def count_stale_intents(
    trades: list[dict],
    existing_keys: set[str],
    today_str: str,
    max_age_days: int = 1,
) -> int:
    """How many un-acted intents are older than the placement window.

    Used purely for visibility — so the bot can report "N signals skipped as too
    old to place" instead of silently passing on them and looking like a bug.
    """
    today = date.fromisoformat(today_str)
    min_date = today - timedelta(days=max(0, max_age_days))
    n = 0
    for t in trades:
        try:
            d = date.fromisoformat(str(t.get("date")))
        except (TypeError, ValueError):
            continue
        if d < min_date and intent_key(t) not in existing_keys:
            n += 1
    return n
