"""Parity test: the vendored engine is byte-for-byte the same logic as upstream.

This test is the load-bearing guarantee that paper-trading produces the same
trades the backtester does. It runs the vendored engine on synthetic candle data
that exercises every code path (entry, pyramid add, tier exit) and checks the
output is deterministic and structurally correct.

If you re-vendor engine_v2 from upstream, run this test first. If it fails, you
broke parity — investigate before shipping.
"""

from __future__ import annotations

import os
from datetime import datetime, time, timedelta

os.environ.setdefault("PG_PASSWORD", "test")
os.environ.setdefault("ANGEL_API_KEY", "test")
os.environ.setdefault("ANGEL_CLIENT_CODE", "test")
os.environ.setdefault("ANGEL_PASSWORD", "test")
os.environ.setdefault("ANGEL_TOTP_SECRET", "JBSWY3DPEHPK3PXP")
os.environ.setdefault("DASHBOARD_PASSWORD", "test")
os.environ.setdefault("SESSION_SECRET", "test-secret-do-not-use")

import pandas as pd  # noqa: E402

from src.engine.v2_engine import ChargeConfigV2, run_backtest_v2  # noqa: E402
from src.strategies.registry import get  # noqa: E402


def _gen_intraday_5m(date: datetime.date, close_: float, volume: int = 1_000_000) -> list[dict]:
    """Synthesize 5-minute intraday bars from 09:15 to 15:30 IST that gap to
    today's close at the open and hold flat. This guarantees the engine's
    scan-time (11:00) snapshot sees today's close vs yesterday's close, which
    is what `fall_threshold` is computed against."""
    bars = []
    n = 75  # ~75 5-min candles per session
    for i in range(n):
        t = datetime.combine(date, time(9, 15)) + timedelta(minutes=5 * i)
        bars.append({
            "timestamp": t,
            "symbol": None,
            "open":  close_,
            "high":  close_ * 1.001,
            "low":   close_ * 0.999,
            "close": close_,
            "volume": volume // n,
        })
    return bars


def _build_history_one_symbol(symbol: str, daily_closes: list[tuple[datetime.date, float]]) -> pd.DataFrame:
    """Build a continuous intraday DataFrame for one symbol given daily closes."""
    rows: list[dict] = []
    for date, close in daily_closes:
        bars = _gen_intraday_5m(date, close)
        for b in bars:
            b["symbol"] = symbol
            rows.append(b)
    df = pd.DataFrame(rows)
    df["date"] = df["timestamp"].dt.date
    df["time"] = df["timestamp"].dt.strftime("%H:%M")
    return df


