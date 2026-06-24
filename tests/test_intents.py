"""Tests for the real-order intent selection (src/engine/real_executor).

This is the load-bearing guard for real money: it decides which engine signals
become live broker orders. We verify the dedup key, the recency window, and the
stale-signal counter in isolation (no DB / Angel).
"""

from __future__ import annotations

from src.engine.real_executor import (
    count_stale_intents,
    intent_key,
    reconcile_sell_qty,
    scan_time_elapsed,
    select_new_intents,
    sip_deposit_amount,
    surveillance_reject_code,
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


# ---- logical dedup: same entry at a wiggling price must NOT re-place ----

def test_same_entry_different_price_not_replaced():
    """The TCS-3× bug: the forming scan bar gives a different price each tick, so
    the same logical entry must still be deduped against an already-placed order."""
    placed = _trade("2026-06-16", symbol="TCS", price=2073.47)
    incoming = _trade("2026-06-16", symbol="TCS", price=2075.77)  # same entry, new price
    out = select_new_intents([incoming], {intent_key(placed)}, "2026-06-16", max_age_days=1)
    assert out == []


def test_same_entry_twice_in_one_batch_dedups():
    a = _trade("2026-06-16", symbol="TCS", price=2073.47)
    b = _trade("2026-06-16", symbol="TCS", price=2075.77)
    out = select_new_intents([a, b], set(), "2026-06-16", max_age_days=1)
    assert len(out) == 1


def test_different_reasons_not_deduped():
    # entry and a pyramid add on the same symbol/day are different logical actions
    entry = _trade("2026-06-16", symbol="TCS", reason="entry_scan_11:00_drop_-3%")
    pyr = _trade("2026-06-16", symbol="TCS", reason="pyramid_avg_-10%_lvl1")
    out = select_new_intents([entry, pyr], set(), "2026-06-16", max_age_days=1)
    assert len(out) == 2


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


# ---- sip_deposit_amount (net-value vs baseline) ----

def test_genuine_topup_above_baseline_detected():
    # net ₹25k vs baseline ₹20k (capital+deposits) → ₹5k deposit
    assert sip_deposit_amount(25000.0, 20000.0) == 5000.0


def test_initial_funding_below_capital_is_not_a_deposit():
    # the exact bug: account funded to ₹19,250 against ₹20k capital → NOT a deposit
    assert sip_deposit_amount(19250.0, 20000.0) == 0.0


def test_exactly_at_baseline_is_not_a_deposit():
    assert sip_deposit_amount(20000.0, 20000.0) == 0.0


def test_small_excess_below_min_is_not_a_deposit():
    assert sip_deposit_amount(20300.0, 20000.0, min_amount=500.0) == 0.0


def test_none_net_is_not_a_deposit():
    # failed funds read (no net) → never a deposit
    assert sip_deposit_amount(None, 20000.0) == 0.0


def test_baseline_includes_prior_deposits():
    # after a real ₹5k deposit, baseline is ₹25k; net ₹25.1k is not a new deposit
    assert sip_deposit_amount(25100.0, 25000.0, min_amount=500.0) == 0.0
    # but a further ₹6k top-up (net ₹31k) is
    assert sip_deposit_amount(31000.0, 25000.0) == 6000.0


# ---- scan_time_elapsed (don't front-run the scan time) ----

def test_scan_entry_before_scan_time_not_ready():
    # 14:00 scan evaluated at 11:20 is provisional → not ready
    assert scan_time_elapsed("entry_scan_14:00_drop_-3%", "11:20") is False


def test_scan_entry_at_scan_time_still_forming_not_ready():
    # at 14:00 the 14:00 bar is only just opening (covers 14:00–14:05) → not ready
    assert scan_time_elapsed("entry_scan_14:00_drop_-3%", "14:00") is False
    assert scan_time_elapsed("entry_scan_14:00_drop_-3%", "14:04") is False


def test_scan_entry_after_bar_completes_ready():
    # the 14:00 bar finalises at 14:05 → ready
    assert scan_time_elapsed("entry_scan_14:00_drop_-3%", "14:05") is True
    assert scan_time_elapsed("entry_scan_14:00_drop_-3%", "15:05") is True


def test_earlier_scan_window_ready_after_its_bar():
    assert scan_time_elapsed("entry_scan_11:00_drop_-3%", "11:04") is False
    assert scan_time_elapsed("entry_scan_11:00_drop_-3%", "11:05") is True
    assert scan_time_elapsed("entry_scan_11:00_drop_-3%", "11:20") is True


def test_non_scan_reasons_always_ready():
    # pyramid adds / exits act on the current bar by design
    assert scan_time_elapsed("pyramid_avg_-10%_lvl1", "09:30") is True
    assert scan_time_elapsed("target_+25%_tier1", "09:30") is True
    assert scan_time_elapsed("", "09:30") is True


# ---- reconcile_sell_qty: broker is the source of truth ----

def test_sell_skipped_when_broker_holds_none():
    """Phantom guard: a SELL for a symbol the broker doesn't hold returns 0 (skip).
    This is what stops the bot re-trying a sell of a never-filled / surveillance-
    blocked position (e.g. PARACABLES) on every tick forever."""
    assert reconcile_sell_qty(engine_qty=45, broker_qty=0) == 0
    assert reconcile_sell_qty(engine_qty=45, broker_qty=0, fully_closed=True) == 0


def test_partial_tier_clamps_to_engine_qty():
    """A partial profit tier sells the engine's qty when the broker holds at least that."""
    assert reconcile_sell_qty(engine_qty=3, broker_qty=6, fully_closed=False) == 3


def test_partial_tier_never_oversells_broker():
    """If the broker somehow holds fewer than the engine thinks, never oversell."""
    assert reconcile_sell_qty(engine_qty=10, broker_qty=4, fully_closed=False) == 4


def test_full_close_sweeps_duplicate_fill_orphans():
    """The exact INFY/TCS case: engine fully closed (qty 3) but the broker holds 6
    from duplicate fills — the final exit sweeps ALL 6, leaving nothing stranded."""
    assert reconcile_sell_qty(engine_qty=3, broker_qty=6, fully_closed=True) == 6


def test_reserved_shares_are_not_resold_within_a_tick():
    """Multiple tiers for one symbol in one tick can't collectively oversell."""
    # tier1 already reserved 4 of 6 → full close sweeps only the remaining 2
    assert reconcile_sell_qty(engine_qty=2, broker_qty=6, reserved=4, fully_closed=True) == 2
    # everything already reserved → skip
    assert reconcile_sell_qty(engine_qty=2, broker_qty=6, reserved=6, fully_closed=True) == 0
    # partial tier clamps against what's left after the reservation
    assert reconcile_sell_qty(engine_qty=3, broker_qty=6, reserved=5, fully_closed=False) == 1


# ---- surveillance_reject_code: AB4036 auto-skip quarantine trigger ----

def test_ab4036_rejection_is_quarantined():
    """The exact PARACABLES/UNIVCABLES case: a cautionary/surveillance block returns its
    code so the caller benches the symbol instead of re-firing a doomed order each signal."""
    err = "Angel placeOrder rejected [AB4036]: scrip not allowed for trading"
    assert surveillance_reject_code(err) == "AB4036"


def test_transient_infra_rejection_is_not_quarantined():
    """An IP-whitelist / infra error must NOT bench a tradeable symbol — it should keep
    retrying. Only permanent per-symbol blocks quarantine."""
    assert surveillance_reject_code("Angel placeOrder rejected [AG7002]: IP not whitelisted") is None
    assert surveillance_reject_code("Angel placeOrder rejected [AB1004]: insufficient funds") is None


def test_reject_code_extraction_is_case_insensitive():
    assert surveillance_reject_code("rejected [ab4036]: ...") == "AB4036"


def test_no_code_or_empty_is_not_quarantined():
    assert surveillance_reject_code("connection reset by peer") is None
    assert surveillance_reject_code("") is None
    assert surveillance_reject_code(None) is None
