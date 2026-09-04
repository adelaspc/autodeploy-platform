"""Emit structured logs with request and deployment correlation fields."""

import json
import logging
from datetime import datetime, timezone

from control_plane.security import redact_log_text


STRUCTURED_FIELDS = (
    "component",
    "event",
    "request_id",
    "method",
    "path",
    "status_code",
    "duration_ms",
    "project_id",
    "deployment_id",
    "build_id",
    "command_id",
    "error_type",
    "final_status",
    "reconciliation_actions",
    "poll_interval_seconds",
)


class JsonLogFormatter(logging.Formatter):
    def __init__(self, component=None):
        super().__init__()
        self.component = component

    def format(self, record):
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "event": getattr(record, "event", None) or record.getMessage().split(" ", 1)[0],
            "message": redact_log_text(record.getMessage()),
        }
        for field in STRUCTURED_FIELDS:
            value = getattr(record, field, None)
            if field == "component" and value is None:
                value = self.component
            if value is not None and field != "event":
                payload[field] = value
        if record.exc_info:
            payload["exception"] = redact_log_text(self.formatException(record.exc_info))
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def install_structured_logging(app):
    if app.config.get("CONTROL_PLANE_LOG_FORMAT", "json").lower() != "json":
        return
    formatter = JsonLogFormatter(app.config.get("CONTROL_PLANE_COMPONENT", "api"))
    for handler in app.logger.handlers:
        handler.setFormatter(formatter)
