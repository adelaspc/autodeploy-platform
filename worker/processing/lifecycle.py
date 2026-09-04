"""Apply pipeline results while keeping deployment, build, and event state aligned."""

from control_plane.deployment_runtime_metadata import persist_helm_runtime_metadata
from control_plane.extensions import db
from control_plane.security import redact_sensitive_data, redact_text
from worker.processing.claims import (
    ensure_claim_owned,
    now_utc,
    refresh_claim,
    release_deployment_claim,
    worker_id,
    write_claim_event,
)
from worker.processing.events import deployment_secret_values, record_auxiliary_events, record_event


BUILD_STATUS_BY_DEPLOYMENT_STATUS = {
    "cloning": "cloning",
    "building": "building",
    "testing": "testing",
    "pushing_image": "pushing_image",
}


def push_error_hint(message, metadata=None):
    haystack = " ".join(
        str(part)
        for part in [
            message or "",
            (metadata or {}).get("summary") or "",
            " ".join((metadata or {}).get("output_tail") or []),
        ]
        if part
    ).lower()
    if "denied: requested access to the resource is denied" in haystack or "denied:" in haystack:
        return {
            "possible_causes": [
                "registry authentication or token scope issue",
                "repository permission or namespace access issue",
                "registry plan or private repository limit",
            ]
        }
    if "unauthorized" in haystack or "insufficient scopes" in haystack or "insufficient_scope" in haystack:
        return {
            "possible_causes": [
                "registry authentication failed",
                "registry token does not have push permission",
            ]
        }
    if "manifest unknown" in haystack or "no such manifest" in haystack:
        return {
            "possible_causes": [
                "registry image tag was not published",
                "registry push may have partially succeeded without a retrievable image manifest",
            ]
        }
    if "repository does not exist" in haystack:
        return {
            "possible_causes": [
                "target repository does not exist",
                "authenticated user does not have permission to create or push to the repository",
            ]
        }
    return {}


def push_event_metadata(result, message, *, status):
    metadata = result.metadata | {
        "step": "image.push",
        "success": status == "succeeded",
        "push_log_available": bool(result.log_path),
        "push_log_path": result.log_path,
        "push_output_tail": result.metadata.get("output_tail"),
        "push_summary": result.metadata.get("summary") or message,
        "push_duration": result.metadata.get("duration_seconds"),
        "push_attempts": result.metadata.get("attempt"),
        "push_total_attempts": result.metadata.get("total_attempts"),
    }
    if status == "failed":
        metadata["error_message"] = message
        metadata |= push_error_hint(message, metadata)
    return metadata


def apply_execution_result(deployment, result):
    if result is None:
        return

    build = deployment.build
    if result.workspace_path:
        build.workspace_path = result.workspace_path
    if result.log_path:
        build.log_path = result.log_path
    if result.image_tag:
        build.image_tag = result.image_tag
    if result.image_ref:
        build.image_ref = result.image_ref
    if result.deploy_target:
        deployment.deploy_target = result.deploy_target
    if result.container_name:
        deployment.container_name = result.container_name
    if result.container_id:
        deployment.container_id = result.container_id
    if result.host_port is not None:
        deployment.host_port = result.host_port
    if result.healthcheck_url:
        deployment.healthcheck_url = result.healthcheck_url
    if result.service_url:
        deployment.service_url = result.service_url
    persist_helm_runtime_metadata(deployment, result.metadata)


def clear_preflight_state(deployment):
    deployment.preflight_status = None
    deployment.preflight_summary = None
    deployment.preflight_metadata_json = None
    deployment.preflight_completed_at = None


def persist_preflight_result(deployment, result):
    if result is None:
        return
    secret_values = deployment_secret_values(deployment)
    deployment.preflight_status = result.status
    deployment.preflight_summary = redact_text(result.summary, secret_values=secret_values)
    deployment.preflight_metadata_json = redact_sensitive_data(
        {
            **(result.metadata or {}),
            "log_path": result.log_path,
            "deploy_target": result.deploy_target,
            "summary": result.summary,
            "status": result.status,
        },
        secret_values=secret_values,
    )
    deployment.preflight_completed_at = now_utc()


def persist_preflight_failure(deployment, error):
    if "preflight" not in (error.step or ""):
        return
    secret_values = deployment_secret_values(deployment)
    deployment.preflight_status = "failed"
    deployment.preflight_summary = redact_text(error.message, secret_values=secret_values)
    deployment.preflight_metadata_json = redact_sensitive_data(
        {
            **(error.metadata or {}),
            "log_path": error.log_path,
            "summary": error.message,
            "status": "failed",
        },
        secret_values=secret_values,
    )
    deployment.preflight_completed_at = now_utc()


def set_deployment_status(deployment, status, *, event_type, message):
    if deployment.started_at is None and status != "pending":
        deployment.started_at = now_utc()
    if deployment.build.started_at is None and status in BUILD_STATUS_BY_DEPLOYMENT_STATUS:
        deployment.build.started_at = now_utc()

    deployment.transition_to(status)
    build_status = BUILD_STATUS_BY_DEPLOYMENT_STATUS.get(status)
    if build_status:
        deployment.build.transition_to(build_status)
    record_event(deployment, event_type, status, message, step=status)
    db.session.flush()


def mark_failed(deployment, step, message, *, metadata=None):
    metadata = {"summary": message, **(metadata or {})}
    if step == "image.push":
        metadata = push_event_metadata(
            type("PushFailureResult", (), {"metadata": metadata, "log_path": deployment.build.log_path})(),
            message,
            status="failed",
        )
    ensure_claim_owned(deployment)
    refresh_claim(deployment)
    # Failures often contain command output. Redact once before copying the same
    # information into models and operator-visible events.
    secret_values = deployment_secret_values(deployment)
    sanitized_message = redact_text(message, secret_values=secret_values)
    sanitized_metadata = redact_sensitive_data(metadata, secret_values=secret_values)
    deployment.transition_to("failed")
    deployment.finished_at = now_utc()
    deployment.last_error = sanitized_message
    deployment.build.transition_to("failed")
    deployment.build.finished_at = now_utc()
    deployment.build.last_error = sanitized_message
    record_event(
        deployment,
        f"{step}.failed",
        "failed",
        sanitized_message,
        step=step,
        level="error",
        metadata={"log_path": deployment.build.log_path, **sanitized_metadata},
    )
    record_event(
        deployment,
        "deployment.failed",
        "failed",
        sanitized_message,
        step="deployment",
        level="error",
        metadata={"log_path": deployment.build.log_path, **sanitized_metadata},
    )
    write_claim_event(
        deployment,
        "claim_cleared",
        "Worker cleared deployment claim after failure",
        metadata={"worker_id": worker_id()},
    )
    release_deployment_claim(deployment)
    db.session.commit()
    return deployment


def begin_step(deployment, status, *, event_type, message):
    ensure_claim_owned(deployment)
    refresh_claim(deployment)
    set_deployment_status(deployment, status, event_type=event_type, message=message)
    db.session.commit()


def commit_step_result(deployment, *, event_type, status, message, step, metadata, extra_events=None):
    ensure_claim_owned(deployment)
    refresh_claim(deployment)
    record_auxiliary_events(deployment, extra_events)
    record_event(deployment, event_type, status, message, step=step, metadata=metadata)
    db.session.commit()
