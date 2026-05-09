"""Uvicorn entrypoint for the FastAPI dashboard."""

from __future__ import annotations

import uvicorn

from src.core.config import settings


def main() -> None:
    uvicorn.run(
        "src.web.app:app",
        host=settings.web_host,
        port=settings.web_port,
        log_level=settings.log_level.lower(),
        access_log=True,
        # PM2 is the supervisor; we don't want uvicorn doing reloads in prod.
        reload=False,
    )


if __name__ == "__main__":
    main()
