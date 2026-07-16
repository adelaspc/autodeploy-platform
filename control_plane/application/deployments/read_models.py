from collections import deque
from pathlib import Path

from flask import current_app

from control_plane.deployment_spec import project_for_deployment
from control_plane.security import redact_sensitive_data, redact_text, secret_values_from_env_vars


def deployment_secret_values(deployment):
    return secret_values_from_env_vars(project_for_deployment(deployment).env_vars)


def get_runtime_log_path(deployment):
    for event in sorted(deployment.events, key=lambda item: (item.created_at, item.id or 0), reverse=True):
        metadata = event.metadata_json or {}
        runtime_log_path = metadata.get("runtime_log_path")
        if runtime_log_path:
            return Path(runtime_log_path)
    return None


def get_build_log_path(deployment):
    log_path = deployment.build.build_log_path
    if not log_path:
        for event in sorted(deployment.events, key=lambda item: (item.created_at, item.id or 0), reverse=True):
            if event.event_type != "image.build_succeeded":
                continue
            metadata = event.metadata_json or {}
            log_path = metadata.get("log_path")
            if log_path:
                break
    if not log_path:
        log_path = deployment.build.log_path
    if not log_path:
        return None
    return Path(log_path)


def recent_deployment_events(deployment, *, limit=5):
    events = sorted(deployment.events, key=lambda item: (item.created_at, item.id or 0), reverse=True)
    return events[:limit]


def latest_meaningful_event(deployment):
    ignored_prefixes = ("claim_", "reconcile.")
    for event in sorted(deployment.events, key=lambda item: (item.created_at, item.id or 0), reverse=True):
        if event.event_type.startswith(ignored_prefixes):
            continue
        return event
    return None


def is_safe_log_path(path):
    allowed_roots = [
        Path(current).resolve()
        for current in {
            current_app.config.get("CONTROL_PLANE_WORKSPACE_ROOT", "/tmp/paas-workspaces"),
            current_app.instance_path,
        }
    ]
    resolved = path.resolve()
    return any(root == resolved or root in resolved.parents for root in allowed_roots)


def build_log_available(deployment):
    log_path = get_build_log_path(deployment)
    return bool(log_path and is_safe_log_path(log_path) and log_path.is_file())


def runtime_log_available(deployment):
    log_path = get_runtime_log_path(deployment)
    return bool(log_path and is_safe_log_path(log_path) and log_path.is_file())


def latest_push_event(deployment):
    for event in sorted(deployment.events, key=lambda item: (item.created_at, item.id or 0), reverse=True):
        if event.event_type in {"image.push_succeeded", "image.push.failed"}:
            return event
    return None


def latest_kubernetes_event(deployment, event_types):
    for event in sorted(deployment.events, key=lambda item: (item.created_at, item.id or 0), reverse=True):
        if event.event_type in event_types:
            return event
    return None


def deployment_preflight_record(deployment):
    if not deployment.preflight_status:
        return None
    secret_values = deployment_secret_values(deployment)
    metadata = redact_sensitive_data(deployment.preflight_metadata_json or {}, secret_values=secret_values)
    return {
        "status": deployment.preflight_status,
        "summary": redact_text(deployment.preflight_summary, secret_values=secret_values),
        "metadata": metadata,
        "completed_at": deployment.preflight_completed_at.isoformat() if deployment.preflight_completed_at else None,
    }


