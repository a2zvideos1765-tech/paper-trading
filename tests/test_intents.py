"""Tests for the real-order intent selection (src/engine/real_executor).

This is the load-bearing guard for real money: it decides which engine signals
become live broker orders. We verify the dedup key, the recency window, and the
stale-signal counter in isolation (no DB / Angel).
"""

from __future__ import annotations

from src.engine.real_executor import (
    count_stale_intents,
    intent_key,
    select_new_intents,
)


def _trade(date_str, symbol="AUROPHARMA", side="BUY", qty=4, price=1399.40,
           time="14:00", reason="entry_scan_14:00_drop_-3%"):
    return {"date": date_str, "time": time, "symbol": symbol, "side": side,
            "qty": qty, "price": price, "reason": reason}


# ---- intent_key ----

def test_intent_key_is_stable_and_unique():
    t = _trade("2026-06-16")
    assert intent_key(t) == intent_key(dict(t))           # deterministic
    assert intent_key(t) != intent_key(_trade("2026-06-16", qty=5))
    assert intent_key(t) != intent_key(_trade("2026-06-15"))


def test_intent_key_normalises_qty_and_price():
    a = intent_key(_trade("2026-06-16", qty=4, price=1399.4))
    b = intent_key(_trade("2026-06-16", qty=4.0, price=1399.40))
    assert a == b


# ---- select_new_intents: recency window ----

def test_today_intent_is_selected():
    trades = [_trade("2026-06-16")]
    out = select_new_intents(trades, set(), "2026-06-16", max_age_days=1)
    assert len(out) == 1


def test_yesterday_intent_selected_with_default_window():
    """The case the user hit: a late signal from yesterday should still place."""
    trades = [_trade("2026-06-15")]
    out = select_new_intents(trades, set(), "2026-06-16", max_age_days=1)
    assert len(out) == 1
    assert out[0][1]["symbol"] == "AUROPHARMA"


def test_yesterday_intent_skipped_with_strict_window():
    trades = [_trade("2026-06-15")]
    out = select_new_intents(trades, set(), "2026-06-16", max_age_days=0)
    assert out == []


def test_intent_older_than_window_is_skipped():
    trades = [_trade("2026-06-10")]
    out = select_new_intents(trades, set(), "2026-06-16", max_age_days=1)
    assert out == []


def test_future_dated_intent_never_selected():
    trades = [_trade("2026-06-17")]
    out = select_new_intents(trades, set(), "2026-06-16", max_age_days=3)
    assert out == []


def test_already_placed_intent_is_skipped():
    t = _trade("2026-06-16")
    out = select_new_intents([t], {intent_key(t)}, "2026-06-16", max_age_days=1)
    assert out == []


def test_order_preserved_chronologically():
    trades = [_trade("2026-06-16", symbol="A"), _trade("2026-06-16", symbol="B")]
    out = select_new_intents(trades, set(), "2026-06-16", max_age_days=1)
    assert [t["symbol"] for _, t in out] == ["A", "B"]


def test_malformed_date_is_skipped_not_crash():
    out = select_new_intents([_trade("not-a-date")], set(), "2026-06-16")
    assert out == []


# ---- count_stale_intents ----

def test_count_stale_counts_only_unplaced_older_than_window():
    trades = [
        _trade("2026-06-16", symbol="TODAY"),      # in window
        _trade("2026-06-15", symbol="YDAY"),       # in window (max_age=1)
        _trade("2026-06-10", symbol="OLD1"),       # stale
        _trade("2026-06-09", symbol="OLD2"),       # stale
    ]
    assert count_stale_intents(trades, set(), "2026-06-16", max_age_days=1) == 2


def test_count_stale_ignores_already_placed():
    old = _trade("2026-06-10", symbol="OLD")
    assert count_stale_intents([old], {intent_key(old)}, "2026-06-16", max_age_days=1) == 0
