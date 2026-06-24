"""Operational alert feed for the dashboard notification bell + a downloadable
debug bundle.

One place that answers "what's gone wrong lately?" by aggregating three sources:

  1. **App logs** (logs/<date>/<app>.log) — every WARNING / ERROR line written by
     setup_logging(), including exception tracebacks (the `exc` field).
  2. **Process heartbeats** (runs table) — runners that are errored or stale (>5 min
     since last beat).
  3. **Real-order rejections** (real_orders) — broker rejects (AB4036 surveillance,
     AG7002 IP-whitelist, insufficient funds, …) with their raw error text.

Angel rate-limit hits show up via (1): src/core/angel.py logs a WARNING on every
backoff, and classify() tags any "exceeding access rate" / "access denied" line as
`rate_limit`.

Everything here is read-only. The web layer gates it behind admin (logs can carry
order detail), mirroring /api/bot/logs.
"""

from __future__ import annotations

import glob
import json
import os
import re
import socket
from datetime import datetime, timedelta, timezone

from src.core.config import settings
from src.core.db import fetch
from src.core.logtail import KNOWN_APPS
from src.core.time import IST, now_ist


# Substrings (lowercased) that classify a log line / error into a category.
_RATE_HINTS = ("exceeding access rate", "access denied", "rate limit", "too many request")
_REJECT_HINTS = ("rejected [", "order rejected", "placeorder rejected")
_AUTH_HINTS = ("session expired", "invalid token", "invalid session", "jwt", "unauthor",
               "token expired", "re-login", "relogin")

# Bound the work: tail at most this many lines per log file, keep at most this many
# alerts per app, and never return a feed larger than this overall.
_TAIL_LINES = 4000
_PER_APP_CAP = 150
_FEED_CAP = 400


def classify(level: str, msg: str, exc: str = "") -> tuple[str, str]:
    """Map a log line to (category, severity).

    category: rate_limit | order_reject | auth | error | warning
    severity: error | warning   (drives the badge colour)
    """
    text = f"{msg}\n{exc}".lower()
    if any(h in text for h in _RATE_HINTS):
        category = "rate_limit"
    elif any(h in text for h in _REJECT_HINTS):
        category = "order_reject"
    elif any(h in text for h in _AUTH_HINTS):
        category = "auth"
    else:
        category = "error" if level == "ERROR" else "warning"
    severity = "error" if level == "ERROR" else "warning"
    return category, severity


def _ist_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST).isoformat()


def _app_log_files_since(app: str, since: datetime) -> list[str]:
    """Every logs/<date>/<app>.log file that was written to within the window.

    A long-running runner (e.g. poller up for days) keeps appending to its
    start-date folder, so its file has a recent mtime even though the folder is
    old — `mtime >= since` catches both that and a freshly-restarted process."""
    safe = re.sub(r"[^a-z0-9_\-]", "", str(app).lower())
    if not safe:
        return []
    matches = glob.glob(str(settings.log_dir / "*" / f"{safe}.log"))
    since_ts = since.timestamp()
    files = [p for p in matches if os.path.getmtime(p) >= since_ts]
    if not files and matches:
        # Quiet-but-present app: still scan its newest file so a stale error shows.
        files = [max(matches, key=os.path.getmtime)]
    return files


def _scan_file(app: str, path: str, since: datetime) -> list[dict]:
    """Parse WARNING/ERROR JSON lines in `path` that are at or after `since`."""
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return []
    out: list[dict] = []
    for ln in text.splitlines()[-_TAIL_LINES:]:
        ln = ln.strip()
        if not ln or '"level"' not in ln:
            continue
        try:
            e = json.loads(ln)
        except json.JSONDecodeError:
            continue
        level = e.get("level")
        if level not in ("WARNING", "ERROR"):
            continue
        try:
            ts = datetime.fromisoformat(e["ts"])
        except (KeyError, ValueError):
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts < since:
            continue

        msg = str(e.get("msg", ""))
        exc = str(e.get("exc", "")) if e.get("exc") else ""
        category, severity = classify(level, msg, exc)

        # Compact dump of any extra=… fields the caller attached, for context.
        skip = {"ts", "level", "logger", "msg", "exc"}
        extras = {k: v for k, v in e.items() if k not in skip}
        detail_parts = [msg]
        if extras:
            detail_parts.append(json.dumps(extras, default=str))
        if exc:
            detail_parts.append(exc)

        out.append({
            "ts": _ist_iso(ts),
            "ts_utc": ts.astimezone(timezone.utc).isoformat(),
            "app": app,
            "source": e.get("logger", app),
            "level": level,
            "severity": severity,
            "category": category,
            "title": msg[:200] or f"({level})",
            "detail": "\n\n".join(p for p in detail_parts if p),
        })
    return out


def collect_log_alerts(hours: int) -> list[dict]:
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    out: list[dict] = []
    for app in sorted(KNOWN_APPS):
        app_alerts: list[dict] = []
        for path in _app_log_files_since(app, since):
            app_alerts.extend(_scan_file(app, path, since))
        app_alerts.sort(key=lambda a: a["ts_utc"], reverse=True)
        out.extend(app_alerts[:_PER_APP_CAP])
    return out


