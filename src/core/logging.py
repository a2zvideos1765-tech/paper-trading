"""Structured JSON logging for runners + the web app.

Each line is a single JSON object so future Claude/coding agents can grep, jq, or
ingest into ELK. PM2 wraps stdout/stderr but we also write to logs/{date}/{app}.log
for permanent rotation-friendly storage.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.core.config import settings


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:  # noqa: D401
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Pull anything the caller passed via `extra=`.
        for k, v in record.__dict__.items():
            if k in {"msg", "args", "levelname", "levelno", "pathname", "filename",
                     "module", "exc_info", "exc_text", "stack_info", "lineno",
                     "funcName", "created", "msecs", "relativeCreated", "thread",
                     "threadName", "processName", "process", "name", "message",
                     "taskName"}:
                continue
            try:
                json.dumps(v)
                payload[k] = v
            except (TypeError, ValueError):
                payload[k] = repr(v)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def setup_logging(app: str) -> logging.Logger:
    """Initialise root logger for an app (poller/trader/web/backfill).
    Writes JSON to stdout (PM2 picks this up) AND to logs/{date}/{app}.log.
    """
    _IST = timezone(timedelta(hours=5, minutes=30))
    log_dir = settings.log_dir / datetime.now(_IST).strftime("%Y-%m-%d")
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{app}.log"

    formatter = JsonFormatter()

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)

    file = logging.FileHandler(log_path, encoding="utf-8")
    file.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(stream)
    root.addHandler(file)
    root.setLevel(settings.log_level)

    return logging.getLogger(app)