def kubernetes_summary_fields(deployment):
    secret_values = deployment_secret_values(deployment)
    if deployment.deploy_target != "kubernetes":
        return {
            "kubernetes_namespace": None,
            "kubernetes_deployment_name": None,
            "kubernetes_service_name": None,
            "kubernetes_ingress_name": None,
            "kubernetes_ingress_host": None,
            "kubernetes_ingress_class": None,
            "internal_service_url": None,
            "last_kubernetes_failure_stage": None,
            "last_kubernetes_failure_summary": None,
            "last_kubernetes_failure_missing_resources": None,
        }

    preflight_record = deployment_preflight_record(deployment)
    apply_event = latest_kubernetes_event(
        deployment,
        {"deployment.apply_succeeded", "kubernetes.healthcheck_succeeded", "kubernetes.healthcheck_failed"},
    )
    failure_event = latest_kubernetes_event(
        deployment,
        {
            "kubernetes.preflight_failed",
            "kubernetes.manifest_apply_failed",
            "kubernetes.rollout_failed",
            "kubernetes.healthcheck_failed",
        },
    )
    source_event = failure_event or apply_event
    metadata = redact_sensitive_data(
        source_event.metadata_json if source_event and source_event.metadata_json else {},
        secret_values=secret_values,
    )
    if preflight_record and preflight_record["metadata"].get("namespace"):
        metadata = preflight_record["metadata"] | metadata

    failure_stage = None
    failure_summary = None
    failure_missing_resources = None
    if preflight_record and preflight_record["status"] == "failed":
        failure_stage = "preflight"
        failure_missing_resources = preflight_record["metadata"].get("missing_resources")
        failure_summary = (
            ", ".join(
                f"{item.get('kind')}/{item.get('name')}"
                for item in failure_missing_resources or []
                if item.get("kind") and item.get("name")
            )
            or preflight_record["summary"]
        )
    elif failure_event is not None:
        if failure_event.event_type == "kubernetes.preflight_failed":
            failure_stage = "preflight"
            failure_missing_resources = metadata.get("missing_resources")
            failure_summary = (
                ", ".join(
                    f"{item.get('kind')}/{item.get('name')}"
                    for item in failure_missing_resources or []
                    if item.get("kind") and item.get("name")
                )
                or failure_event.message
            )
        elif failure_event.event_type == "kubernetes.manifest_apply_failed":
            failure_stage = "manifest_apply"
            failure_summary = (
                metadata.get("apply_pod_logs_summary")
                or metadata.get("apply_pod_describe_summary")
                or metadata.get("apply_services_summary")
                or metadata.get("apply_pods_summary")
                or failure_event.message
            )
        elif failure_event.event_type == "kubernetes.rollout_failed":
            failure_stage = "rollout"
            failure_summary = (
                metadata.get("rollout_pod_logs_summary")
                or metadata.get("rollout_pod_describe_summary")
                or metadata.get("rollout_describe_summary")
                or metadata.get("rollout_pods_summary")
                or failure_event.message
            )
        elif failure_event.event_type == "kubernetes.healthcheck_failed":
            failure_stage = "healthcheck"
            failure_summary = (
                metadata.get("healthcheck_pod_logs_summary")
                or metadata.get("healthcheck_pod_describe_summary")
                or metadata.get("healthcheck_service_summary")
                or metadata.get("healthcheck_deployment_summary")
                or metadata.get("healthcheck_pods_summary")
                or failure_event.message
            )

    return {
        "kubernetes_namespace": deployment.helm_namespace or metadata.get("namespace"),
        "kubernetes_deployment_name": metadata.get("deployment_name"),
        "kubernetes_service_name": metadata.get("service_name"),
        "kubernetes_ingress_name": metadata.get("ingress_name"),
        "kubernetes_ingress_host": metadata.get("ingress_host"),
        "kubernetes_ingress_class": metadata.get("ingress_class"),
        "internal_service_url": metadata.get("internal_service_url"),
        "last_kubernetes_failure_stage": failure_stage,
        "last_kubernetes_failure_summary": failure_summary,
        "last_kubernetes_failure_missing_resources": failure_missing_resources,
    }


def helm_summary_fields(deployment):
    return {
        "helm_release_name": deployment.helm_release_name,
        "helm_namespace": deployment.helm_namespace,
        "helm_chart_path": deployment.helm_chart_path,
    }


