"""Protect Helm releases whose identity is shared by deployment attempts."""

from sqlalchemy import select

from control_plane.deployment_runtime_metadata import kubernetes_deployment_mode
from control_plane.extensions import db
from control_plane.models import PlatformDeployment


def deployment_has_helm_release_metadata(deployment):
    """Return whether a deployment is associated with a Helm release."""
    if kubernetes_deployment_mode(deployment) == "helm":
        return True
    if getattr(deployment, "helm_release_name", None):
        return True
    for event in getattr(deployment, "events", []) or []:
        metadata = event.metadata_json or {}
        if isinstance(metadata, dict) and metadata.get("helm_release_name"):
            return True
    return False


def newer_helm_workload_owner_id(deployment):
    """Return the newest owner of the deployment's shared Helm workload."""
    deployment_id = deployment.id
    project_id = deployment.project_id
    environment = deployment.environment
    # Finish the current transaction so MySQL opens a fresh read view before
    # checking whether another deployment now owns this stable release.
    db.session.commit()
    return db.session.scalar(
        select(PlatformDeployment.id)
        .where(
            PlatformDeployment.id > deployment_id,
            PlatformDeployment.project_id == project_id,
            PlatformDeployment.environment == environment,
        )
        .order_by(PlatformDeployment.id.asc())
        .limit(1)
    )
