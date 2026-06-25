"""Centralised env loading. Every entrypoint imports `settings` from here."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(REPO_ROOT / ".env")


def _req(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(
            f"Missing required env var {name!r}. "
            f"Copy .env.example to .env on the VPS and fill it in."
        )
    return val


def _opt(name: str, default: str) -> str:
    return os.getenv(name) or default


@dataclass(frozen=True)
class Settings:
    pg_host: str
    pg_port: int
    pg_db: str
    pg_user: str
    pg_password: str

    angel_api_key: str
    angel_client_code: str
    angel_password: str
    angel_totp_secret: str

    # Optional second Angel One account. When fully configured, market-data
    # fetching (poller/backfill) moves to it by default, leaving account 1
    # dedicated to real trading — see ANGEL_DATA_ACCOUNT / ANGEL_TRADING_ACCOUNT.
    angel2_api_key: str | None
    angel2_client_code: str | None
    angel2_password: str | None
    angel2_totp_secret: str | None

    angel_data_account: str     # 'auto' | '1' | '2' — account for candles/backfill
    angel_trading_account: str  # '1' | '2' — account the real-money bot trades on

    web_host: str
    web_port: int
    dashboard_password: str
    viewer_password: str | None
    session_secret: str

    poller_interval_seconds: int
    trader_interval_seconds: int
    trader_offset_seconds: int

    # How many calendar days back the real bot may still place an engine signal.
    # 1 (default) = today or yesterday, so a signal whose candle arrived late
    # (after market close) is still placed next session. 0 = strict today-only.
    real_trader_intent_max_age_days: int

    log_level: str
    log_dir: Path

    mcp_token: str | None        # Bearer token for the MCP server (None = MCP disabled)
    mcp_host: str
    mcp_port: int

    @property
    def pg_dsn(self) -> str:
        return (
            f"postgresql://{self.pg_user}:{self.pg_password}"
            f"@{self.pg_host}:{self.pg_port}/{self.pg_db}"
        )

    @property
    def has_angel_account2(self) -> bool:
        return all([self.angel2_api_key, self.angel2_client_code,
                    self.angel2_password, self.angel2_totp_secret])


def resolve_angel_account(configured: str, has_account2: bool) -> int:
    """Map an ANGEL_*_ACCOUNT selector ('auto' | '1' | '2') to an account number.

    'auto' picks account 2 when it is fully configured, else account 1. Pure —
    no settings access — so the routing logic is unit-testable.
    """
    v = (configured or "auto").strip().lower()
    if v == "auto":
        return 2 if has_account2 else 1
    if v in ("1", "2"):
        n = int(v)
        if n == 2 and not has_account2:
            raise RuntimeError(
                "Angel account 2 selected but ANGEL2_API_KEY / ANGEL2_CLIENT_CODE / "
                "ANGEL2_PASSWORD / ANGEL2_TOTP_SECRET are not all set in .env"
            )
        return n
    raise RuntimeError(
        f"Invalid Angel account selector {configured!r} — use 'auto', '1' or '2'"
    )


def load_settings() -> Settings:
    return Settings(
        pg_host=_opt("PG_HOST", "127.0.0.1"),
        pg_port=int(_opt("PG_PORT", "5432")),
        pg_db=_opt("PG_DB", "paper_trading"),
        pg_user=_opt("PG_USER", "paper"),
        pg_password=_req("PG_PASSWORD"),
        angel_api_key=_req("ANGEL_API_KEY"),
        angel_client_code=_req("ANGEL_CLIENT_CODE"),
        angel_password=_req("ANGEL_PASSWORD"),
        angel_totp_secret=_req("ANGEL_TOTP_SECRET"),
        angel2_api_key=(os.getenv("ANGEL2_API_KEY") or None),
        angel2_client_code=(os.getenv("ANGEL2_CLIENT_CODE") or None),
        angel2_password=(os.getenv("ANGEL2_PASSWORD") or None),
        angel2_totp_secret=(os.getenv("ANGEL2_TOTP_SECRET") or None),
        angel_data_account=_opt("ANGEL_DATA_ACCOUNT", "auto"),
        angel_trading_account=_opt("ANGEL_TRADING_ACCOUNT", "1"),
        web_host=_opt("WEB_HOST", "127.0.0.1"),
        web_port=int(_opt("WEB_PORT", "8000")),
        dashboard_password=_req("DASHBOARD_PASSWORD"),
        viewer_password=(os.getenv("VIEWER_PASSWORD") or None),
        session_secret=_req("SESSION_SECRET"),
        # 150s, not 60s: the poller sweeps the universe sequentially with ~1.25s
        # rate-limit pacing per symbol, so N symbols cost ~1.25*N seconds before
        # any network time. At ~73 symbols a 60s tick is physically impossible and
        # the sweep falls minutes behind real-time. Live bars are 5-min, so 150s
        # still refreshes the forming bar 2-3x before it finalises. Override per VPS.
        poller_interval_seconds=int(_opt("POLLER_INTERVAL_SECONDS", "150")),
        trader_interval_seconds=int(_opt("TRADER_INTERVAL_SECONDS", "60")),
        trader_offset_seconds=int(_opt("TRADER_OFFSET_SECONDS", "5")),
        real_trader_intent_max_age_days=int(_opt("REAL_TRADER_INTENT_MAX_AGE_DAYS", "1")),
        log_level=_opt("LOG_LEVEL", "INFO").upper(),
        log_dir=REPO_ROOT / _opt("LOG_DIR", "logs"),
        mcp_token=(os.getenv("MCP_TOKEN") or None),
        mcp_host=_opt("MCP_HOST", "127.0.0.1"),
        mcp_port=int(_opt("MCP_PORT", "8001")),
    )


# Lazy singleton — most callers use `from src.core.config import settings`
class _LazySettings:
    _val: Settings | None = None

    def __getattr__(self, item: str):
        if self._val is None:
            object.__setattr__(self, "_val", load_settings())
        return getattr(self._val, item)


settings = _LazySettings()
