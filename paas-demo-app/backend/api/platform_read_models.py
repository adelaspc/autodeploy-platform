from sqlalchemy.orm import selectinload

from backend.api.auth import api_auth_enabled, configured_api_roles
from backend.api.project_validation import (
    kubernetes_deployment_prereq_error,
    kubernetes_deployment_prereq_missing_settings,
)
from backend.models import PlatformDeployment, Project, WebhookDelivery
from backend.security import redact_text, secret_values_from_env_vars
from worker.executor import executor_contract_for_name


def local_repo_paths_allowed():
    return (control_plane_env_name() or "").strip().lower() == "development"


def control_plane_env_name():
    from flask import current_app

    return current_app.config.get("CONTROL_PLANE_ENV", "")


def current_executor_name():
    from flask import current_app

    return (current_app.config.get("CONTROL_PLANE_EXECUTOR", "fake") or "").strip().lower()


def registry_missing_settings():
    from flask import current_app

    missing = []
    if not (current_app.config.get("CONTROL_PLANE_REGISTRY_URL") or "").strip():
        missing.append("CONTROL_PLANE_REGISTRY_URL")
    if not (current_app.config.get("CONTROL_PLANE_REGISTRY_NAMESPACE") or "").strip():
        missing.append("CONTROL_PLANE_REGISTRY_NAMESPACE")
    return missing


def platform_status_payload():
    from flask import current_app

    executor = current_executor_name()
    executor_contract = executor_contract_for_name(executor)
    kubernetes_selected = executor == "kubernetes"
    kubernetes_missing = kubernetes_deployment_prereq_missing_settings()
    deployment_prereq_error = kubernetes_deployment_prereq_error()
    registry_enabled = bool(current_app.config.get("CONTROL_PLANE_REGISTRY_ENABLED", False))
    registry_missing = registry_missing_settings()
    configured_roles = configured_api_roles()

    deployment_creation_ready = deployment_prereq_error is None
    status = "ok" if deployment_creation_ready else "degraded"

    return {
        "status": status,
        "environment": (current_app.config.get("CONTROL_PLANE_ENV", "") or "").strip().lower() or None,
        "executor": executor,
        "executor_contract": executor_contract.as_dict(),
        "local_repo_paths_allowed": local_repo_paths_allowed(),
        "deployment_creation_ready": deployment_creation_ready,
        "deployment_creation_error": deployment_prereq_error,
        "api_auth": {
            "enabled": api_auth_enabled(),
            "configured_roles": configured_roles,
            "token_transport": "bearer",
            "public_routes": ["/health"],
            "protected_health_routes": [
                "/health/db",
                "/health/platform",
                "/health/activity",
            ],
            "webhook_auth_mode": "github_signature",
        },
        "registry": {
            "enabled": registry_enabled,
            "required_for_current_executor": executor_contract.requires_registry_push,
            "configured": not registry_missing,
            "missing_settings": registry_missing,
        },
        "kubernetes": {
            "selected": kubernetes_selected,
            "namespace": (current_app.config.get("CONTROL_PLANE_K8S_NAMESPACE", "default") or "").strip() or None,
            "kubeconfig_configured": bool((current_app.config.get("CONTROL_PLANE_KUBECONFIG") or "").strip()),
            "image_pull_secret_configured": bool(
                (current_app.config.get("CONTROL_PLANE_K8S_IMAGE_PULL_SECRET") or "").strip()
            ),
            "deployment_prereqs_ready": not kubernetes_missing,
            "missing_deployment_prereqs": kubernetes_missing,
        },
    }


def serialize_platform_activity_deployment(deployment):
    build = deployment.build
    project = deployment.project
    secret_values = secret_values_from_env_vars(project.env_vars if project else [])
    return {
        "deployment_id": deployment.id,
        "project_id": project.id,
        "project_name": project.name,
        "status": deployment.status,
        "build_status": build.status,
        "deploy_target": deployment.deploy_target,
        "service_url": deployment.service_url,
        "last_error": redact_text(deployment.last_error or build.last_error, secret_values=secret_values)
        if (deployment.last_error or build.last_error)
        else None,
        "commit_sha": build.commit_sha,
        "image_ref": build.image_ref,
        "created_at": deployment.created_at.isoformat() if deployment.created_at else None,
        "updated_at": deployment.updated_at.isoformat() if deployment.updated_at else None,
        "started_at": deployment.started_at.isoformat() if deployment.started_at else None,
        "finished_at": deployment.finished_at.isoformat() if deployment.finished_at else None,
    }


