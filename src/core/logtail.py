"""Shared reader for the JSON-per-line app logs written by setup_logging().

Used by the /bot logs panel (web) and the MCP tail_logs tool so both surface logs
identically.

Note on date folders: setup_logging() pins each process's log file to the date the
process STARTED (logs/<start-date>/<app>.log). A runner that has been up for days
therefore keeps writing to an older folder. So we don't guess today/yesterday — we
glob every date folder for the app and pick the file most recently written to.
"""

from __future__ import annotations

import glob
import json
import os
import re

from src.core.config import settings


# Apps that write a logs/<date>/<app>.log via setup_logging().
KNOWN_APPS = {
    "real_trader", "trader", "poller", "backfill", "backfill_queue", "web", "mcp",
}


def read_log_tail(app: str, lines: int = 100) -> dict:
    """Return the last `lines` structured log entries for `app`.

    Result: {app, log_path, total_lines, tail_lines, entries:[...]} or {error,...}.
    Each entry is the parsed JSON object (ts/level/logger/msg + any extra=), or
    {"raw": "<line>"} for any line that wasn't valid JSON.
    """
    lines = max(1, min(int(lines), 1000))
    safe = re.sub(r"[^a-z0-9_\-]", "", str(app).lower())  # block path traversal
    if not safe:
        return {"error": "invalid app name", "entries": []}

    # Newest-written log file for this app across all date folders.
    matches = glob.glob(str(settings.log_dir / "*" / f"{safe}.log"))
    if not matches:
        return {"app": safe, "entries": [],
                "error": f"no log file found for {safe!r} (is the process running?)"}
    path = max(matches, key=os.path.getmtime)

    text = open(path, encoding="utf-8", errors="replace").read()
    all_lines = [ln for ln in text.splitlines() if ln.strip()]
    tail = all_lines[-lines:]

    entries = []
    for ln in tail:
        try:
            entries.append(json.loads(ln))
        except json.JSONDecodeError:
            entries.append({"raw": ln})

    return {
        "app": safe,
        "log_path": path,
        "total_lines": len(all_lines),
        "tail_lines": len(entries),
        "entries": entries,
    }