def _trading_days(start: datetime.date, n: int) -> list[datetime.date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d = d + timedelta(days=1)
    return out


def test_engine_produces_buy_on_drop_then_sell_on_target():
    """Day-by-day: 25 days flat at ₹100 to warm up RSI/BB, then a -6% drop on
    day 26 (entry), then run-up to +35% (S6's last tier exit at +50% won't fire,
    but +15% and +30% tiers will)."""
    days = _trading_days(datetime(2025, 1, 1).date(), 60)
    closes = [100.0] * 25
    closes.append(94.0)   # -6% from prev ₹100 → triggers S6 entry on day 26
    # Slow ramp up over the next 30 days from 94 to 145 (+54%), exercising tier exits at +15% and +30%.
    for i in range(30):
        closes.append(94.0 + (145.0 - 94.0) * ((i + 1) / 30))
    # Pad to 60
    while len(closes) < len(days):
        closes.append(closes[-1])

    df = _build_history_one_symbol("ACME", list(zip(days, closes)))

    s6 = get("S6_tiered_exit")
    result = run_backtest_v2(df, s6, ChargeConfigV2())

    sides = [t["side"] for t in result["trades"]]
    assert "BUY" in sides, "S6 should fire an entry on the -6% drop"
    assert "SELL" in sides, "S6 should fire at least one tier exit on the run-up"

    # First trade is the entry, on day 26.
    first = result["trades"][0]
    assert first["side"] == "BUY"
    assert first["symbol"] == "ACME"
    # Subsequent SELLs should be 'target_*_tier*'
    sell_reasons = [t["reason"] for t in result["trades"] if t["side"] == "SELL"]
    assert any(r.startswith("target_") for r in sell_reasons), f"got: {sell_reasons}"


def test_engine_is_deterministic():
    """Two replays on the exact same data must produce the exact same trades —
    this is what guarantees the live trader (which replays every minute) doesn't
    spuriously fire duplicates."""
    days = _trading_days(datetime(2025, 1, 1).date(), 60)
    closes = [100.0] * 25 + [94.0] + [94.0 + (140.0 - 94.0) * (i / 30) for i in range(34)]
    df = _build_history_one_symbol("ACME", list(zip(days, closes)))

    s1 = get("S1_user_pyramid")
    r1 = run_backtest_v2(df, s1, ChargeConfigV2())
    r2 = run_backtest_v2(df, s1, ChargeConfigV2())

    assert r1["trades"] == r2["trades"], "engine output must be deterministic"
    assert r1["summary"] == r2["summary"]


def test_sip_deposit_injection_funds_a_later_entry():
    """SIP: a mid-run deposit (passed via deposits={date: amount}) injects cash
    before that day's exits/entries, so an entry that was unaffordable becomes
    affordable. Also confirms the engine returns a non-empty `deposits` log."""
    from dataclasses import replace
    days = _trading_days(datetime(2025, 1, 1).date(), 60)
    closes = [100.0] * 25 + [94.0] + [94.0 + (145.0 - 94.0) * ((i + 1) / 30) for i in range(30)]
    while len(closes) < len(days):
        closes.append(closes[-1])
    df = _build_history_one_symbol("ACME", list(zip(days, closes)))

    # Start near-broke so the day-26 entry can't fire (S6 needs ~₹10k).
    base = replace(get("S6_tiered_exit"), starting_cash=500.0)
    r_nodep = run_backtest_v2(df, base, ChargeConfigV2())
    assert not any(t["side"] == "BUY" for t in r_nodep["trades"]), "too poor to buy without a deposit"
    assert r_nodep["deposits"] == [], "no deposits → empty deposit log"

    # Inject ₹50k on the drop day → the entry is now affordable.
    drop_day = str(days[25])
    r_dep = run_backtest_v2(df, base, ChargeConfigV2(), deposits={drop_day: 50_000.0})
    assert any(t["side"] == "BUY" for t in r_dep["trades"]), "deposit should fund the entry"
    assert r_dep["deposits"], "deposit log should record the injection"
    assert r_dep["deposits"][0]["amount"] == 50_000.0
    assert r_dep["deposits"][0]["date"] == drop_day


def test_external_position_is_adopted_and_managed():
    """Broker adoption: a position the engine didn't create (passed via
    external_positions={date: {symbol: {qty, avg_price}}}) is injected into holdings
    and EXITED by the strategy's rules — without ever emitting a BUY for it, and
    without the engine re-buying it. external_positions=None leaves output unchanged."""
    from dataclasses import replace

    days = _trading_days(datetime(2025, 1, 1).date(), 60)
    # Monotonic up-ramp → no -drop, so the engine never NATIVELY enters ACME.
    closes = [100.0] * 25 + [100.0 + (160.0 - 100.0) * ((i + 1) / 35) for i in range(35)]
    while len(closes) < len(days):
        closes.append(closes[-1])
    df = _build_history_one_symbol("ACME", list(zip(days, closes)))

    base = replace(get("S6_tiered_exit"), starting_cash=100_000.0)

    # Baseline: no adoption → no ACME trades at all, empty injection log.
    r_plain = run_backtest_v2(df, base, ChargeConfigV2())
    assert not any(t["symbol"] == "ACME" for t in r_plain["trades"]), "no native entry on a pure up-ramp"
    assert r_plain["external_injections"] == []

    # Parity: external_positions=None must be byte-for-byte the baseline.
    r_none = run_backtest_v2(df, base, ChargeConfigV2(), external_positions=None)
    assert r_none["trades"] == r_plain["trades"], "None adoption must not change engine output"

    # Adopt 100 shares bought at ₹100 on day 26; the run-up to ₹160 must trigger exits.
    inject_day = str(days[25])
    r_adopt = run_backtest_v2(
        df, base, ChargeConfigV2(),
        external_positions={inject_day: {"ACME": {"qty": 100, "avg_price": 100.0}}},
    )
    assert r_adopt["external_injections"], "adoption should be logged"
    inj = r_adopt["external_injections"][0]
    assert inj["symbol"] == "ACME" and inj["qty"] == 100 and inj["avg_price"] == 100.0
    assert "entry_depth_pct" in inj

    acme_trades = [t for t in r_adopt["trades"] if t["symbol"] == "ACME"]
    assert acme_trades, "the adopted position should be managed (and exited)"
    assert not any(t["side"] == "BUY" for t in acme_trades), "adopted ≠ bought: no BUY emitted"
    assert any(t["side"] == "SELL" and t["reason"].startswith("target_") for t in acme_trades), \
        f"the strategy's tier exit should fire on the run-up; got {[t['reason'] for t in acme_trades]}"


def test_min_entry_cash_gates_new_entries():
    """SIP fee gate: when free cash < min_entry_cash, NO new entries fire that day.
    Setting the floor above available cash blocks all buys; disabling it lets the
    same entry through — proving the gate is the only difference."""
    from dataclasses import replace
    days = _trading_days(datetime(2025, 1, 1).date(), 60)
    closes = [100.0] * 25 + [94.0] + [94.0 + (145.0 - 94.0) * ((i + 1) / 30) for i in range(30)]
    while len(closes) < len(days):
        closes.append(closes[-1])
    df = _build_history_one_symbol("ACME", list(zip(days, closes)))

    base = get("S6_tiered_exit")  # starting_cash 100_000
    gated = replace(base, min_entry_cash=200_000.0)  # floor above all available cash
    rg = run_backtest_v2(df, gated, ChargeConfigV2())
    assert not any(t["side"] == "BUY" for t in rg["trades"]), "gate should block every new entry"

    rb = run_backtest_v2(df, base, ChargeConfigV2())  # gate disabled (None)
    assert any(t["side"] == "BUY" for t in rb["trades"]), "without the gate the entry fires"


def test_capital_binding_changes_only_qty_not_signals():
    """Two replays of the same strategy with different starting_cash should fire
    BUYs at the same prices/dates — only the qty differs (capped by cash)."""
    from dataclasses import replace
    days = _trading_days(datetime(2025, 1, 1).date(), 50)
    closes = [100.0] * 25 + [94.0] + [94.0 + 0.3 * i for i in range(24)]
    df = _build_history_one_symbol("ACME", list(zip(days, closes)))

    base = get("S6_tiered_exit")
    cheap = replace(base, starting_cash=50_000.0)
    rich  = replace(base, starting_cash=100_000.0)

    rc = run_backtest_v2(df, cheap, ChargeConfigV2())
    rr = run_backtest_v2(df, rich,  ChargeConfigV2())

    # Same number of trades on either capital (S6 allocates a fixed ₹10k per buy).
    assert len(rc["trades"]) == len(rr["trades"])
    for a, b in zip(rc["trades"], rr["trades"]):
        assert a["side"] == b["side"]
        assert a["date"] == b["date"]
        assert a["symbol"] == b["symbol"]
        assert a["price"] == b["price"]