def serialize_platform_activity_webhook_delivery(delivery):
    return {
        "delivery_id": delivery.delivery_id,
        "event_type": delivery.event_type,
        "repository_url": delivery.repository_url,
        "branch": delivery.branch,
        "commit_sha": delivery.commit_sha,
        "status": delivery.status,
        "reason": delivery.reason,
        "deployment_id": delivery.deployment_id,
        "received_at": delivery.received_at.isoformat() if delivery.received_at else None,
    }


def normalize_activity_repository_url(value):
    if not value:
        return None

    candidate = value.strip()
    if not candidate:
        return None

    if candidate.startswith("https://github.com/"):
        normalized = candidate.rstrip("/")
        if normalized.endswith(".git"):
            normalized = normalized[:-4]
        return normalized.lower()

    return candidate.rstrip("/")


def serialize_project_identity(project):
    return {
        "id": project.id,
        "name": project.name,
        "branch": project.branch,
        "trigger": project.trigger,
        "repo_url": project.repo_url,
    }


def build_deployment_activity_query(*, project_id=None, statuses=None, before_deployment_id=None):
    query = PlatformDeployment.query.options(
        selectinload(PlatformDeployment.project),
        selectinload(PlatformDeployment.build),
    )
    if project_id is not None:
        query = query.filter(PlatformDeployment.project_id == project_id)
    if statuses:
        query = query.filter(PlatformDeployment.status.in_(tuple(statuses)))
    if before_deployment_id is not None:
        query = query.filter(PlatformDeployment.id < before_deployment_id)
    return query


def build_webhook_activity_items(
    *,
    statuses,
    normalized_project_repo_url=None,
    before_webhook_delivery_id=None,
):
    items = (
        WebhookDelivery.query.filter(WebhookDelivery.status.in_(tuple(statuses)))
        .order_by(WebhookDelivery.received_at.desc(), WebhookDelivery.id.desc())
        .all()
    )
    if before_webhook_delivery_id is not None:
        items = [item for item in items if item.id < before_webhook_delivery_id]
    if normalized_project_repo_url is not None:
        items = [
            item
            for item in items
            if normalize_activity_repository_url(item.repository_url) == normalized_project_repo_url
        ]
    return items


