"""Indian charge model — golden tests against engine_v2.delivery_charges_v2."""

from __future__ import annotations

import os

os.environ.setdefault("PG_PASSWORD", "test")
os.environ.setdefault("ANGEL_API_KEY", "test")
os.environ.setdefault("ANGEL_CLIENT_CODE", "test")
os.environ.setdefault("ANGEL_PASSWORD", "test")
os.environ.setdefault("ANGEL_TOTP_SECRET", "JBSWY3DPEHPK3PXP")
os.environ.setdefault("DASHBOARD_PASSWORD", "test")
os.environ.setdefault("SESSION_SECRET", "test-secret-do-not-use")

import pytest  # noqa: E402

from src.engine.v2_engine import ChargeConfigV2, delivery_charges_v2  # noqa: E402


CHARGES = ChargeConfigV2()


def test_buy_10000_charges():
    """Hand-calculated reference for a ₹10,000 BUY:
      brokerage   = 0
      stt         = round(10000 * 0.001)      = 10
      exchange    = 10000 * 0.0000307         = 0.307
      sebi        = 10000 * 0.000001          = 0.01
      gst         = (0 + 0.307 + 0.01) * 0.18 = 0.05706
      stamp (buy) = 10000 * 0.00015           = 1.5
      dp          = 0
      ── total   = 10 + 0.307 + 0.01 + 0.05706 + 1.5 = 11.87406
    """
    fee = delivery_charges_v2(10000.0, "BUY", CHARGES)
    assert fee == pytest.approx(11.87406, abs=1e-5)


def test_sell_10000_charges():
    """Same turnover, SELL side: no stamp, plus DP fee.
      brokerage = 0
      stt       = 10
      exchange  = 0.307
      sebi      = 0.01
      gst       = 0.05706
      stamp     = 0  (sell)
      dp        = 15.34
      ── total = 25.71406
    """
    fee = delivery_charges_v2(10000.0, "SELL", CHARGES)
    assert fee == pytest.approx(25.71406, abs=1e-5)


def test_zero_turnover_only_dp_on_sell():
    assert delivery_charges_v2(0.0, "BUY",  CHARGES) == pytest.approx(0.0)
    assert delivery_charges_v2(0.0, "SELL", CHARGES) == pytest.approx(15.34)


def test_charges_scale_linearly_buy():
    """Doubling turnover should ~double charges (modulo the rupee-rounded STT)."""
    a = delivery_charges_v2(50000.0,  "BUY", CHARGES)
    b = delivery_charges_v2(100000.0, "BUY", CHARGES)
    # STT rounds to nearest rupee — so be generous on tolerance
    assert b == pytest.approx(2 * a, rel=0.001)
