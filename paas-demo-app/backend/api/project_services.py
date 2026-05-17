from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from backend.extensions import db
from backend.models import PlatformDeployment, Project


class ProjectConflictError(Exception):
    pass


def serialize_preflight_fields(deployment):
    return {
        "preflight_status": deployment.preflight_status,
        "preflight_summary": deployment.preflight_summary,
        "preflight_completed_at": (
            deployment.preflight_completed_at.isoformat() if deployment.preflight_completed_at else None
        ),
    }


def build_project_from_payload(payload):
    return Project(
        name=payload["name"],
        repo_url=payload["repo_url"],
        branch=payload["branch"],
        git_auth_type=payload.get("git_auth_type", "none"),
        git_secret_ref=payload.get("git_secret_ref"),
        dockerfile_path=payload.get("dockerfile_path", "Dockerfile"),
        build_context=payload.get("build_context", "."),
        port=payload["port"],
        healthcheck_path=payload["healthcheck_path"],
        env_vars=payload.get("env_vars", []),
        default_test_command=payload.get("default_test_command"),
        migration_command=payload.get("migration_command"),
        cpu=payload.get("cpu"),
        memory=payload.get("memory"),
        trigger=payload.get("trigger", "manual"),
        runtime=payload.get("runtime", "dockerfile"),
    )


def create_project_record(payload):
    project = build_project_from_payload(payload)
    try:
        db.session.add(project)
        db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        raise ProjectConflictError("Project name must be unique") from exc
    return project


def update_project_record(project, update_data):
    for key, value in update_data.items():
        setattr(project, key, value)

    try:
        db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        raise ProjectConflictError("Project name must be unique") from exc
    return project


def serialize_project_deployment(deployment, *, include_events=False):
    payload = {
        **deployment.to_dict(),
        "build": deployment.build.to_dict(),
    }
    if include_events:
        payload["events"] = [event.to_dict() for event in deployment.events]
    return payload


def serialize_latest_project_deployment(deployment, *, branch):
    build = deployment.build
    return {
        "deployment_id": deployment.id,
        "status": deployment.status,
        "build_id": build.id,
        "build_status": build.status,
        "branch": branch,
        "commit_sha": build.commit_sha,
        "image_tag": build.image_tag,
        "image_ref": build.image_ref,
        "service_url": deployment.service_url,
        "created_at": deployment.created_at.isoformat() if deployment.created_at else None,
        "updated_at": deployment.updated_at.isoformat() if deployment.updated_at else None,
    } | serialize_preflight_fields(deployment)


def serialize_triggered_deployment(
    deployment,
    *,
    branch,
    source_deployment_field=None,
    source_deployment_id=None,
):
    payload = {
        "deployment_id": deployment.id,
        "project_id": deployment.project_id,
        "status": deployment.status,
        "branch": branch,
        "commit_sha": deployment.build.commit_sha,
        "image_tag": deployment.build.image_tag,
        "image_ref": deployment.build.image_ref,
    } | serialize_preflight_fields(deployment)
    if source_deployment_field and source_deployment_id is not None:
        payload[source_deployment_field] = source_deployment_id
    return payload


def list_project_deployments_query(project_id):
    return (
        PlatformDeployment.query.options(
            selectinload(PlatformDeployment.project),
            selectinload(PlatformDeployment.build),
            selectinload(PlatformDeployment.events),
        )
        .populate_existing()
        .filter_by(project_id=project_id)
        .order_by(PlatformDeployment.created_at.desc())
    )


def serialize_project_deployments_page(deployments, *, limit):
    return {
        "items": [serialize_project_deployment(deployment) for deployment in deployments],
        "pagination": {
            "limit": limit,
            "count": len(deployments),
            "next_before_deployment_id": deployments[-1].id if deployments else None,
        },
    }
