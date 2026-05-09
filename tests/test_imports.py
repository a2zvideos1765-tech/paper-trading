"""Smoke test: every module that PM2 will load must at least import cleanly.

Catches typos / circular imports / missing deps without needing Postgres or Angel."""

from __future__ import annotations

import os

os.environ.setdefault("PG_PASSWORD", "test")
os.environ.setdefault("ANGEL_API_KEY", "test")
os.environ.setdefault("ANGEL_CLIENT_CODE", "test")
os.environ.setdefault("ANGEL_PASSWORD", "test")
os.environ.setdefault("ANGEL_TOTP_SECRET", "JBSWY3DPEHPK3PXP")
os.environ.setdefault("DASHBOARD_PASSWORD", "test")
os.environ.setdefault("SESSION_SECRET", "test-secret-do-not-use")


def test_core_imports():
    import src.core.config       # noqa: F401
    import src.core.db           # noqa: F401
    import src.core.time         # noqa: F401
    import src.core.logging      # noqa: F401
    import src.core.universe     # noqa: F401


def test_engine_imports():
    import src.engine.v2_engine  # noqa: F401
    import src.engine.replay     # noqa: F401


def test_strategies_import():
    import src.strategies.registry  # noqa: F401
    import src.strategies.s1_user_pyramid  # noqa: F401
    import src.strategies.s6_tiered_exit   # noqa: F401
    import src.strategies.s10_rsi_filter   # noqa: F401


def test_runners_import():
    # We don't run them, just import. They each call setup_logging() at module
    # scope, which needs the LOG_DIR env, but that has a default in config.py.
    import src.runners.web        # noqa: F401
    import src.runners.poller     # noqa: F401
    import src.runners.trader     # noqa: F401
    import src.runners.backfill   # noqa: F401


def test_web_app_imports_and_mounts():
    """Build the FastAPI app and assert all expected routes are mounted."""
    from src.web.app import app
    paths = {r.path for r in app.routes}
    expected = {"/", "/login", "/logout", "/health", "/trades",
                "/portfolio/{portfolio_id}",
                "/diagnose/{portfolio_id}/{symbol}/{ts}",
                "/api/portfolio/{portfolio_id}/state",
                "/api/portfolio/{portfolio_id}/equity"}
    missing = expected - paths
    assert not missing, f"missing routes: {missing}; got: {sorted(paths)}"
