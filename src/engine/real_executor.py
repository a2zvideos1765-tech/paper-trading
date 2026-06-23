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

import re
from datetime import date, datetime, timedelta


# Matches the engine's scan-entry reason, e.g. "entry_scan_14:00_drop_-3%".
_SCAN_REASON_RE = re.compile(r"entry_scan_(\d{2}:\d{2})")


def scan_time_elapsed(reason: str, now_hhmm: str, bar_minutes: int = 5) -> bool:
    """For a scan-mode entry, True only once its scan BAR is complete today.

    The engine reads the scan as `time <= scan_time` → the bar timestamped at the
    scan time (e.g. the 11:00 bar covers 11:00–11:05 and only finalises at 11:05).
    Acting at 11:00 means acting on a *still-forming* bar whose close keeps changing
    every tick — which both front-runs the decision and makes the price (and the
    intent) move each minute. So we wait until scan_time + one bar interval, when
    the bar matches what the backtest used.

    Non-scan reasons (pyramid adds, tiered exits, stops) act on the current bar by
    design and are always allowed.
    """
    m = _SCAN_REASON_RE.search(reason or "")
    if not m:
        return True
    ready_at = (datetime.strptime(m.group(1), "%H:%M")
                + timedelta(minutes=bar_minutes)).strftime("%H:%M")
    return now_hhmm >= ready_at


def _logical_key_from_trade(trade: dict) -> str:
    """One logical action = date · symbol · side · reason (NO price/qty/time).

    The engine re-emits the same entry every tick; while its scan bar is still
    forming the price (and qty) wiggle, so a price-bearing key lets the SAME
    entry be placed as several real orders → duplicate fills. The reason already
    encodes the scan window / pyramid level / exit tier, so this collapses re-
    evaluations of one action while keeping genuinely different actions distinct.
    """
    return "|".join([str(trade["date"]), str(trade["symbol"]),
                     str(trade["side"]), str(trade["reason"])])


def _logical_key_from_intent_key(k: str) -> str:
    """Derive the logical key from a stored full intent_key (back-compat with
    real_orders rows written before logical dedup)."""
    parts = k.split("|")
    if len(parts) < 6:
        return k
    date_part = parts[0].split(" ")[0]
    return "|".join([date_part, parts[1], parts[2], parts[-1]])


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

    Dedup is by *logical* key (date·symbol·side·reason), so the same entry can't
    be placed twice just because its price/qty wiggled between ticks. Order is
    preserved (chronological as the engine emitted them).
    """
    today = date.fromisoformat(today_str)
    min_date = today - timedelta(days=max(0, max_age_days))
    existing_logical = {_logical_key_from_intent_key(k) for k in existing_keys}
    seen_logical: set[str] = set()
    out: list[tuple[str, dict]] = []
    for t in trades:
        try:
            d = date.fromisoformat(str(t.get("date")))
        except (TypeError, ValueError):
            continue
        if d < min_date or d > today:
            continue
        lk = _logical_key_from_trade(t)
        if lk in existing_logical or lk in seen_logical:
            continue
        seen_logical.add(lk)
        out.append((intent_key(t), t))
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


def sip_deposit_amount(
    account_net: float | None,
    expected_baseline: float,
    min_amount: float = 500.0,
) -> float:
    """How much (if any) to record as a SIP deposit this tick — net-value based.

    `account_net` is the account's total value (free cash + holdings market value);
    `expected_baseline` is the seeded live capital plus deposits already recorded.
    A genuine SIP top-up pushes net ABOVE that baseline; returns the excess (≥
    min_amount) or 0.0.

    Net-based detection deliberately ignores:
      * the initial funding up to your capital — net never exceeds the baseline,
        so establishing the ₹20k is NOT a deposit (this is what fabricated the
        ~₹19k phantom deposit and inflated engine equity to ~₹39k);
      * buys/sells — cash and holdings move opposite ways, net ~unchanged;
      * failed/empty funds reads — net drops, never a deposit.
    """
    if account_net is None:
        return 0.0
    excess = float(account_net) - float(expected_baseline)
    return round(excess, 2) if excess >= min_amount else 0.0