def platform_activity_payload(
    *,
    latest_limit=10,
    active_limit=10,
    failed_limit=10,
    ignored_webhook_limit=10,
    accepted_webhook_limit=10,
    deployment_statuses=None,
    webhook_statuses=None,
    project_id=None,
    before_deployment_id=None,
    before_webhook_delivery_id=None,
):
    deployment_statuses = set(deployment_statuses or ())
    webhook_statuses = set(webhook_statuses or ())
    all_statuses = set(PlatformDeployment.VALID_STATUSES)
    normalized_project_repo_url = None
    project = None
    if project_id is not None:
        project = Project.query.filter_by(id=project_id).first()
        if project is not None:
            normalized_project_repo_url = normalize_activity_repository_url(project.repo_url)

    latest_query = build_deployment_activity_query(
        project_id=project_id,
        statuses=deployment_statuses or None,
        before_deployment_id=before_deployment_id,
    )
    latest_deployments = latest_query.order_by(PlatformDeployment.created_at.desc(), PlatformDeployment.id.desc()).limit(
        latest_limit
    ).all()

    active_statuses = {"pending", "running"} & (deployment_statuses or all_statuses)
    active_query = build_deployment_activity_query(
        project_id=project_id,
        statuses=active_statuses or {"__none__"},
        before_deployment_id=before_deployment_id,
    )
    active_deployments = active_query.order_by(PlatformDeployment.created_at.desc(), PlatformDeployment.id.desc()).limit(
        active_limit
    ).all()

    failed_statuses = {"failed"} & (deployment_statuses or all_statuses)
    failed_query = build_deployment_activity_query(
        project_id=project_id,
        statuses=failed_statuses or {"__none__"},
        before_deployment_id=before_deployment_id,
    )
    failed_deployments = failed_query.order_by(PlatformDeployment.updated_at.desc(), PlatformDeployment.id.desc()).limit(
        failed_limit
    ).all()
    ignored_webhook_deliveries = []
    if not webhook_statuses or "ignored" in webhook_statuses:
        ignored_webhook_deliveries = build_webhook_activity_items(
            statuses=("ignored",),
            normalized_project_repo_url=normalized_project_repo_url if project_id is not None else None,
            before_webhook_delivery_id=before_webhook_delivery_id,
        )
        ignored_webhook_deliveries = ignored_webhook_deliveries[:ignored_webhook_limit]
    accepted_webhook_deliveries = []
    if not webhook_statuses or "accepted" in webhook_statuses:
        accepted_webhook_deliveries = build_webhook_activity_items(
            statuses=("accepted",),
            normalized_project_repo_url=normalized_project_repo_url if project_id is not None else None,
            before_webhook_delivery_id=before_webhook_delivery_id,
        )
        accepted_webhook_deliveries = accepted_webhook_deliveries[:accepted_webhook_limit]
    return {
        "status": "ok",
        "project": serialize_project_identity(project) if project is not None else None,
        "latest_deployment_at": latest_deployments[0]["created_at"] if False else (
            latest_deployments[0].created_at.isoformat() if latest_deployments else None
        ),
        "active_deployment_count": len(active_deployments),
        "failed_deployment_count": len(failed_deployments),
        "recent_webhook_delivery_count": len(ignored_webhook_deliveries) + len(accepted_webhook_deliveries),
        "next_before_deployment_id": latest_deployments[-1].id if latest_deployments else None,
        "next_before_webhook_delivery_id": (
            min(
                [item.id for item in ignored_webhook_deliveries + accepted_webhook_deliveries],
                default=None,
            )
        ),
        "pagination": {
            "latest_limit": latest_limit,
            "active_limit": active_limit,
            "failed_limit": failed_limit,
            "ignored_webhook_limit": ignored_webhook_limit,
            "accepted_webhook_limit": accepted_webhook_limit,
            "next_before_deployment_id": latest_deployments[-1].id if latest_deployments else None,
            "next_before_webhook_delivery_id": min(
                [item.id for item in ignored_webhook_deliveries + accepted_webhook_deliveries],
                default=None,
            ),
        },
        "latest_deployments": [serialize_platform_activity_deployment(item) for item in latest_deployments],
        "active_deployments": [serialize_platform_activity_deployment(item) for item in active_deployments],
        "failed_deployments": [serialize_platform_activity_deployment(item) for item in failed_deployments],
        "ignored_webhook_deliveries": [
            serialize_platform_activity_webhook_delivery(item) for item in ignored_webhook_deliveries
        ],
        "accepted_webhook_deliveries": [
            serialize_platform_activity_webhook_delivery(item) for item in accepted_webhook_deliveries
        ],
    }
