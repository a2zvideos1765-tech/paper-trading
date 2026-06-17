"""PROOF: in scan mode the engine evaluates the scan on the LATEST bar <= scan_time.

On a COMPLETE day that's the actual scan bar (e.g. 14:00). On the CURRENT,
incomplete day it's just the most recent bar (e.g. 11:20) — so a "14:00 scan"
fires early on a provisional price. This is exactly why the live bot placed a
trade labelled entry_scan_14:00 at 11:20.

The two tests below use the vendored run_backtest_v2 (what the bot runs) with one
clean scan strategy (S6, scan time moved to 14:00, drop threshold -3%):

  * incomplete day  : bars only to 11:20 at -4%      -> engine ENTERS (at 11:20)
  * complete day    : -4% at 11:20 but recovered by 14:00 -> engine does NOT enter

So at 11:26 the live bot (seeing only up to 11:20) takes a trade that the correct
end-of-day 14:00 evaluation would reject. That's the front-running bug.
"""

from __future__ import annotations

import os
from dataclasses import replace
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


FULL = 75                                   # 09:15..15:25, 5-min bars
I_1120 = (11 * 60 + 20 - (9 * 60 + 15)) // 5  # bar index of 11:20 -> 25
I_1400 = (14 * 60 - (9 * 60 + 15)) // 5        # bar index of 14:00 -> 57


def _trading_days(start, n):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d = d + timedelta(days=1)
    return out


def _bars(date, closes):
    rows = []
    for i, c in enumerate(closes):
        t = datetime.combine(date, time(9, 15)) + timedelta(minutes=5 * i)
        rows.append({"timestamp": t, "symbol": "ACME", "open": c,
                     "high": c * 1.001, "low": c * 0.999, "close": c, "volume": 10_000})
    return rows


def _frame(rows):
    df = pd.DataFrame(rows)
    df["date"] = df["timestamp"].dt.date
    df["time"] = df["timestamp"].dt.strftime("%H:%M")
    return df


def _warmup_rows():
    rows = []
    for d in _trading_days(datetime(2025, 1, 1).date(), 25):
        rows += _bars(d, [100.0] * FULL)
    return rows


def _strategy():
    # S6 is a plain scan strategy. Move its scan to 14:00, drop threshold to -3%,
    # and fund it so affordability isn't the variable.
    return replace(get("S6_tiered_exit"), scan_time="14:00",
                   fall_threshold=-0.03, starting_cash=100_000.0)


def test_incomplete_day_14h_scan_fires_at_1120():
    """Today's day only has bars up to 11:20 (it's ~11:26). All at -4%."""
    cur = _trading_days(datetime(2025, 1, 1).date(), 26)[-1]
    rows = _warmup_rows() + _bars(cur, [96.0] * (I_1120 + 1))   # 09:15..11:20 only
    result = run_backtest_v2(_frame(rows), _strategy(), ChargeConfigV2())
    buys = [t for t in result["trades"] if t["side"] == "BUY"]
    print("\n[INCOMPLETE DAY] buys:", buys)
    assert buys, "engine entered on the latest (11:20) bar for a 14:00 scan"
    assert buys[0]["time"] == "11:20", f"entry bar should be 11:20, got {buys[0]['time']}"
    assert "14:00" in buys[0]["reason"], buys[0]["reason"]


def test_complete_day_recovered_by_14h_does_not_enter():
    """Full day: -4% through 11:20 but recovered to flat by 14:00 → the REAL
    14:00 scan sees 0% change, so the correct decision is NOT to enter."""
    cur = _trading_days(datetime(2025, 1, 1).date(), 26)[-1]
    closes = [96.0] * (I_1120 + 1) + [100.0] * (FULL - (I_1120 + 1))  # recover after 11:20
    rows = _warmup_rows() + _bars(cur, closes)
    result = run_backtest_v2(_frame(rows), _strategy(), ChargeConfigV2())
    buys = [t for t in result["trades"] if t["side"] == "BUY"]
    print("\n[COMPLETE DAY] buys:", buys)
    assert not buys, "correct 14:00 scan sees recovery → must NOT enter"
