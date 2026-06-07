"""Read-only end-to-end check of the multi-regime stack on the live VPS DB.

Proves that S228 / S283 will work when the market opens, WITHOUT writing anything
to the database (no trades, no positions, no equity snapshots are persisted).

What it checks:
  1. INDIA_VIX is in the universe as kind='index'.
  2. NIFTY_50 + INDIA_VIX have enough daily (1d) history for a 50-DMA.
  3. All strategies load from the registry; the schema drift check passes.
  4. The regime classifier runs on real NIFTY+VIX data — prints the bull/bear/
     sideways split and the most recent few days' regime.
  5. run_backtest_v2 runs S228 + S283 against a real window of 5m equity candles
     — proving the multi-regime engine path executes on live data.

Run on the VPS:
    python -m tools.verify_regime
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta

from src.core.db import close_pool, fetch, fetchrow, get_pool
from src.core.time import now_ist
from src.engine.replay import load_candles_window, load_index_close
from src.engine.v2_engine import (
    ChargeConfigV2,
    classify_regime_by_date,
    clear_regime_cache,
    prime_regime_index,
    run_backtest_v2,
)
from src.strategies.registry import all_strategies, get
from src.strategies.schema import assert_schema_covers_dataclass, public_schema


CHECK = "  [PASS]"
FAIL = "  [FAIL]"


async def main() -> None:
    await get_pool()
    problems: list[str] = []
    try:
        # ---- 1. INDIA_VIX in universe ----------------------------------
        print("1. INDIA_VIX in the universe")
        row = await fetchrow(
            "SELECT symbol, exchange, token, kind, enabled FROM universe_symbols "
            "WHERE symbol = 'INDIA_VIX'"
        )
        if row and row["kind"] == "index" and row["enabled"]:
            print(f"{CHECK} INDIA_VIX — token {row['token']}, {row['exchange']}, kind=index, enabled")
        else:
            problems.append("INDIA_VIX missing/disabled in universe_symbols")
            print(f"{FAIL} INDIA_VIX not found as an enabled index — run sql/005_india_vix.sql")

        # ---- 2. Daily history depth ------------------------------------
        print("\n2. Daily (1d) history depth for the regime classifier")
        for sym in ("NIFTY_50", "INDIA_VIX"):
            r = await fetchrow(
                "SELECT count(*) AS n, min(ts)::date AS lo, max(ts)::date AS hi "
                "FROM candles WHERE symbol = $1 AND interval = '1d'",
                sym,
            )
            n = int(r["n"]) if r else 0
            if n >= 50:
                print(f"{CHECK} {sym}: {n} 1d bars  ({r['lo']} → {r['hi']})")
            else:
                problems.append(f"{sym} has only {n} 1d bars (<50)")
                print(f"{FAIL} {sym}: {n} 1d bars — need ≥50; run tools/load_regime_history.py")

        # ---- 3. Registry + schema --------------------------------------
        print("\n3. Strategy registry + schema")
        try:
            assert_schema_covers_dataclass()
            reg = all_strategies()
            print(f"{CHECK} schema drift check passed; {len(reg)} strategies loaded; "
                  f"{len(public_schema())} editable fields")
            for s in ("S50_drop3_e15_alloc10_vol11", "S228_multiregime_bear_pyr_small",
                      "S283_mm_dma_classic", "S404_s392_side_only"):
                if s in reg:
                    print(f"{CHECK} {s} present")
                else:
                    problems.append(f"{s} not in registry")
                    print(f"{FAIL} {s} missing")
        except Exception as exc:  # noqa: BLE001
            problems.append(f"registry/schema error: {exc}")
            print(f"{FAIL} {exc}")

        # ---- 4. Regime classifier on real data -------------------------
        print("\n4. Regime classifier on real NIFTY + VIX data")
        nifty = await load_index_close("NIFTY_50", interval="1d")
        sensex = await load_index_close("SENSEX", interval="1d")
        vix = await load_index_close("INDIA_VIX", interval="1d")
        clear_regime_cache()
        if not nifty.empty:
            prime_regime_index("NIFTY_50", nifty)
        if not sensex.empty:
            prime_regime_index("SENSEX", sensex)
        if not vix.empty:
            prime_regime_index("INDIA_VIX", vix)
        regime = classify_regime_by_date(vix_bear_threshold=20.0, vix_only_bear=False)
        if regime.empty:
            problems.append("classify_regime_by_date returned empty")
            print(f"{FAIL} regime classifier produced nothing — NIFTY 1d data not reaching it")
        else:
            counts = regime.value_counts().to_dict()
            print(f"{CHECK} classified {len(regime)} days — "
                  f"bull={counts.get('bull', 0)} bear={counts.get('bear', 0)} "
                  f"sideways={counts.get('sideways', 0)}")
            tail = regime.tail(5)
            print("       last 5 classified days: "
                  + ", ".join(f"{d}={r}" for d, r in tail.items()))

        # ---- 5. Run the engine on real candles -------------------------
        print("\n5. run_backtest_v2 on real 5m candles (read-only — nothing is saved)")
        eq_rows = await fetch(
            "SELECT DISTINCT symbol FROM candles WHERE interval = '5m' "
            "AND symbol NOT IN ('NIFTY_50', 'SENSEX', 'INDIA_VIX') "
            "ORDER BY symbol LIMIT 8"
        )
        symbols = [r["symbol"] for r in eq_rows]
        until = now_ist().replace(second=0, microsecond=0)
        since = until - timedelta(days=300)
        candles = await load_candles_window(symbols, "5m", since, until)
        print(f"       loaded {len(candles)} candle rows for {len(symbols)} symbols "
              f"({since.date()} → {until.date()})")
        if candles.empty:
            problems.append("no 5m candles available for the engine test")
            print(f"{FAIL} no 5m candles in the last 300 days — cannot exercise the engine")
        else:
            for name in ("S228_multiregime_bear_pyr_small", "S283_mm_dma_classic",
                         "S404_s392_side_only"):
                try:
                    strat = replace(get(name), starting_cash=100000.0)
                    result = run_backtest_v2(candles, strat, ChargeConfigV2())
                    print(f"{CHECK} {name}: {len(result['trades'])} trades, "
                          f"{len(result['open_positions'])} open, "
                          f"final equity ₹{result['summary']['final_equity']:,.0f}")
                except Exception as exc:  # noqa: BLE001
                    problems.append(f"{name} engine run failed: {exc}")
                    print(f"{FAIL} {name}: {exc}")

        # ---- Verdict ---------------------------------------------------
        print("\n" + "=" * 60)
        if problems:
            print(f"VERIFICATION FAILED — {len(problems)} problem(s):")
            for p in problems:
                print(f"  - {p}")
            raise SystemExit(1)
        print("VERIFICATION PASSED — multi-regime stack is live and working.")
        print("Note: this ran the engine on the FULL candle window for a true")
        print("engine test. The live trader is forward-only, so S228/S283")
        print("portfolios will only trade on bars dated after they started.")
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
