from __future__ import annotations

from collections.abc import Mapping, Sequence

from flask import current_app, request

from control_plane.api.request_context import current_request_id
from control_plane.extensions import db
from control_plane.models import AuditEvent
from control_plane.security import REDACTED, redact_sensitive_data


def sanitize_audit_metadata(value):
    if value is None:
        return None
    if isinstance(value, Mapping):
        sanitized = {}
        for key, item in value.items():
            if key == "env_vars":
                sanitized[key] = REDACTED
            else:
                sanitized[key] = sanitize_audit_metadata(item)
        return sanitized
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [sanitize_audit_metadata(item) for item in value]
    return redact_sensitive_data(value)


def client_ip_address():
    return request.remote_addr


def record_audit_event(
    *,
    action,
    resource_type,
    resource_id=None,
    status="success",
    actor_role=None,
    metadata=None,
):
    from control_plane.api.auth import current_request_actor_role

    sanitized_metadata = sanitize_audit_metadata(metadata)
    resolved_actor_role = actor_role if actor_role is not None else current_request_actor_role()
    payload = {
        "action": action,
        "actor_role": resolved_actor_role,
        "resource_type": resource_type,
        "resource_id": str(resource_id) if resource_id is not None else None,
        "status": status,
        "request_id": current_request_id(),
        "ip_address": client_ip_address(),
        "metadata_json": sanitized_metadata,
    }

    try:
        with db.engine.begin() as connection:
            connection.execute(AuditEvent.__table__.insert().values(**payload))
    except Exception as exc:
        current_app.logger.warning(
            "audit_event_record_failed",
            extra={
                "action": action,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "status": status,
                "error": str(exc),
            },
        )


def list_audit_events(*, limit, before_id=None):
    query = AuditEvent.query.order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
    if before_id is not None:
        query = query.filter(AuditEvent.id < before_id)
    return query.limit(limit).all()


def serialize_audit_events_page(events, *, limit):
    return {
        "items": [event.to_dict() for event in events],
        "pagination": {
            "limit": limit,
            "count": len(events),
            "next_before_id": events[-1].id if events else None,
        },
    }