async def _collect_heartbeats() -> tuple[list[dict], list[dict]]:
    """Return (full heartbeat list for the bundle, alert rows for errored/stale)."""
    rows = await fetch("SELECT app, last_beat, status, detail FROM runs ORDER BY app")
    now = datetime.now(timezone.utc)
    heartbeats: list[dict] = []
    alerts: list[dict] = []
    for r in rows:
        last = r["last_beat"]
        stale = (now - last) > timedelta(minutes=5)
        heartbeats.append({
            "app": r["app"],
            "status": r["status"],
            "detail": r["detail"],
            "last_beat": _ist_iso(last),
            "stale": stale,
        })
        if r["status"] == "error" or stale:
            cat = "stale" if stale else "error"
            mins = int((now - last).total_seconds() // 60)
            title = (f"process '{r['app']}' is stale — no heartbeat for {mins} min"
                     if stale else f"process '{r['app']}' reported an error")
            alerts.append({
                "ts": _ist_iso(last),
                "ts_utc": last.astimezone(timezone.utc).isoformat(),
                "app": r["app"],
                "source": "runs",
                "level": "ERROR",
                "severity": "error",
                "category": cat,
                "title": title,
                "detail": (r["detail"] or "(no detail)") + f"\n\nlast_beat: {_ist_iso(last)} IST",
            })
    return heartbeats, alerts


async def _collect_order_rejections(hours: int) -> list[dict]:
    rows = await fetch(
        """
        SELECT symbol, side, qty, status, error, reason, requested_at
        FROM real_orders
        WHERE status IN ('rejected', 'error')
          AND requested_at >= now() - ($1 * interval '1 hour')
        ORDER BY requested_at DESC
        LIMIT 100
        """,
        hours,
    )
    out: list[dict] = []
    for r in rows:
        ts = r["requested_at"]
        err = r["error"] or "(no error text recorded)"
        category, _ = classify("ERROR", err)
        if category not in ("rate_limit", "auth", "order_reject"):
            category = "order_reject"
        out.append({
            "ts": _ist_iso(ts),
            "ts_utc": ts.astimezone(timezone.utc).isoformat(),
            "app": "real_trader",
            "source": "real_orders",
            "level": "ERROR",
            "severity": "error",
            "category": category,
            "title": f"{r['side']} {r['qty']} {r['symbol']} {r['status']}",
            "detail": f"{err}\n\nreason: {r['reason']}",
        })
    return out


async def collect_alerts(hours: int = 24) -> dict:
    """Unified, newest-first alert feed plus a summary and full heartbeat list."""
    hours = max(1, min(int(hours), 168))  # clamp 1h..7d
    log_alerts = collect_log_alerts(hours)
    heartbeats, hb_alerts = await _collect_heartbeats()
    order_alerts = await _collect_order_rejections(hours)

    alerts = log_alerts + hb_alerts + order_alerts
    alerts.sort(key=lambda a: a["ts_utc"], reverse=True)
    alerts = alerts[:_FEED_CAP]

    summary = {
        "total": len(alerts),
        "errors": sum(1 for a in alerts if a["severity"] == "error"),
        "warnings": sum(1 for a in alerts if a["severity"] == "warning"),
        "rate_limits": sum(1 for a in alerts if a["category"] == "rate_limit"),
        "order_rejects": sum(1 for a in alerts if a["category"] == "order_reject"),
        "auth": sum(1 for a in alerts if a["category"] == "auth"),
        "stale_processes": sum(1 for a in alerts if a["category"] == "stale"),
    }
    return {
        "generated_at": now_ist().isoformat(),
        "hours": hours,
        "summary": summary,
        "alerts": alerts,
        "heartbeats": heartbeats,
    }


def build_debug_bundle(data: dict, host: str | None = None) -> str:
    """Render collect_alerts() output as a single Markdown document designed to be
    pasted straight into Claude for diagnosis."""
    host = host or socket.gethostname()
    s = data["summary"]
    lines: list[str] = []
    lines.append("# Paper Trading — Debug Bundle")
    lines.append("")
    lines.append(f"- Generated: **{data['generated_at']}** (IST)")
    lines.append(f"- Window: last **{data['hours']}h**")
    lines.append(f"- Host: `{host}`")
    lines.append("")
    lines.append("## Summary")
    lines.append(f"- Errors: **{s['errors']}**")
    lines.append(f"- Warnings: **{s['warnings']}**")
    lines.append(f"- Angel rate-limit hits: **{s['rate_limits']}**")
    lines.append(f"- Order rejections: **{s['order_rejects']}**")
    lines.append(f"- Auth/session issues: **{s['auth']}**")
    lines.append(f"- Stale/errored processes: **{s['stale_processes']}**")
    lines.append("")

    lines.append("## Process heartbeats")
    if data["heartbeats"]:
        lines.append("| app | status | last beat (IST) | stale? | detail |")
        lines.append("|---|---|---|---|---|")
        for h in data["heartbeats"]:
            detail = (h["detail"] or "").replace("|", "\\|").replace("\n", " ")
            lines.append(
                f"| {h['app']} | {h['status']} | {h['last_beat']} | "
                f"{'YES' if h['stale'] else 'no'} | {detail} |"
            )
    else:
        lines.append("_(no heartbeats recorded)_")
    lines.append("")

    lines.append(f"## Errors, warnings & rate limits (newest first, {len(data['alerts'])} events)")
    if not data["alerts"]:
        lines.append("")
        lines.append("_No alerts in the window — clean._")
    for a in data["alerts"]:
        lines.append("")
        lines.append(f"### [{a['level']}] {a['app']} · {a['category']} · {a['ts']}")
        lines.append("")
        lines.append("```")
        lines.append(a["detail"].strip() or a["title"])
        lines.append("```")
    lines.append("")
    return "\n".join(lines)
