"""Angel One SmartAPI client — auth + rate-limited candle fetch.

Ported from algo project's angel_download.py:108–119 (login) and :302–318 (retry).
We don't import the algo project's module so this repo stays self-contained.

Usage:
    client = AngelClient.login()
    bars = client.get_candle("RELIANCE", "2885", "NSE", "ONE_MINUTE",
                             from_dt=datetime(2026,5,9,9,15),
                             to_dt=datetime(2026,5,9,15,30))
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pandas as pd
import pyotp
from SmartApi.smartConnect import SmartConnect

from src.core.config import settings


# Map our internal interval strings to Angel's enum values.
INTERVAL_MAP = {
    "1m":  "ONE_MINUTE",
    "3m":  "THREE_MINUTE",
    "5m":  "FIVE_MINUTE",
    "15m": "FIFTEEN_MINUTE",
    "30m": "THIRTY_MINUTE",
    "1h":  "ONE_HOUR",
    "1d":  "ONE_DAY",
}


def _clean_totp_secret(value: str) -> str:
    return value.strip().replace(" ", "").replace("-", "").upper()


@dataclass
class AngelClient:
    smart: SmartConnect

    @classmethod
    def login(cls) -> "AngelClient":
        # SmartConnect spams INFO; silence to keep our JSON logs clean.
        logging.disable(logging.CRITICAL)

        secret = _clean_totp_secret(settings.angel_totp_secret)
        if secret.isdigit() and len(secret) == 6:
            raise SystemExit(
                "ANGEL_TOTP_SECRET must be the QR/manual setup key, "
                "not the 6-digit OTP. Re-enable 2FA and copy the setup key."
            )

        smart = SmartConnect(api_key=settings.angel_api_key)
        totp = pyotp.TOTP(secret).now()
        session = smart.generateSession(
            settings.angel_client_code,
            settings.angel_password,
            totp,
        )
        logging.disable(logging.NOTSET)

        if not session.get("status"):
            raise SystemExit(f"Angel login failed: {session}")
        return cls(smart=smart)

    # ------------------------------------------------------------------
    # Order placement + account reads (real-money trading).
    #
    # The same authenticated SmartConnect session used for candles handles
    # orders, funds, holdings and positions. Read calls are retry-wrapped
    # (safe to repeat). place_order is NOT retried — repeating an order risks
    # a duplicate fill — the caller records intent before calling and decides.
    # ------------------------------------------------------------------

    def place_order(
        self,
        tradingsymbol: str,
        token: str,
        exchange: str,
        side: str,
        qty: int,
        price: float,
        *,
        product: str = "DELIVERY",   # Angel's term for CNC / delivery
        order_type: str = "LIMIT",   # priced at the engine's decided price
        variety: str = "NORMAL",
        duration: str = "DAY",
    ) -> str:
        """Place a single equity order and return Angel's order id.

        Defaults are CNC/delivery LIMIT, DAY validity — the swing-strategy bot
        prices each order at the engine's decided trade price. Not retried.
        """
        params = {
            "variety": variety,
            "tradingsymbol": tradingsymbol,
            "symboltoken": str(token),
            "transactiontype": side.upper(),     # "BUY" | "SELL"
            "exchange": exchange,
            "ordertype": order_type,
            "producttype": product,
            "duration": duration,
            "price": f"{float(price):.2f}",
            "squareoff": "0",
            "stoploss": "0",
            "quantity": str(int(qty)),
        }
        resp = self.smart.placeOrder(params)
        # SDK versions differ: some return the order-id string directly, others a
        # dict {"status", "data": {"orderid": ...}}. Normalise to the id string.
        if isinstance(resp, dict):
            if not resp.get("status", True):
                raise RuntimeError(f"Angel placeOrder failed: {resp}")
            data = resp.get("data") or {}
            order_id = data.get("orderid") or data.get("orderId")
            if not order_id:
                raise RuntimeError(f"Angel placeOrder returned no order id: {resp}")
            return str(order_id)
        if not resp:
            raise RuntimeError("Angel placeOrder returned an empty order id")
        return str(resp)

    def cancel_order(self, order_id: str, variety: str = "NORMAL") -> dict:
        """Cancel an open order by Angel order id."""
        return _call_with_retry(lambda: self.smart.cancelOrder(order_id, variety), max_retries=2)

    def get_order_book(self) -> list[dict]:
        """Return the day's order book (list of order dicts). Empty list if none."""
        resp = _call_with_retry(self.smart.orderBook, max_retries=3)
        if isinstance(resp, dict):
            return resp.get("data") or []
        return resp or []

    def get_funds(self) -> dict:
        """Return the RMS limit / funds dict (availablecash, net, utiliseddebits, …)."""
        resp = _call_with_retry(self.smart.rmsLimit, max_retries=3)
        if isinstance(resp, dict):
            return resp.get("data") or {}
        return resp or {}

    def get_holdings(self) -> list[dict]:
        """Return the account's equity holdings (list of holding dicts)."""
        # allholding() carries per-holding rows + a totalholding summary; prefer it,
        # fall back to holding() on older SDKs.
        getter = getattr(self.smart, "allholding", None) or getattr(self.smart, "holding")
        resp = _call_with_retry(getter, max_retries=3)
        if isinstance(resp, dict):
            data = resp.get("data") or {}
            if isinstance(data, dict):
                return data.get("holdings") or []
            return data or []
        return resp or []

    def get_positions(self) -> list[dict]:
        """Return today's positions (list of position dicts). Empty list if none."""
        resp = _call_with_retry(self.smart.position, max_retries=3)
        if isinstance(resp, dict):
            return resp.get("data") or []
        return resp or []

    def get_ltp(self, exchange: str, tradingsymbol: str, token: str) -> float | None:
        """Return the last traded price for one instrument, or None if unavailable."""
        resp = _call_with_retry(
            lambda: self.smart.ltpData(exchange, tradingsymbol, str(token)),
            max_retries=3,
        )
        if isinstance(resp, dict):
            data = resp.get("data") or {}
            ltp = data.get("ltp")
            return float(ltp) if ltp is not None else None
        return None

    def get_candle(
        self,
        symbol: str,
        token: str,
        exchange: str,
        interval: str,
        from_dt: datetime,
        to_dt: datetime,
        max_retries: int = 5,
    ) -> pd.DataFrame:
        """Fetch candles for a single symbol within [from_dt, to_dt].

        Returns DataFrame with columns: timestamp, symbol, open, high, low, close, volume.
        Empty frame if Angel returns no data.
        """
        angel_interval = INTERVAL_MAP.get(interval, interval)
        params = {
            "exchange": exchange,
            "symboltoken": str(token),
            "interval": angel_interval,
            "fromdate": from_dt.strftime("%Y-%m-%d %H:%M"),
            "todate":   to_dt.strftime("%Y-%m-%d %H:%M"),
        }
        response = _get_candle_with_retry(self.smart, params, max_retries=max_retries)
        if not response.get("status") or not response.get("data"):
            return pd.DataFrame(columns=["timestamp", "symbol", "open", "high", "low", "close", "volume"])
        df = pd.DataFrame(
            response["data"],
            columns=["timestamp", "open", "high", "low", "close", "volume"],
        )
        df["symbol"] = symbol
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        return df[["timestamp", "symbol", "open", "high", "low", "close", "volume"]]


def _call_with_retry(fn, *, max_retries: int = 3):
    """Call a zero-arg Angel SDK function with the same rate-limit backoff used for
    candle fetches. For READ-only calls — never wrap order placement in this."""
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            msg = str(exc).lower()
            is_rate_limit = "exceeding access rate" in msg or "access denied" in msg
            if not is_rate_limit or attempt >= max_retries:
                raise
            time.sleep(min(90, 10 * (attempt + 1)))
    raise RuntimeError("Unreachable retry state")


def _get_candle_with_retry(smart: SmartConnect, params: dict[str, Any], max_retries: int) -> dict[str, Any]:
    """Mirrors the retry/backoff in algo project's angel_download.py:302-318."""
    for attempt in range(max_retries + 1):
        try:
            return smart.getCandleData(params)
        except Exception as exc:  # noqa: BLE001
            msg = str(exc).lower()
            is_rate_limit = "exceeding access rate" in msg or "access denied" in msg
            if not is_rate_limit or attempt >= max_retries:
                raise
            wait = min(90, 10 * (attempt + 1))
            time.sleep(wait)
    raise RuntimeError("Unreachable retry state")
