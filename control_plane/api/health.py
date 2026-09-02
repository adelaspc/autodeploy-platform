from flask import Blueprint, current_app, jsonify
from sqlalchemy import text

from control_plane.api.auth import require_api_role
from control_plane.application.projects.read_models import platform_activity_payload, platform_status_payload
from control_plane.api.request_parsing import parse_limit_arg, parse_optional_int_arg, parse_status_filter_arg
from control_plane.extensions import db
from control_plane.models import PlatformDeployment


health_bp = Blueprint("health", __name__)


@health_bp.get("/health")
def health_check():
    # Liveness is intentionally lightweight and public: it only proves that the HTTP process can answer requests
    return jsonify({"status": "ok"}), 200


@health_bp.get("/health/ready") # public endpoint
def readiness_health_check():
    # Readiness also verifies the database because the API cannot serve its core workload without persistence
    try:
        db.session.execute(text("SELECT 1"))
    except Exception as exc:
        # Log only the exception type and return a stable error code so database credentials or internal addresses cannot leak through the response
        current_app.logger.warning(
            "API readiness check failed",
            extra={"event": "api_readiness_failed", "error_type": type(exc).__name__},
        )
        return jsonify({"status": "error", "error_code": "not_ready"}), 503

    return jsonify({"status": "ok"}), 200


@health_bp.get("/health/db") # protected endpoint
@require_api_role("read_only")
def database_health_check():
    # This operator-facing check exposes database reachability, so unlike the generic readiness endpoint it is protected by API authentication
    try:
        db.session.execute(text("SELECT 1"))
    except Exception as exc:
        current_app.logger.warning(
            "Database health check failed",
            extra={"event": "database_health_failed", "error_type": type(exc).__name__},
        )
        return jsonify({"status": "error", "database": "unreachable", "error_code": "database_unreachable"}), 503

    return jsonify({"status": "ok", "database": "reachable"}), 200


@health_bp.get("/health/platform")
@require_api_role("read_only")
def platform_health_check():
    # The read model summarizes executor, authentication, registry, and Kubernetes readiness without exposing their secret configuration values
    return jsonify(platform_status_payload()), 200


@health_bp.get("/health/observability")
@require_api_role("read_only")
def observability_health_check():
    from flask import current_app

    # Report the observability posture rather than sensitive values such as the metrics bearer token itself.
    log_format = (current_app.config.get("CONTROL_PLANE_LOG_FORMAT", "json") or "").strip().lower()
    return jsonify(
        {
            "status": "ok",
            "metrics": {
                "enabled": bool(current_app.config.get("CONTROL_PLANE_METRICS_ENABLED", False)),
                "endpoint": "/metrics",
                "authentication": "dedicated_bearer_token",
            },
            "logging": {
                "format": log_format or "text",
                "structured": log_format == "json",
                "destination": "stdout",
            },
            "request_correlation": {
                "enabled": True,
                "header": "X-Request-ID",
            },
        }
    ), 200


@health_bp.get("/health/activity")
@require_api_role("read_only")
def platform_activity_health_check():
    from flask import request

    # Each collection has its own bounded page size because the response combines several deployment and webhook activity feeds.
    latest_limit, error_response, status_code = parse_limit_arg("latest_limit", default=10, request=request)
    if error_response is not None:
        return error_response, status_code
    active_limit, error_response, status_code = parse_limit_arg("active_limit", default=10, request=request)
    if error_response is not None:
        return error_response, status_code
    failed_limit, error_response, status_code = parse_limit_arg("failed_limit", default=10, request=request)
    if error_response is not None:
        return error_response, status_code
    ignored_webhook_limit, error_response, status_code = parse_limit_arg(
        "ignored_webhook_limit",
        default=10,
        request=request,
    )
    if error_response is not None:
        return error_response, status_code
    accepted_webhook_limit, error_response, status_code = parse_limit_arg(
        "accepted_webhook_limit",
        default=10,
        request=request,
    )
    if error_response is not None:
        return error_response, status_code
    deployment_statuses, error_response, status_code = parse_status_filter_arg(
        "deployment_status",
        request=request,
        allowed_values=PlatformDeployment.VALID_STATUSES,
    )
    if error_response is not None:
        return error_response, status_code
    webhook_statuses, error_response, status_code = parse_status_filter_arg(
        "webhook_status",
        request=request,
        allowed_values=("accepted", "ignored"),
    )
    if error_response is not None:
        return error_response, status_code
    # Optional filters and cursor IDs let operators narrow the feed and request older pages without results shifting when new activity is recorded.
    project_id, error_response, status_code = parse_optional_int_arg("project_id", request=request, min_value=1)
    if error_response is not None:
        return error_response, status_code
    before_deployment_id, error_response, status_code = parse_optional_int_arg(
        "before_deployment_id",
        request=request,
        min_value=1,
    )
    if error_response is not None:
        return error_response, status_code
    before_webhook_delivery_id, error_response, status_code = parse_optional_int_arg(
        "before_webhook_delivery_id",
        request=request,
        min_value=1,
    )
    if error_response is not None:
        return error_response, status_code
    return jsonify(
        platform_activity_payload(
            latest_limit=latest_limit,
            active_limit=active_limit,
            failed_limit=failed_limit,
            ignored_webhook_limit=ignored_webhook_limit,
            accepted_webhook_limit=accepted_webhook_limit,
            deployment_statuses=deployment_statuses,
            webhook_statuses=webhook_statuses,
            project_id=project_id,
            before_deployment_id=before_deployment_id,
            before_webhook_delivery_id=before_webhook_delivery_id,
        )
    ), 200