def latest_kubernetes_failure_event(deployment):
    if deployment.preflight_status == "failed":
        return None
    return latest_kubernetes_event(
        deployment,
        {
            "kubernetes.preflight_failed",
            "kubernetes.manifest_apply_failed",
            "kubernetes.rollout_failed",
            "kubernetes.healthcheck_failed",
        },
    )


HELM_DIAGNOSTIC_EVENTS = {
    "kubernetes.helm_deploy_failed",
    "kubernetes.helm_uninstall_failed",
    "reconcile.helm_release_missing",
    "reconcile.helm_cleanup_failed",
}


def latest_helm_diagnostic_event(deployment):
    return latest_kubernetes_event(deployment, HELM_DIAGNOSTIC_EVENTS)


def helm_failure_summary(event, metadata):
    if event is None:
        return None
    return (
        metadata.get("helm_stderr_summary")
        or metadata.get("helm_stdout_summary")
        or metadata.get("error_message")
        or metadata.get("summary")
        or event.message
    )


def kubernetes_failure_stage_and_summary(failure_event):
    if failure_event is None:
        return None, None

    secret_values = deployment_secret_values(failure_event.deployment)
    metadata = redact_sensitive_data(failure_event.metadata_json or {}, secret_values=secret_values)
    if failure_event.event_type == "kubernetes.preflight_failed":
        missing_resources = metadata.get("missing_resources") or []
        summary = (
            ", ".join(
                f"{item.get('kind')}/{item.get('name')}"
                for item in missing_resources
                if item.get("kind") and item.get("name")
            )
            or failure_event.message
        )
        return "preflight", summary
    if failure_event.event_type == "kubernetes.manifest_apply_failed":
        summary = (
            metadata.get("apply_pod_logs_summary")
            or metadata.get("apply_pod_describe_summary")
            or metadata.get("apply_services_summary")
            or metadata.get("apply_pods_summary")
            or failure_event.message
        )
        return "manifest_apply", summary
    if failure_event.event_type == "kubernetes.rollout_failed":
        summary = (
            metadata.get("rollout_pod_logs_summary")
            or metadata.get("rollout_pod_describe_summary")
            or metadata.get("rollout_describe_summary")
            or metadata.get("rollout_pods_summary")
            or failure_event.message
        )
        return "rollout", summary
    if failure_event.event_type == "kubernetes.healthcheck_failed":
        summary = (
            metadata.get("healthcheck_pod_logs_summary")
            or metadata.get("healthcheck_pod_describe_summary")
            or metadata.get("healthcheck_service_summary")
            or metadata.get("healthcheck_deployment_summary")
            or metadata.get("healthcheck_pods_summary")
            or failure_event.message
        )
        return "healthcheck", summary
    return None, None


def helm_diagnostic_fields(deployment, metadata):
    return {
        "helm_release_name": metadata.get("helm_release_name") or deployment.helm_release_name,
        "helm_namespace": metadata.get("namespace") or deployment.helm_namespace,
        "helm_chart_path": metadata.get("chart_path") or deployment.helm_chart_path,
        "helm_returncode": metadata.get("helm_returncode"),
        "helm_stdout_summary": metadata.get("helm_stdout_summary"),
        "helm_stderr_summary": metadata.get("helm_stderr_summary"),
        "helm_log_path": metadata.get("helm_log_path"),
        "helm_release_status": metadata.get("release_status"),
    }