def project_activity_payload(
    project,
    *,
    latest_limit=10,
    webhook_limit=10,
    deployment_statuses=None,
    webhook_statuses=None,
    active_only=False,
    include_latest_failed=True,
    include_webhooks=True,
    before_deployment_id=None,
    before_webhook_delivery_id=None,
):
    deployment_statuses = set(deployment_statuses or ())
    webhook_statuses = set(webhook_statuses or ())
    all_statuses = set(PlatformDeployment.VALID_STATUSES)

    latest_query = build_deployment_activity_query(
        project_id=project.id,
        before_deployment_id=before_deployment_id,
    )
    if active_only:
        latest_query = latest_query.filter(PlatformDeployment.status.in_(("pending", "running")))
    if deployment_statuses:
        latest_query = latest_query.filter(PlatformDeployment.status.in_(tuple(deployment_statuses)))
    latest_deployments = latest_query.order_by(PlatformDeployment.created_at.desc(), PlatformDeployment.id.desc()).limit(
        latest_limit
    ).all()

    active_statuses = {"pending", "running"} & (deployment_statuses or all_statuses)
    active_query = build_deployment_activity_query(
        project_id=project.id,
        statuses=active_statuses or {"__none__"},
        before_deployment_id=before_deployment_id,
    )
    active_deployment = active_query.order_by(PlatformDeployment.created_at.desc(), PlatformDeployment.id.desc()).first()

    failed_statuses = {"failed"} & (deployment_statuses or all_statuses)
    failed_query = build_deployment_activity_query(
        project_id=project.id,
        statuses=failed_statuses or {"__none__"},
        before_deployment_id=before_deployment_id,
    )
    latest_failed_deployment = failed_query.order_by(PlatformDeployment.updated_at.desc(), PlatformDeployment.id.desc()).first()

    relevant_webhook_deliveries = []
    normalized_repo_url = normalize_activity_repository_url(project.repo_url)
    if include_webhooks and project.trigger == "github_push" and normalized_repo_url:
        statuses = tuple(webhook_statuses) if webhook_statuses else ("accepted", "ignored")
        relevant_webhook_deliveries = build_webhook_activity_items(
            statuses=statuses,
            normalized_project_repo_url=normalized_repo_url,
            before_webhook_delivery_id=before_webhook_delivery_id,
        )
        relevant_webhook_deliveries = [
            delivery
            for delivery in relevant_webhook_deliveries
            if normalize_activity_repository_url(delivery.repository_url) == normalized_repo_url
        ][:webhook_limit]

    return {
        "status": "ok",
        "project": serialize_project_identity(project),
        "latest_deployment_at": latest_deployments[0].created_at.isoformat() if latest_deployments else None,
        "active_deployment_count": 1 if active_deployment is not None else 0,
        "failed_deployment_count": 1 if latest_failed_deployment is not None else 0,
        "recent_webhook_delivery_count": len(relevant_webhook_deliveries),
        "next_before_deployment_id": latest_deployments[-1].id if latest_deployments else None,
        "next_before_webhook_delivery_id": relevant_webhook_deliveries[-1].id if relevant_webhook_deliveries else None,
        "pagination": {
            "latest_limit": latest_limit,
            "webhook_limit": webhook_limit,
            "next_before_deployment_id": latest_deployments[-1].id if latest_deployments else None,
            "next_before_webhook_delivery_id": relevant_webhook_deliveries[-1].id if relevant_webhook_deliveries else None,
        },
        "latest_deployments": [serialize_platform_activity_deployment(item) for item in latest_deployments],
        "active_deployment": (
            serialize_platform_activity_deployment(active_deployment) if active_deployment is not None else None
        ),
        "latest_failed_deployment": (
            serialize_platform_activity_deployment(latest_failed_deployment)
            if include_latest_failed and latest_failed_deployment is not None
            else None
        ),
        "recent_webhook_deliveries": [
            serialize_platform_activity_webhook_delivery(item) for item in relevant_webhook_deliveries
        ],
    }


def project_status_payload(project):
    latest_deployment = (
        PlatformDeployment.query.options(
            selectinload(PlatformDeployment.project),
            selectinload(PlatformDeployment.build),
        )
        .filter_by(project_id=project.id)
        .order_by(PlatformDeployment.created_at.desc(), PlatformDeployment.id.desc())
        .first()
    )
    active_deployment = (
        PlatformDeployment.query.options(
            selectinload(PlatformDeployment.project),
            selectinload(PlatformDeployment.build),
        )
        .filter(
            PlatformDeployment.project_id == project.id,
            PlatformDeployment.status.in_(("pending", "running")),
        )
        .order_by(PlatformDeployment.created_at.desc(), PlatformDeployment.id.desc())
        .first()
    )
    latest_failed_deployment = (
        PlatformDeployment.query.options(
            selectinload(PlatformDeployment.project),
            selectinload(PlatformDeployment.build),
        )
        .filter(
            PlatformDeployment.project_id == project.id,
            PlatformDeployment.status == "failed",
        )
        .order_by(PlatformDeployment.updated_at.desc(), PlatformDeployment.id.desc())
        .first()
    )
    deployment_creation_error = kubernetes_deployment_prereq_error()
    return {
        "status": "ok",
        "project": serialize_project_identity(project),
        "deployment_creation_ready": deployment_creation_error is None,
        "deployment_creation_error": deployment_creation_error,
        "latest_deployment_at": latest_deployment.created_at.isoformat() if latest_deployment else None,
        "active_deployment_count": 1 if active_deployment is not None else 0,
        "failed_deployment_count": 1 if latest_failed_deployment is not None else 0,
        "latest_deployment": (
            serialize_platform_activity_deployment(latest_deployment) if latest_deployment is not None else None
        ),
        "active_deployment": (
            serialize_platform_activity_deployment(active_deployment) if active_deployment is not None else None
        ),
        "latest_failed_deployment": (
            serialize_platform_activity_deployment(latest_failed_deployment)
            if latest_failed_deployment is not None
            else None
        ),
    }
