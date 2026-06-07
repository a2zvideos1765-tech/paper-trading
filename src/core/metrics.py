"""Portfolio performance metrics shared by the dashboard + portfolio detail.

Estimated APY annualises the *current* return so far via CAGR (compound annual
growth rate). It is an extrapolation, not a guarantee — and it is wild for the
first few days of live trading (a +2% move over 5 days annualises to a silly
number), so we return None until the portfolio has at least `MIN_DAYS_LIVE` days
of history. The UI shows "—" in that warm-up window.
"""

from __future__ import annotations

from datetime import datetime

from src.core.time import now_ist


# Below this many days live, CAGR extrapolation is noise — show "—" instead.
MIN_DAYS_LIVE = 7


def estimated_apy(equity: float, capital: float, started_at: datetime | None) -> float | None:
    """CAGR as a percent (e.g. 42.5 == +42.5%/yr), or None while warming up.

    CAGR = (equity / capital) ** (365 / days_live) − 1, expressed in percent.
    Returns None when inputs are unusable (non-positive capital/equity, no
    started_at, or fewer than MIN_DAYS_LIVE days of history).
    """
    if not started_at or capital <= 0 or equity <= 0:
        return None
    # started_at is tz-aware (DB UTC); now_ist() is tz-aware IST — subtraction is tz-safe.
    days_live = (now_ist() - started_at).total_seconds() / 86400.0
    if days_live < MIN_DAYS_LIVE:
        return None
    growth = equity / capital
    cagr = growth ** (365.0 / days_live) - 1.0
    return cagr * 100.0
