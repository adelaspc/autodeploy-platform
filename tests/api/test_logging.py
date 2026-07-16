import json
import logging
import pytest

from control_plane.logging_config import JsonLogFormatter
from control_plane.security import REDACTED, redact_log_text


def test_json_log_formatter_emits_bounded_structured_context():
    record = logging.makeLogRecord(
        {
            "name": "control-plane",
            "levelno": logging.INFO,
            "levelname": "INFO",
            "msg": "request_completed request_id=req-1",
            "args": (),
            "event": "request_completed",
            "component": "api",
            "request_id": "req-1",
            "status_code": 200,
            "duration_ms": 12.5,
        }
    )

    payload = json.loads(JsonLogFormatter().format(record))

    assert payload["event"] == "request_completed"
    assert payload["component"] == "api"
    assert payload["request_id"] == "req-1"
    assert payload["status_code"] == 200
    assert payload["duration_ms"] == 12.5


@pytest.mark.parametrize(
    "message,secret",
    [
        ("failed mysql+pymysql://control:db-password@db.internal/control", "db-password"),
        ("Authorization: Bearer api-token-value", "api-token-value"),
        ("password=plain-secret", "plain-secret"),
        ("token: webhook-secret", "webhook-secret"),
    ],
)
def test_log_redaction_removes_common_secret_shapes(message, secret):
    sanitized = redact_log_text(message)

    assert secret not in sanitized
    assert REDACTED in sanitized


def test_json_log_formatter_redacts_exception_text():
    try:
        raise RuntimeError("database mysql://user:sensitive-password@db.internal/app")
    except RuntimeError:
        record = logging.makeLogRecord(
            {
                "name": "control-plane",
                "levelno": logging.ERROR,
                "levelname": "ERROR",
                "msg": "database request failed",
                "args": (),
                "exc_info": __import__("sys").exc_info(),
            }
        )

    payload = json.loads(JsonLogFormatter().format(record))

    assert "sensitive-password" not in payload["exception"]
    assert REDACTED in payload["exception"]
