from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        extra_keys = {
            "source",
            "url",
            "status",
            "inserted",
            "skipped",
            "failed",
            "count",
            "total",
            "valid",
            "invalid",
            "listing_pages",
            "reason",
            "db_path",
            "config_path",
            "table",
            "total_jobs",
            "fts_rebuilt",
        }
        for key, value in record.__dict__.items():
            if key.startswith("job_") or key in extra_keys:
                payload[key] = value
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
