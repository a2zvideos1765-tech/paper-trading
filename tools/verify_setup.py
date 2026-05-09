"""End-to-end smoke verification — run on the VPS after first deploy, or any
time something looks wrong. Each check is independent so partial failures still
print useful diagnostics. Exit code is 0 only if everything passes.

This is what future Claude / coding agents will run first when asked to debug
the system. Each check prints a clear PASS/FAIL with detail.

Usage:  python -m tools.verify_setup [--skip-angel]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import traceback
from datetime import datetime, timezone

from src.core.db import close_pool, fetch, fetchrow, get_pool
from src.core.universe import load_universe
from src.strategies.registry import all_strategies


PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
WARN = "\033[33mWARN\033[0m"


class Result:
    def __init__(self) -> None:
        self.failed = 0
        self.passed = 0
        self.warned = 0

    def ok(self, name: str, detail: str = "") -> None:
        self.passed += 1
        print(f"  {PASS}  {name}{(' — ' + detail) if detail else ''}")

    def warn(self, name: str, detail: str = "") -> None:
        self.warned += 1
        print(f"  {WARN}  {name}{(' — ' + detail) if detail else ''}")

    def fail(self, name: str, detail: str = "") -> None:
        self.failed += 1
        print(f"  {FAIL}  {name}{(' — ' + detail) if detail else ''}")


# ---- checks ----

async def check_db_round_trip(r: Result) -> None:
    print("DB connectivity")
    try:
        row = await fetchrow("SELECT 1 AS one")
        assert row and row["one"] == 1
        r.ok("connect + select 1")
    except Exception as exc:  # noqa: BLE001
        r.fail("connect + select 1", str(exc))


async def check_schema(r: Result) -> None:
    print("Schema")
    expected = {"candles", "portfolios", "positions", "trades", "equity_snapshots", "runs"}
    rows = await fetch(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
    )
    found = {row["tablename"] for row in rows}
    missing = expected - found
    if missing:
        r.fail("required tables present", f"missing: {sorted(missing)}")
    else:
        r.ok("required tables present", f"{len(expected)} tables")

    # Check the dedupe unique index
    idx = await fetch(
        "SELECT indexname FROM pg_indexes WHERE tablename = 'trades'"
    )
    if any(i["indexname"] == "trades_portfolio_dedupe" for i in idx):
        r.ok("trades dedupe index present")
    else:
        r.fail("trades dedupe index present",
               "expected unique index trades_portfolio_dedupe — replay will duplicate trades without it")


async def check_timescale(r: Result) -> None:
    print("TimescaleDB (optional)")
    try:
        rows = await fetch(
            "SELECT extname FROM pg_extension WHERE extname = 'timescaledb'"
        )
        if rows:
            r.ok("timescaledb extension installed")
        else:
            r.warn("timescaledb not installed",
                   "system will work but you'll lose time-series query optimizations")
    except Exception as exc:  # noqa: BLE001
        r.warn("timescaledb check failed", str(exc))


def check_strategies(r: Result) -> None:
    print("Strategies")
    try:
        reg = all_strategies()
        if not reg:
            r.fail("registry has at least 1 strategy", "registry is empty")
        else:
            r.ok("registry loaded", ", ".join(sorted(reg)))
    except Exception as exc:  # noqa: BLE001
        r.fail("registry import failed", str(exc))


def check_universe(r: Result) -> None:
    print("Universe config")
    try:
        eq, idx = load_universe()
        if not eq:
            r.fail("universe.yaml has at least one equity", "symbols list is empty")
        else:
            r.ok(f"loaded {len(eq)} equities, {len(idx)} indices")
        seen_tokens = set()
        for spec in [*eq, *idx]:
            if spec.token in seen_tokens:
                r.fail("token uniqueness", f"duplicate token {spec.token}")
                return
            seen_tokens.add(spec.token)
        r.ok("all tokens unique")
    except Exception as exc:  # noqa: BLE001
        r.fail("universe load failed", str(exc))


async def check_portfolios_synced(r: Result) -> None:
    print("Portfolios")
    try:
        rows = await fetch("SELECT name, strategy_id, capital::float8, enabled FROM portfolios")
        if not rows:
            r.warn("no portfolios in DB", "run the trader once to bootstrap from portfolios.yaml")
            return
        enabled = sum(1 for r2 in rows if r2["enabled"])
        r.ok(f"{len(rows)} portfolios in DB ({enabled} enabled)")
        # Each enabled portfolio's strategy must be in the registry.
        reg = set(all_strategies().keys())
        for row in rows:
            if row["enabled"] and row["strategy_id"] not in reg:
                r.fail("strategy reference",
                       f"portfolio {row['name']!r} references unknown strategy {row['strategy_id']!r}")
                return
        r.ok("all enabled portfolios reference known strategies")
    except Exception as exc:  # noqa: BLE001
        r.fail("portfolios check failed", str(exc))


async def check_runner_heartbeats(r: Result) -> None:
    print("Runner heartbeats")
    rows = await fetch("SELECT app, last_beat, status, detail FROM runs")
    if not rows:
        r.warn("no heartbeats yet", "start poller/trader/backfill at least once")
        return
    now = datetime.now(timezone.utc)
    for row in rows:
        age = (now - row["last_beat"]).total_seconds()
        if age > 600 and row["status"] != "sleeping":
            r.warn(f"{row['app']} heartbeat stale", f"{int(age)}s ago, status={row['status']}")
        else:
            r.ok(f"{row['app']} heartbeat", f"{int(age)}s ago, status={row['status']}")


def check_angel_login(r: Result) -> None:
    print("Angel SmartAPI login")
    try:
        from src.core.angel import AngelClient
        client = AngelClient.login()
        r.ok("login + TOTP succeeded")
    except SystemExit as exc:
        r.fail("login failed", str(exc))
    except Exception as exc:  # noqa: BLE001
        r.fail("login crashed", str(exc))


async def check_candles_present(r: Result) -> None:
    print("Candles table")
    row = await fetchrow("SELECT count(*) AS n, max(ts) AS latest, min(ts) AS earliest FROM candles")
    if not row or not row["n"]:
        r.warn("candles table empty", "run tools/load_history.py to bulk-import")
        return
    r.ok(f"{row['n']:,} rows", f"{row['earliest']} → {row['latest']}")


# ---- main ----

async def _run(skip_angel: bool) -> int:
    r = Result()

    # synchronous (no DB) checks first
    check_strategies(r)
    check_universe(r)
    if not skip_angel:
        check_angel_login(r)

    # async checks
    try:
        await get_pool()
        await check_db_round_trip(r)
        await check_schema(r)
        await check_timescale(r)
        await check_portfolios_synced(r)
        await check_runner_heartbeats(r)
        await check_candles_present(r)
    finally:
        await close_pool()

    print()
    print(f"Summary: {r.passed} pass · {r.warned} warn · {r.failed} fail")
    return 0 if r.failed == 0 else 1


def main() -> None:
    p = argparse.ArgumentParser(description="End-to-end paper-trading verification.")
    p.add_argument("--skip-angel", action="store_true",
                   help="Skip Angel SmartAPI login check (offline / no creds yet)")
    args = p.parse_args()
    try:
        rc = asyncio.run(_run(args.skip_angel))
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        rc = 2
    sys.exit(rc)


if __name__ == "__main__":
    main()
