"""Serialize deployment aggregates and apply operator-requested state updates."""

from control_plane.application.deployments.orchestration import (
    create_build_and_deployment_records,
    create_deployment_event,
)
from control_plane.extensions import db


def create_manual_deployment(project, payload, *, test_command):
    registry = payload.get("registry")
    image_name = payload.get("image_name")
    image_tag = payload.get("image_tag")
    image_ref = payload.get("image_ref")
    if not image_ref and registry and image_name and image_tag:
        image_ref = f"{registry.rstrip('/')}/{image_name}:{image_tag}"

    build, deployment = create_build_and_deployment_records(
        project,
        commit_sha=payload["commit_sha"],
        registry=registry,
        image_name=image_name,
        image_tag=image_tag,
        image_ref=image_ref,
        build_status=payload.get("build_status", "pending"),
        test_command=test_command,
        environment=payload.get("environment", "production"),
        deployment_status=payload.get("status", "pending"),
        service_url=payload.get("service_url"),
        deployment_message=payload.get("message", "Deployment record created"),
        deployment_metadata={"branch": project.branch, "commit_sha": payload["commit_sha"]},
        deployment_branch=project.branch,
    )

    if build.status != "pending":
        create_deployment_event(
            deployment.id,
            "build.status_reported",
            build.status,
            f"Build recorded in status '{build.status}'",
            step="build",
        )

    db.session.commit()
    return build, deployment


def apply_deployment_update(deployment, update_data):
    message = update_data.get("message")

    if "status" in update_data:
        deployment.transition_to(update_data["status"])
        create_deployment_event(
            deployment.id,
            "deployment.status_updated",
            deployment.status,
            message or f"Deployment status updated to '{deployment.status}'",
            step="deployment",
        )

    if "build_status" in update_data:
        deployment.build.transition_to(update_data["build_status"])
        create_deployment_event(
            deployment.id,
            "build.status_updated",
            deployment.build.status,
            message or f"Build status updated to '{deployment.build.status}'",
            step="build",
        )
