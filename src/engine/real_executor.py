"""Pure helpers for translating engine intent into real broker orders.

Kept free of DB / Angel I/O so the dedup logic — the load-bearing guard against
double-placing a real order — is unit-testable in isolation.

The engine (`run_backtest_v2`) is replayed forward-only every tick and re-emits the
*full* deterministic trade list. Each trade maps to one `intent_key`; we place a real
order only for intents we haven't already acted on (tracked in `real_orders`), and only
for intents dated *today* so a bot that was off for a while never fires a burst of stale
orders at yesterday's prices when it's switched back on.
"""

from __future__ import annotations


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
) -> list[tuple[str, dict]]:
    """Return [(intent_key, trade), …] for trades that should be placed as real orders.

    A trade qualifies only if:
      * its date == `today_str` (no backdated/stale orders), and
      * its intent_key is not already in `existing_keys` (not already acted on).

    Order is preserved (chronological as the engine emitted them).
    """
    out: list[tuple[str, dict]] = []
    for t in trades:
        if str(t.get("date")) != today_str:
            continue
        key = intent_key(t)
        if key in existing_keys:
            continue
        out.append((key, t))
    return out