def serialize_kubernetes_diagnostics(deployment):
    if deployment.deploy_target != "kubernetes":
        return None
    secret_values = deployment_secret_values(deployment)

    preflight_record = deployment_preflight_record(deployment)
    if preflight_record and preflight_record["status"] == "failed":
        metadata = preflight_record["metadata"]
        missing_resources = metadata.get("missing_resources")
        failure_summary = (
            ", ".join(
                f"{item.get('kind')}/{item.get('name')}"
                for item in missing_resources or []
                if item.get("kind") and item.get("name")
            )
            or preflight_record["summary"]
        )
        return {
            "deployment_id": deployment.id,
            "deploy_target": deployment.deploy_target,
            "namespace": metadata.get("namespace") or deployment.helm_namespace,
            "deployment_name": metadata.get("deployment_name"),
            "service_name": metadata.get("service_name"),
            "failure_stage": "preflight",
            "failure_summary": failure_summary,
            "failure_event_type": "deployment.preflight_failed",
            "failure_event_at": preflight_record["completed_at"],
            "missing_resources": missing_resources,
            "checked_resources": metadata.get("checked_resources"),
            "configmap_refs_used": metadata.get("configmap_refs_used"),
            "secret_refs_used": metadata.get("secret_refs_used"),
            "image_pull_secret": metadata.get("image_pull_secret"),
            "pod_names": None,
            "pod_describe_summary": None,
            "pod_logs_summary": None,
            "diagnostics": metadata,
        } | helm_diagnostic_fields(deployment, metadata)

    failure_event = latest_kubernetes_failure_event(deployment)
    failure_stage, failure_summary = kubernetes_failure_stage_and_summary(failure_event)
    helm_event = latest_helm_diagnostic_event(deployment)
    if helm_event is not None and (
        failure_event is None
        or (
            helm_event.created_at,
            helm_event.id or 0,
        )
        >= (
            failure_event.created_at,
            failure_event.id or 0,
        )
    ):
        failure_event = helm_event
        metadata = redact_sensitive_data(helm_event.metadata_json or {}, secret_values=secret_values)
        failure_stage = "helm"
        failure_summary = helm_failure_summary(helm_event, metadata)
    else:
        source_event = failure_event or latest_kubernetes_event(
            deployment, {"kubernetes.healthcheck_succeeded"}
        )
        metadata = redact_sensitive_data(
            source_event.metadata_json if source_event and source_event.metadata_json else {},
            secret_values=secret_values,
        )

    pod_stage = failure_stage if failure_stage in {"manifest_apply", "rollout", "healthcheck"} else "healthcheck"

    return {
        "deployment_id": deployment.id,
        "deploy_target": deployment.deploy_target,
        "namespace": metadata.get("namespace") or deployment.helm_namespace,
        "deployment_name": metadata.get("deployment_name"),
        "service_name": metadata.get("service_name"),
        "ingress_name": metadata.get("ingress_name"),
        "ingress_host": metadata.get("ingress_host"),
        "ingress_class": metadata.get("ingress_class"),
        "internal_service_url": metadata.get("internal_service_url"),
        "failure_stage": failure_stage,
        "failure_summary": failure_summary,
        "failure_event_type": failure_event.event_type if failure_event else None,
        "failure_event_at": failure_event.created_at.isoformat() if failure_event and failure_event.created_at else None,
        "missing_resources": metadata.get("missing_resources"),
        "checked_resources": metadata.get("checked_resources"),
        "configmap_refs_used": metadata.get("configmap_refs_used"),
        "secret_refs_used": metadata.get("secret_refs_used"),
        "image_pull_secret": metadata.get("image_pull_secret"),
        "pod_names": metadata.get(f"{pod_stage}_pod_names"),
        "pod_phase": metadata.get(f"{pod_stage}_pod_phase"),
        "container_reason": metadata.get(f"{pod_stage}_container_reason"),
        "restart_count": metadata.get(f"{pod_stage}_restart_count"),
        "images": metadata.get(f"{pod_stage}_images"),
        "image_pull_secrets": metadata.get(f"{pod_stage}_image_pull_secrets"),
        "pod_runtime": metadata.get(f"{pod_stage}_pod_runtime"),
        "pod_describe_summary": (
            metadata.get(f"{failure_stage}_pod_describe_summary")
            if failure_stage in {"manifest_apply", "rollout", "healthcheck"}
            else None
        ),
        "pod_logs_summary": (
            metadata.get(f"{failure_stage}_pod_logs_summary")
            if failure_stage in {"manifest_apply", "rollout", "healthcheck"}
            else None
        ),
        "pod_previous_logs_summary": (
            metadata.get(f"{failure_stage}_pod_previous_logs_summary")
            if failure_stage in {"manifest_apply", "rollout", "healthcheck"}
            else None
        ),
        "diagnostics": metadata,
    } | helm_diagnostic_fields(deployment, metadata)


