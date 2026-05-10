"""Smoke tests for the universe loader — YAML bootstrap fallback.

The DB-first path is exercised by the live system; here we just verify the
sync YAML reader still works (it's the fallback path) and the SymbolSpec
dataclass.
"""

from __future__ import annotations

import os

# Provide minimal env so `settings` doesn't blow up.
os.environ.setdefault("PG_PASSWORD", "test")
os.environ.setdefault("ANGEL_API_KEY", "test")
os.environ.setdefault("ANGEL_CLIENT_CODE", "test")
os.environ.setdefault("ANGEL_PASSWORD", "test")
os.environ.setdefault("ANGEL_TOTP_SECRET", "JBSWY3DPEHPK3PXP")
os.environ.setdefault("DASHBOARD_PASSWORD", "test")
os.environ.setdefault("SESSION_SECRET", "test-secret")

from src.core.universe import SymbolSpec, load_universe_sync  # noqa: E402


def test_yaml_loads_equities_and_indices():
    eq, idx = load_universe_sync()
    # Project ships with 10 equities and 2 indices in config/universe.yaml.
    assert len(eq) >= 1, "at least one equity expected in YAML"
    assert all(isinstance(s, SymbolSpec) for s in eq)
    assert all(isinstance(s, SymbolSpec) for s in idx)


def test_symbol_spec_is_frozen():
    s = SymbolSpec(symbol="X", token="1", exchange="NSE")
    try:
        s.symbol = "Y"  # type: ignore[misc]
        raise AssertionError("SymbolSpec should be frozen")
    except Exception:
        pass


def test_yaml_tokens_unique():
    eq, idx = load_universe_sync()
    tokens = [s.token for s in [*eq, *idx]]
    assert len(tokens) == len(set(tokens)), "duplicate tokens in universe.yaml"
