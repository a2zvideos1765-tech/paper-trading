"""place_order must surface Angel's real rejection reason, not a bare 'empty id'.

Regression guard for the live bug where Angel's placeOrder returned nothing and
the old code raised an opaque 'returned an empty order id'. We now prefer
placeOrderFullResponse and include the actual error code / message / params.
"""

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

from src.core.angel import AngelClient  # noqa: E402


class _FakeFull:
    """SDK exposing placeOrderFullResponse (modern smartapi-python)."""
    def __init__(self, resp):
        self._resp = resp
        self.last_params = None

    def placeOrderFullResponse(self, params):
        self.last_params = params
        return self._resp


class _FakeOld:
    """Old SDK: only placeOrder, returns the order-id string or None."""
    def __init__(self, resp):
        self._resp = resp
        self.last_params = None

    def placeOrder(self, params):
        self.last_params = params
        return self._resp


def _place(smart, price=1399.40):
    c = AngelClient(smart=smart, account=1)
    return c.place_order("AUROPHARMA-EQ", "212", "NSE", "BUY", 4, price)


def test_full_response_success_returns_orderid():
    oid = _place(_FakeFull({"status": True, "data": {"orderid": "240616000123"}}))
    assert oid == "240616000123"


def test_rejection_surfaces_errorcode_and_message():
    with pytest.raises(RuntimeError) as e:
        _place(_FakeFull({"status": False, "errorcode": "AB1018",
                          "message": "Insufficient funds"}))
    assert "AB1018" in str(e.value)
    assert "Insufficient funds" in str(e.value)


def test_none_response_raises_with_params_context():
    with pytest.raises(RuntimeError) as e:
        _place(_FakeFull(None))
    msg = str(e.value)
    assert "no response" in msg.lower()
    assert "AUROPHARMA-EQ" in msg   # params included for diagnosis
    assert "NSE" in msg


def test_old_sdk_string_orderid():
    assert _place(_FakeOld("ORD999")) == "ORD999"


def test_old_sdk_none_raises():
    with pytest.raises(RuntimeError):
        _place(_FakeOld(None))


def test_price_snapped_to_tick_grid():
    fake = _FakeFull({"status": True, "data": {"orderid": "1"}})
    _place(fake, price=1399.43)          # 1399.43 → nearest 0.05 → 1399.45
    assert fake.last_params["price"] == "1399.45"
