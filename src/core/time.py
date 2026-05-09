"""IST market clock helpers. NSE/BSE trade Mon-Fri 09:15-15:30 IST."""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone


IST = timezone(timedelta(hours=5, minutes=30))

MARKET_OPEN  = time(9, 15)
MARKET_CLOSE = time(15, 30)


def now_ist() -> datetime:
    return datetime.now(IST)


def is_market_open(at: datetime | None = None) -> bool:
    """True only Mon-Fri between 09:15 and 15:30 IST. Doesn't know about holidays —
    that's fine for paper trading; trades just won't fire on holiday data."""
    at = at or now_ist()
    if at.weekday() >= 5:
        return False
    t = at.timetz().replace(tzinfo=None)
    return MARKET_OPEN <= t <= MARKET_CLOSE


def seconds_until_market_open(at: datetime | None = None) -> float:
    """Seconds until the next market open (skipping weekends).
    Useful for sleeping the poller/trader between sessions without burning CPU."""
    at = at or now_ist()
    candidate = at.replace(hour=MARKET_OPEN.hour, minute=MARKET_OPEN.minute,
                           second=0, microsecond=0)
    if at.timetz().replace(tzinfo=None) >= MARKET_OPEN and at.weekday() < 5:
        # already past today's open — go to tomorrow
        candidate = candidate + timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate = candidate + timedelta(days=1)
    return max(0.0, (candidate - at).total_seconds())


def floor_to_minute(at: datetime) -> datetime:
    return at.replace(second=0, microsecond=0)
