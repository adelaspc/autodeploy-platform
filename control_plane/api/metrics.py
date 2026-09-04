"""Expose bounded operational counters in Prometheus text format."""

import hmac
from datetime import timezone

from flask import Blueprint, Response, current_app, jsonify, request
from sqlalchemy import func

from control_plane.api.request_context import error_payload
from control_plane.extensions import db
from control_plane.models import Build, DeploymentEvent, PlatformDeployment, WebhookDelivery


metrics_bp = Blueprint("metrics", __name__)
PROMETHEUS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def _bearer_token():
    authorization = request.headers.get("Authorization")
    if not isinstance(authorization, str):
        return None
    parts = authorization.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


def _unauthorized():
    response = jsonify(error_payload("Invalid or missing metrics bearer token"))
    response.headers["WWW-Authenticate"] = "Bearer"
    return response, 401


def _status_counts(model):
    return dict(db.session.query(model.status, func.count(model.id)).group_by(model.status).all())


def _metric_family(lines, name, help_text, metric_type="gauge"):
    lines.extend((f"# HELP {name} {help_text}", f"# TYPE {name} {metric_type}"))


def _timestamp_seconds(value):
    if value is None:
        return 0
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


def render_metrics():
    lines = []

    _metric_family(lines, "control_plane_deployments", "Current deployments by status.")
    deployment_counts = _status_counts(PlatformDeployment)
    for status in PlatformDeployment.VALID_STATUSES:
        lines.append(f'control_plane_deployments{{status="{status}"}} {deployment_counts.get(status, 0)}')

    _metric_family(lines, "control_plane_builds", "Current builds by status.")
    build_counts = _status_counts(Build)
    for status in Build.VALID_STATUSES:
        lines.append(f'control_plane_builds{{status="{status}"}} {build_counts.get(status, 0)}')

    _metric_family(lines, "control_plane_webhook_deliveries", "Recorded webhook deliveries by result.")
    webhook_counts = _status_counts(WebhookDelivery)
    for status in ("accepted", "ignored"):
        lines.append(f'control_plane_webhook_deliveries{{result="{status}"}} {webhook_counts.get(status, 0)}')

    _metric_family(lines, "control_plane_deployment_events", "Recorded deployment events by level.")
    event_counts = dict(
        db.session.query(DeploymentEvent.level, func.count(DeploymentEvent.id)).group_by(DeploymentEvent.level).all()
    )
    for level in ("info", "warning", "error"):
        lines.append(f'control_plane_deployment_events{{level="{level}"}} {event_counts.get(level, 0)}')

    _metric_family(lines, "control_plane_claimed_deployments", "Deployments currently owned by a worker.")
    claimed = PlatformDeployment.query.filter(PlatformDeployment.claimed_at.isnot(None)).count()
    lines.append(f"control_plane_claimed_deployments {claimed}")

    _metric_family(
        lines,
        "control_plane_latest_deployment_update_timestamp_seconds",
        "Unix timestamp of the most recent deployment update, or zero when no deployment exists.",
    )
    latest_update = db.session.query(func.max(PlatformDeployment.updated_at)).scalar()
    lines.append(
        "control_plane_latest_deployment_update_timestamp_seconds "
        f"{_timestamp_seconds(latest_update):.3f}"
    )

    return "\n".join(lines) + "\n"


@metrics_bp.get("/metrics")
def metrics():
    if not current_app.config.get("CONTROL_PLANE_METRICS_ENABLED", False):
        return jsonify(error_payload("Not found")), 404

    configured_token = current_app.config["CONTROL_PLANE_METRICS_TOKEN"]
    supplied_token = _bearer_token()
    if supplied_token is None or not hmac.compare_digest(supplied_token, configured_token):
        return _unauthorized()

    response = Response(render_metrics(), content_type=PROMETHEUS_CONTENT_TYPE)
    response.headers["Cache-Control"] = "no-store"
    return response