def serialize_deployment_summary(deployment, *, branch):
    build = deployment.build
    secret_values = deployment_secret_values(deployment)
    meaningful_event = latest_meaningful_event(deployment)
    push_event = latest_push_event(deployment)
    current_step = meaningful_event.step if meaningful_event and meaningful_event.step else deployment.status
    last_meaningful_event = None
    if meaningful_event is not None:
        last_meaningful_event = {
            "event_type": meaningful_event.event_type,
            "step": meaningful_event.step,
            "status": meaningful_event.status,
            "message": redact_text(meaningful_event.message, secret_values=secret_values) if meaningful_event.message else None,
            "created_at": meaningful_event.created_at.isoformat(),
        }

    push_metadata = redact_sensitive_data(push_event.metadata_json if push_event else {}, secret_values=secret_values)

    return {
        "deployment_id": deployment.id,
        "deployment_status": deployment.status,
        "build_id": build.id,
        "build_status": build.status,
        "current_step": current_step,
        "last_meaningful_event": last_meaningful_event,
        "last_error": redact_text(deployment.last_error or build.last_error, secret_values=secret_values)
        if (deployment.last_error or build.last_error)
        else None,
        "branch": branch,
        "commit_sha": build.commit_sha,
        "image_tag": build.image_tag,
        "image_ref": build.image_ref,
        "registry_push_status": build.registry_push_status,
        "push_log_available": bool(push_metadata.get("push_log_available")),
        "last_push_error_summary": push_metadata.get("error_message") or push_metadata.get("push_summary"),
        "service_url": deployment.service_url,
        "deploy_target": deployment.deploy_target,
        "build_log_available": build_log_available(deployment),
        "runtime_log_available": runtime_log_available(deployment),
        "started_at": deployment.started_at.isoformat() if deployment.started_at else None,
        "finished_at": deployment.finished_at.isoformat() if deployment.finished_at else None,
        "created_at": deployment.created_at.isoformat() if deployment.created_at else None,
        "updated_at": deployment.updated_at.isoformat() if deployment.updated_at else None,
        "events": [event.to_dict() for event in reversed(recent_deployment_events(deployment))],
    } | kubernetes_summary_fields(deployment) | helm_summary_fields(deployment)


def read_log_tail(path, *, tail_lines, secret_values=()):
    lines = deque(maxlen=tail_lines)
    total_lines = 0
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            total_lines += 1
            lines.append(redact_text(line.rstrip("\n"), secret_values=secret_values))
    return {
        "content": "\n".join(lines),
        "line_count": total_lines,
        "truncated": total_lines > tail_lines,
    }


def serialize_runtime_log_payload(deployment, log_path, *, tail_lines):
    secret_values = deployment_secret_values(deployment)
    payload = read_log_tail(log_path, tail_lines=tail_lines, secret_values=secret_values)
    return {
        "deployment_id": deployment.id,
        "path": str(log_path),
        "tail_lines": tail_lines,
        **payload,
    }


def serialize_build_log_payload(deployment, log_path, *, tail_lines):
    secret_values = deployment_secret_values(deployment)
    payload = read_log_tail(log_path, tail_lines=tail_lines, secret_values=secret_values)
    return {
        "deployment_id": deployment.id,
        "build_id": deployment.build.id,
        "path": str(log_path),
        "tail_lines": tail_lines,
        **payload,
    }
