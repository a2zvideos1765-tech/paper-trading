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

    web_host: str
    web_port: int
    dashboard_password: str
    viewer_password: str | None
    session_secret: str

    poller_interval_seconds: int
    trader_interval_seconds: int
    trader_offset_seconds: int

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
        web_host=_opt("WEB_HOST", "127.0.0.1"),
        web_port=int(_opt("WEB_PORT", "8000")),
        dashboard_password=_req("DASHBOARD_PASSWORD"),
        viewer_password=(os.getenv("VIEWER_PASSWORD") or None),
        session_secret=_req("SESSION_SECRET"),
        poller_interval_seconds=int(_opt("POLLER_INTERVAL_SECONDS", "60")),
        trader_interval_seconds=int(_opt("TRADER_INTERVAL_SECONDS", "60")),
        trader_offset_seconds=int(_opt("TRADER_OFFSET_SECONDS", "5")),
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
