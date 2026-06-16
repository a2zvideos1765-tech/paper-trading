"""get_candle must distinguish an expired session from genuine no-data.

This is the regression guard for the silent-poller bug: an expired daily token
made Angel return status=false, which the old code treated as "no data" and
returned an empty frame — so the poller wrote 0 rows all day with no error.
Now an auth-failure status must RAISE so the runner re-logs in.
"""

from __future__ import annotations

import os
from datetime import datetime

os.environ.setdefault("PG_PASSWORD", "test")
os.environ.setdefault("ANGEL_API_KEY", "test")
os.environ.setdefault("ANGEL_CLIENT_CODE", "test")
os.environ.setdefault("ANGEL_PASSWORD", "test")
os.environ.setdefault("ANGEL_TOTP_SECRET", "JBSWY3DPEHPK3PXP")
os.environ.setdefault("DASHBOARD_PASSWORD", "test")
os.environ.setdefault("SESSION_SECRET", "test-secret-do-not-use")

import pytest  # noqa: E402

from src.core.angel import AngelClient, AngelSessionError  # noqa: E402


class _FakeSmart:
    """Stand-in for SmartConnect.getCandleData returning a canned response."""
    def __init__(self, response):
        self._response = response

    def getCandleData(self, params):
        return self._response


_DT = datetime(2026, 6, 16, 14, 0)


def _client(response) -> AngelClient:
    return AngelClient(smart=_FakeSmart(response), account=1)


def test_expired_token_raises_session_error():
    c = _client({"status": False, "message": "Invalid Token"})
    with pytest.raises(AngelSessionError):
        c.get_candle("RELIANCE", "2885", "NSE", "5m", _DT, _DT, max_retries=0)


def test_token_expired_message_raises():
    c = _client({"status": False, "message": "Token Expired", "errorcode": "AG8002"})
    with pytest.raises(AngelSessionError):
        c.get_candle("TCS", "11536", "NSE", "5m", _DT, _DT, max_retries=0)


def test_genuine_no_data_returns_empty_not_raise():
    # status true, no rows — a holiday / illiquid bar. Must NOT raise.
    c = _client({"status": True, "data": []})
    df = c.get_candle("RELIANCE", "2885", "NSE", "5m", _DT, _DT, max_retries=0)
    assert df.empty


def test_nonauth_status_false_returns_empty():
    # status false but not an auth message — log + empty, don't raise.
    c = _client({"status": False, "message": "Something benign"})
    df = c.get_candle("RELIANCE", "2885", "NSE", "5m", _DT, _DT, max_retries=0)
    assert df.empty


def test_valid_data_is_parsed():
    c = _client({"status": True, "data": [
        ["2026-06-16T14:00:00+05:30", 100.0, 101.0, 99.0, 100.5, 1234],
    ]})
    df = c.get_candle("RELIANCE", "2885", "NSE", "5m", _DT, _DT, max_retries=0)
    assert len(df) == 1
    assert df.iloc[0]["symbol"] == "RELIANCE"
    assert df.iloc[0]["close"] == 100.5
