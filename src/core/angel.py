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
