import json
import logging
import sys
from datetime import datetime, timezone

from flask import has_request_context, request


LOG_RECORD_RESERVED_FIELDS = {
    "args",
    "asctime",
    "created",
    "exc_info",
    "exc_text",
    "filename",
    "funcName",
    "levelname",
    "levelno",
    "lineno",
    "message",
    "module",
    "msecs",
    "msg",
    "name",
    "pathname",
    "process",
    "processName",
    "relativeCreated",
    "stack_info",
    "thread",
    "threadName",
    "taskName",
}


class JsonLogFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        if has_request_context():
            payload.update(
                {
                    "method": request.method,
                    "path": request.path,
                    "remote_addr": request.headers.get("X-Forwarded-For", request.remote_addr),
                    "request_id": request.headers.get("X-Request-Id"),
                }
            )

        for key, value in record.__dict__.items():
            if key not in LOG_RECORD_RESERVED_FIELDS and not key.startswith("_"):
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str, separators=(",", ":"))


class MaxLevelFilter(logging.Filter):
    def __init__(self, max_level):
        super().__init__()
        self.max_level = max_level

    def filter(self, record):
        return record.levelno < self.max_level


def configure_json_logging(app):
    log_level = app.config.get("LOG_LEVEL", "INFO")
    formatter = JsonLogFormatter()
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)
    stdout_handler.addFilter(MaxLevelFilter(logging.ERROR))

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(formatter)
    stderr_handler.setLevel(logging.ERROR)

    app.logger.handlers.clear()
    app.logger.addHandler(stdout_handler)
    app.logger.addHandler(stderr_handler)
    app.logger.propagate = False
    app.logger.setLevel(log_level)
