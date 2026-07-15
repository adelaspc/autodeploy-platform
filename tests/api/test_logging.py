import json
import logging

from control_plane.logging_config import JsonLogFormatter


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
