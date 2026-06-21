from sqlalchemy.exc import IntegrityError
from flask import Blueprint, abort, current_app, jsonify, request

from backend.extensions import db
from backend.models import Deployment


deployments_bp = Blueprint("deployments", __name__, url_prefix="/api/deployments")


def get_deployment_or_404(deployment_id):
    deployment = db.session.get(Deployment, deployment_id)
    if deployment is None:
        abort(404)
    return deployment


def validate_deployment_payload(payload, *, partial=False):
    required_fields = ("application_name", "version", "environment", "status")
    missing_fields = [field for field in required_fields if not payload.get(field)]
    if missing_fields and not partial:
        return f"Missing required fields: {', '.join(missing_fields)}"

    if "environment" in payload and payload["environment"] not in Deployment.VALID_ENVIRONMENTS:
        return (
            "Invalid environment. Expected one of: "
            + ", ".join(Deployment.VALID_ENVIRONMENTS)
        )

    if "status" in payload and payload["status"] not in Deployment.VALID_STATUSES:
        return "Invalid status. Expected one of: " + ", ".join(Deployment.VALID_STATUSES)

    return None


@deployments_bp.get("")
def list_deployments():
    query = Deployment.query

    environment = request.args.get("environment", type=str)
    status = request.args.get("status", type=str)
    application_name = request.args.get("application_name", type=str)

    if environment:
        query = query.filter(Deployment.environment == environment)

    if status:
        query = query.filter(Deployment.status == status)

    if application_name:
        query = query.filter(Deployment.application_name.ilike(f"%{application_name}%"))

    page = max(request.args.get("page", default=1, type=int), 1)
    per_page = request.args.get("per_page", default=10, type=int)
    per_page = min(max(per_page, 1), 50)

    pagination = query.order_by(Deployment.created_at.desc()).paginate(
        page=page,
        per_page=per_page,
        error_out=False,
    )
    return jsonify(
        {
            "items": [deployment.to_dict() for deployment in pagination.items],
            "page": pagination.page,
            "per_page": pagination.per_page,
            "total": pagination.total,
            "pages": pagination.pages,
            "has_next": pagination.has_next,
            "has_prev": pagination.has_prev,
        }
    )


@deployments_bp.get("/<int:deployment_id>")
def get_deployment(deployment_id):
    deployment = get_deployment_or_404(deployment_id)
    return jsonify(deployment.to_dict())


@deployments_bp.post("")
def create_deployment():
    payload = request.get_json(silent=True) or {}
    validation_error = validate_deployment_payload(payload)
    if validation_error:
        current_app.logger.warning(
            "Deployment create rejected",
            extra={"event": "deployment_create_rejected", "reason": validation_error},
        )
        return jsonify({"error": validation_error}), 400

    deployment = Deployment(
        application_name=payload["application_name"],
        version=payload["version"],
        environment=payload["environment"],
        status=payload["status"],
    )
    try:
        db.session.add(deployment)
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        current_app.logger.warning(
            "Deployment create conflict",
            extra={
                "event": "deployment_create_conflict",
                "application_name": payload["application_name"],
                "version": payload["version"],
                "environment": payload["environment"],
            },
        )
        return jsonify({"error": "Deployment already exists for this application, version, and environment"}), 409

    current_app.logger.info(
        "Deployment created",
        extra={
            "event": "deployment_created",
            "deployment_id": deployment.id,
            "application_name": deployment.application_name,
            "version": deployment.version,
            "environment": deployment.environment,
            "status": deployment.status,
        },
    )

    return jsonify(deployment.to_dict()), 201


@deployments_bp.patch("/<int:deployment_id>")
def update_deployment(deployment_id):
    deployment = get_deployment_or_404(deployment_id)
    payload = request.get_json(silent=True) or {}
    allowed_fields = {"status", "environment"}
    update_data = {key: value for key, value in payload.items() if key in allowed_fields}

    if not update_data:
        current_app.logger.warning(
            "Deployment update rejected",
            extra={
                "event": "deployment_update_rejected",
                "deployment_id": deployment.id,
                "reason": "no_updatable_fields",
            },
        )
        return jsonify({"error": "Provide at least one updatable field: status, environment"}), 400

    validation_error = validate_deployment_payload(update_data, partial=True)
    if validation_error:
        current_app.logger.warning(
            "Deployment update rejected",
            extra={
                "event": "deployment_update_rejected",
                "deployment_id": deployment.id,
                "reason": validation_error,
            },
        )
        return jsonify({"error": validation_error}), 400

    next_status = update_data.get("status")
    if next_status and not deployment.can_transition_to(next_status):
        current_app.logger.warning(
            "Deployment status transition rejected",
            extra={
                "event": "deployment_status_transition_rejected",
                "deployment_id": deployment.id,
                "current_status": deployment.status,
                "requested_status": next_status,
            },
        )
        return (
            jsonify(
                {
                    "error": (
                        f"Invalid status transition from {deployment.status} to {next_status}. "
                        f"Allowed transitions: {', '.join(deployment.allowed_transitions) or 'none'}"
                    )
                }
            ),
            409,
        )

    for key, value in update_data.items():
        setattr(deployment, key, value)

    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        current_app.logger.warning(
            "Deployment update conflict",
            extra={
                "event": "deployment_update_conflict",
                "deployment_id": deployment.id,
                "application_name": deployment.application_name,
                "version": deployment.version,
                "environment": deployment.environment,
            },
        )
        return jsonify({"error": "Deployment already exists for this application, version, and environment"}), 409

    current_app.logger.info(
        "Deployment updated",
        extra={
            "event": "deployment_updated",
            "deployment_id": deployment.id,
            "application_name": deployment.application_name,
            "version": deployment.version,
            "environment": deployment.environment,
            "status": deployment.status,
            "updated_fields": sorted(update_data),
        },
    )

    return jsonify(deployment.to_dict())


@deployments_bp.delete("/<int:deployment_id>")
def delete_deployment(deployment_id):
    deployment = get_deployment_or_404(deployment_id)
    log_context = {
        "event": "deployment_deleted",
        "deployment_id": deployment.id,
        "application_name": deployment.application_name,
        "version": deployment.version,
        "environment": deployment.environment,
    }

    db.session.delete(deployment)
    db.session.commit()

    current_app.logger.info("Deployment deleted", extra=log_context)

    return jsonify({"message": "Deployment deleted"}), 200
