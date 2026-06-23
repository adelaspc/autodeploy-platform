from backend.api.deployment_orchestration import (
    create_build_and_deployment_records,
    create_deployment_event,
    now_utc,
)
from backend.deployment_runtime_metadata import persist_helm_runtime_metadata
from backend.extensions import db
from worker.executor import create_executor_for_deployment


def stop_deployment_runtime(deployment, *, message=None):
    executor = create_executor_for_deployment(deployment)
    create_deployment_event(
        deployment.id,
        "deployment.stop_started",
        "stopped",
        message or "Stopping deployment runtime",
        step="deploy.stop",
    )
    stop_result = executor.stop(deployment)
    deployment.status = "stopped"
    deployment.service_url = None
    deployment.finished_at = now_utc()
    if stop_result.deploy_target:
        deployment.deploy_target = stop_result.deploy_target
    if stop_result.container_name:
        deployment.container_name = stop_result.container_name
    if stop_result.container_id:
        deployment.container_id = stop_result.container_id
    if stop_result.host_port is not None:
        deployment.host_port = stop_result.host_port
    if stop_result.healthcheck_url:
        deployment.healthcheck_url = stop_result.healthcheck_url
    persist_helm_runtime_metadata(deployment, stop_result.metadata)
    for event in stop_result.events or ():
        create_deployment_event(
            deployment.id,
            event["event_type"],
            event["status"],
            event.get("message"),
            step=event.get("step"),
            level=event.get("level", "info"),
            metadata_json=event.get("metadata_json"),
        )
    create_deployment_event(
        deployment.id,
        "deployment.stopped",
        "stopped",
        stop_result.message,
        step="deploy.stop",
        metadata_json=stop_result.metadata
        | {
            "log_path": stop_result.log_path,
            "summary": stop_result.message,
            "container_name": deployment.container_name,
            "container_id": deployment.container_id,
        },
    )


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

    if update_data.get("status") == "stopped":
        stop_deployment_runtime(deployment, message=message)
    elif "status" in update_data:
        deployment.status = update_data["status"]
        create_deployment_event(
            deployment.id,
            "deployment.status_updated",
            deployment.status,
            message or f"Deployment status updated to '{deployment.status}'",
            step="deployment",
        )

    if "service_url" in update_data:
        deployment.service_url = update_data["service_url"]
        create_deployment_event(
            deployment.id,
            "deployment.service_url_updated",
            deployment.status,
            message or f"Service URL updated to '{deployment.service_url}'",
            step="deploy",
        )

    if "build_status" in update_data:
        deployment.build.status = update_data["build_status"]
        create_deployment_event(
            deployment.id,
            "build.status_updated",
            deployment.build.status,
            message or f"Build status updated to '{deployment.build.status}'",
            step="build",
        )
