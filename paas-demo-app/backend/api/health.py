from flask import Blueprint, jsonify
from sqlalchemy import text

from backend.api.auth import require_api_role
from backend.api.platform_read_models import platform_activity_payload, platform_status_payload
from backend.api.request_parsing import parse_limit_arg, parse_optional_int_arg, parse_status_filter_arg
from backend.extensions import db
from backend.models import PlatformDeployment


health_bp = Blueprint("health", __name__)


@health_bp.get("/health")
def health_check():
    return jsonify({"status": "ok"}), 200


@health_bp.get("/health/db")
@require_api_role("read_only")
def database_health_check():
    try:
        db.session.execute(text("SELECT 1"))
    except Exception as exc:
        return jsonify({"status": "error", "database": "unreachable", "details": str(exc)}), 503

    return jsonify({"status": "ok", "database": "reachable"}), 200


@health_bp.get("/health/platform")
@require_api_role("read_only")
def platform_health_check():
    return jsonify(platform_status_payload()), 200


@health_bp.get("/health/activity")
@require_api_role("read_only")
def platform_activity_health_check():
    from flask import request

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
