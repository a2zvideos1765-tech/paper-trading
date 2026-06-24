"""Tests for the operational alert feed (src/core/alerts).

We verify the pure classification logic (what counts as a rate limit / order
reject / auth / plain error) and the Markdown debug-bundle rendering, with no
DB or filesystem coupling — the same posture as test_intents.
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

from src.core.alerts import build_debug_bundle, classify  # noqa: E402


# ---- classify ----

def test_angel_rate_limit_is_categorised():
    cat, sev = classify("WARNING", "angel rate limit — backing off",
                        "Access denied because of exceeding access rate")
    assert cat == "rate_limit"
    assert sev == "warning"


def test_access_denied_alone_is_rate_limit():
    cat, _ = classify("ERROR", "Couldn't reach Angel: access denied")
    assert cat == "rate_limit"


def test_order_rejection_is_categorised():
    cat, sev = classify("ERROR", "Angel placeOrder rejected [AB4036]: not allowed")
    assert cat == "order_reject"
    assert sev == "error"


def test_auth_session_is_categorised():
    cat, _ = classify("ERROR", "AngelSessionError: invalid token, re-login required")
    assert cat == "auth"


def test_plain_error_falls_through_to_error():
    cat, sev = classify("ERROR", "ZeroDivisionError in tick", "Traceback ...")
    assert cat == "error"
    assert sev == "error"


def test_plain_warning_falls_through_to_warning():
    cat, sev = classify("WARNING", "holdings drift detected")
    assert cat == "warning"
    assert sev == "warning"


def test_severity_tracks_level_even_for_rate_limit():
    # A rate-limit logged at ERROR (retries exhausted) is still a rate_limit, but error severity.
    cat, sev = classify("ERROR", "exceeding access rate after 3 retries")
    assert cat == "rate_limit"
    assert sev == "error"


# ---- build_debug_bundle ----

def _sample_data():
    return {
        "generated_at": "2026-06-24T18:55:00+05:30",
        "hours": 48,
        "summary": {"total": 2, "errors": 1, "warnings": 1, "rate_limits": 1,
                    "order_rejects": 1, "auth": 0, "stale_processes": 0},
        "alerts": [
            {"ts": "2026-06-24T14:03:00+05:30", "app": "real_trader", "level": "ERROR",
             "severity": "error", "category": "order_reject",
             "title": "BUY 2 UNIVCABLES rejected",
             "detail": "Angel placeOrder rejected [AB4036]: scrip not allowed"},
            {"ts": "2026-06-24T12:00:00+05:30", "app": "poller", "level": "WARNING",
             "severity": "warning", "category": "rate_limit",
             "title": "angel rate limit — backing off",
             "detail": "Access denied because of exceeding access rate"},
        ],
        "heartbeats": [
            {"app": "real_trader", "status": "ok", "detail": "replay completed",
             "last_beat": "2026-06-24T18:54:00+05:30", "stale": False},
        ],
    }


def test_bundle_has_header_summary_and_alerts():
    md = build_debug_bundle(_sample_data(), host="srv1501974")
    assert "# Paper Trading — Debug Bundle" in md
    assert "Host: `srv1501974`" in md
    assert "Angel rate-limit hits: **1**" in md
    assert "Order rejections: **1**" in md
    # Each alert appears with its detail fenced in a code block.
    assert "AB4036" in md
    assert "exceeding access rate" in md
    assert "real_trader · order_reject" in md


def test_bundle_handles_empty_feed():
    data = _sample_data()
    data["alerts"] = []
    data["summary"] = {k: 0 for k in data["summary"]}
    md = build_debug_bundle(data, host="h")
    assert "clean" in md.lower()
