"""Attach one correlation ID to each request, response, event, and audit record."""

import re
from time import perf_counter
from uuid import uuid4

from flask import current_app, has_request_context, request


REQUEST_ID_HEADER = "X-Request-ID"
REQUEST_ID_ENV_KEY = "control_plane.request_id"
REQUEST_ID_MAX_LENGTH = 128
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
REQUEST_STARTED_ENV_KEY = "control_plane.request_started"


def generate_request_id():
    return uuid4().hex


def normalize_request_id(value):
    if not isinstance(value, str):
        return generate_request_id()
    candidate = value.strip()
    if not candidate or len(candidate) > REQUEST_ID_MAX_LENGTH or not REQUEST_ID_RE.fullmatch(candidate):
        return generate_request_id()
    return candidate


def current_request_id():
    if not has_request_context():
        return None
    return request.environ.get(REQUEST_ID_ENV_KEY)


def set_request_id_for_current_request():
    incoming = request.headers.get(REQUEST_ID_HEADER)
    request_id = normalize_request_id(incoming)
    request.environ[REQUEST_ID_ENV_KEY] = request_id
    request.environ[REQUEST_STARTED_ENV_KEY] = perf_counter()
    return request_id


def attach_request_id_header(response):
    request_id = current_request_id()
    if request_id:
        response.headers[REQUEST_ID_HEADER] = request_id
    return response


def error_payload(message, **extra):
    payload = {"error": message}
    request_id = current_request_id()
    if request_id:
        payload["request_id"] = request_id
    payload.update(extra)
    return payload


class RequestIdLogFilter:
    def filter(self, record):
        if getattr(record, "request_id", None) is None:
            record.request_id = current_request_id()
        return True


def install_request_logging(app):
    request_id_filter = RequestIdLogFilter()
    app.logger.addFilter(request_id_filter)
    for handler in app.logger.handlers:
        handler.addFilter(request_id_filter)


def log_request_completed(response):
    request_id = current_request_id()
    started_at = request.environ.get(REQUEST_STARTED_ENV_KEY)
    duration_ms = round((perf_counter() - started_at) * 1000, 3) if started_at is not None else None
    current_app.logger.info(
        "request_completed request_id=%s method=%s path=%s status=%s",
        request_id,
        request.method,
        request.path,
        response.status_code,
        extra={
            "event": "request_completed",
            "component": current_app.config.get("CONTROL_PLANE_COMPONENT", "api"),
            "request_id": request_id,
            "method": request.method,
            "path": request.path,
            "status_code": response.status_code,
            "duration_ms": duration_ms,
        },
    )
    return response
