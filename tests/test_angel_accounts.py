"""Tests for Angel One multi-account routing (src/core/config.resolve_angel_account).

The resolver is pure (no settings/env access) so the role → account mapping —
which decides which account real money trades on — is verifiable in isolation.
"""

from __future__ import annotations

import pytest

from src.core.config import resolve_angel_account


def test_auto_prefers_account2_when_configured():
    assert resolve_angel_account("auto", has_account2=True) == 2


def test_auto_falls_back_to_account1_when_unconfigured():
    assert resolve_angel_account("auto", has_account2=False) == 1


def test_blank_and_none_are_treated_as_auto():
    assert resolve_angel_account("", has_account2=False) == 1
    assert resolve_angel_account(None, has_account2=True) == 2


def test_explicit_account1_always_works():
    assert resolve_angel_account("1", has_account2=True) == 1
    assert resolve_angel_account("1", has_account2=False) == 1


def test_explicit_account2_requires_configuration():
    assert resolve_angel_account("2", has_account2=True) == 2
    with pytest.raises(RuntimeError, match="ANGEL2_"):
        resolve_angel_account("2", has_account2=False)


def test_invalid_selector_raises():
    for bad in ("3", "0", "both", "x"):
        with pytest.raises(RuntimeError, match="Invalid Angel account selector"):
            resolve_angel_account(bad, has_account2=True)


def test_selector_is_case_and_whitespace_tolerant():
    assert resolve_angel_account(" AUTO ", has_account2=True) == 2
    assert resolve_angel_account(" 2 ", has_account2=True) == 2
