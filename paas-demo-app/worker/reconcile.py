from __future__ import annotations

from datetime import timedelta

from flask import current_app

from backend.extensions import db
from backend.models import DeploymentEvent, PlatformDeployment
from worker.executor import WorkerExecutionError, create_executor_for_deployment
from worker.service import now_utc, release_deployment_claim


IN_PROGRESS_STATUSES = {"cloning", "building", "testing", "pushing_image", "deploying"}


def record_reconcile_event(deployment, event_type, message, *, level="info", metadata=None, step="reconcile"):
    db.session.add(
        DeploymentEvent(
            deployment_id=deployment.id,
            event_type=event_type,
            step=step,
            level=level,
            status=deployment.status,
            message=message,
            metadata_json=metadata,
        )
    )


def stale_claim_cutoff():
    return now_utc() - timedelta(seconds=current_app.config.get("CONTROL_PLANE_CLAIM_TTL_SECONDS", 300))


def normalize_timestamp(value):
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=now_utc().tzinfo)
    return value


def iter_reconcilable_deployments():
    return PlatformDeployment.query.order_by(PlatformDeployment.created_at.asc()).all()


def reconcile_stale_claim(deployment):
    claimed_at = normalize_timestamp(deployment.claimed_at)
    if claimed_at is None or claimed_at >= stale_claim_cutoff():
        return False

    previous_claimed_at = claimed_at.isoformat()
    previous_claimed_by = deployment.claimed_by
    metadata = {
        "previous_claimed_at": previous_claimed_at,
        "previous_claimed_by": previous_claimed_by,
    }

    if deployment.status == "pending":
        release_deployment_claim(deployment)
        record_reconcile_event(
            deployment,
            "reconcile.claim_cleared",
            "Cleared stale deployment claim",
            metadata=metadata,
        )
    elif deployment.status in IN_PROGRESS_STATUSES:
        release_deployment_claim(deployment)
        deployment.status = "failed"
        deployment.finished_at = now_utc()
        deployment.last_error = "Reconciler marked deployment failed after stale claim"
        deployment.build.status = "failed"
        deployment.build.finished_at = now_utc()
        deployment.build.last_error = deployment.last_error
        record_reconcile_event(
            deployment,
            "reconcile.claim_recovered",
            "Marked deployment failed after stale in-progress claim",
            level="warning",
            metadata=metadata | {"new_status": "failed"},
        )
    else:
        release_deployment_claim(deployment)
        record_reconcile_event(
            deployment,
            "reconcile.claim_cleared",
            "Cleared stale non-active deployment claim",
            metadata=metadata,
        )
    db.session.commit()
    current_app.logger.info(
        "reconcile_stale_claim",
        extra={
            "deployment_id": deployment.id,
            "status": deployment.status,
            "previous_claimed_by": previous_claimed_by,
            "previous_claimed_at": previous_claimed_at,
        },
    )
    return True


def reconcile_running_missing_container(deployment):
    if deployment.status != "running" or deployment.deploy_target != "local-docker":
        return False

    executor = create_executor_for_deployment(deployment)
    try:
        container_exists = executor.container_exists(deployment)
    except Exception as exc:
        record_reconcile_event(
            deployment,
            "reconcile.cleanup_failed",
            f"Failed to verify container existence: {exc}",
            level="error",
            metadata={"action": "container_exists"},
        )
        db.session.commit()
        return False

    if container_exists:
        return False

    deployment.status = "failed"
    deployment.finished_at = now_utc()
    deployment.last_error = "Reconciler detected missing runtime container for running deployment"
    deployment.build.status = "failed"
    deployment.build.finished_at = deployment.finished_at
    deployment.build.last_error = deployment.last_error
    deployment.service_url = None
    release_deployment_claim(deployment)
    record_reconcile_event(
        deployment,
        "reconcile.container_missing",
        "Marked running deployment failed because its container is missing",
        level="warning",
        metadata={"deploy_target": deployment.deploy_target},
    )
    db.session.commit()
    return True


def reconcile_running_missing_kubernetes_resource(deployment):
    if deployment.status != "running" or deployment.deploy_target != "kubernetes":
        return False
    if deployment_has_helm_release_metadata(deployment):
        return False

    executor = create_executor_for_deployment(deployment)
    try:
        resource_status = executor.runtime_resource_status(deployment)
    except WorkerExecutionError as exc:
        record_reconcile_event(
            deployment,
            "reconcile.cleanup_failed",
            f"Failed to verify Kubernetes resources: {exc.message}",
            level="error",
            metadata=exc.metadata | {"step": exc.step},
        )
        db.session.commit()
        return False
    except Exception as exc:
        record_reconcile_event(
            deployment,
            "reconcile.cleanup_failed",
            f"Failed to verify Kubernetes resources: {exc}",
            level="error",
            metadata={"action": "kubernetes_resource_exists"},
        )
        db.session.commit()
        return False

    missing = []
    if not resource_status.get("deployment_exists", False):
        missing.append("Deployment")
    if not resource_status.get("service_exists", False):
        missing.append("Service")
    if not missing:
        return False

    diagnostics = {}
    try:
        diagnostics = executor.runtime_pod_diagnostics(deployment, prefix="reconcile")
    except Exception as exc:
        current_app.logger.warning(
            "reconcile_kubernetes_runtime_pod_diagnostics_failed",
            extra={"deployment_id": deployment.id, "error": str(exc)},
        )

    missing_message = ", ".join(missing)
    deployment.status = "failed"
    deployment.finished_at = now_utc()
    deployment.last_error = f"Reconciler detected missing Kubernetes resource(s): {missing_message}"
    deployment.build.status = "failed"
    deployment.build.finished_at = deployment.finished_at
    deployment.build.last_error = deployment.last_error
    deployment.service_url = None
    release_deployment_claim(deployment)
    record_reconcile_event(
        deployment,
        "reconcile.kubernetes_missing_resource",
        f"Marked running deployment failed because Kubernetes resource is missing: {missing_message}",
        level="error",
        step="reconcile.kubernetes_missing_resource",
        metadata={
            "namespace": resource_status.get("namespace"),
            "deployment_name": resource_status.get("deployment_name"),
            "service_name": resource_status.get("service_name"),
            "deployment_exists": resource_status.get("deployment_exists"),
            "service_exists": resource_status.get("service_exists"),
            "missing_resources": missing,
        }
        | diagnostics,
    )
    db.session.commit()
    return True


def deployment_has_helm_release_metadata(deployment):
    if getattr(deployment, "helm_release_name", None):
        return True
    for event in getattr(deployment, "events", []) or []:
        metadata = event.metadata_json or {}
        if isinstance(metadata, dict) and metadata.get("helm_release_name"):
            return True
    return False


def reconcile_running_missing_helm_release(deployment):
    if deployment.status != "running" or deployment.deploy_target != "kubernetes":
        return False
    if not deployment_has_helm_release_metadata(deployment):
        return False

    executor = create_executor_for_deployment(deployment)
    try:
        helm_status = executor.runtime_helm_status(deployment)
    except WorkerExecutionError as exc:
        record_reconcile_event(
            deployment,
            "reconcile.cleanup_failed",
            f"Failed to verify Helm release: {exc.message}",
            level="error",
            metadata=exc.metadata | {"step": exc.step},
        )
        db.session.commit()
        return False
    except Exception as exc:
        record_reconcile_event(
            deployment,
            "reconcile.cleanup_failed",
            f"Failed to verify Helm release: {exc}",
            level="error",
            metadata={"action": "helm_release_status"},
        )
        db.session.commit()
        return False

    if helm_status is None or helm_status.get("release_exists", False):
        return False

    deployment.status = "failed"
    deployment.finished_at = now_utc()
    deployment.last_error = "Reconciler detected missing Helm release for running deployment"
    deployment.build.status = "failed"
    deployment.build.finished_at = deployment.finished_at
    deployment.build.last_error = deployment.last_error
    deployment.service_url = None
    release_deployment_claim(deployment)
    record_reconcile_event(
        deployment,
        "reconcile.helm_release_missing",
        "Marked running deployment failed because its Helm release is missing",
        level="error",
        step="reconcile.helm_release_missing",
        metadata=helm_status,
    )
    db.session.commit()
    return True


def reconcile_kubernetes_nonrunning_resources(deployment):
    if deployment.deploy_target != "kubernetes" or deployment.status not in {"failed", "stopped"}:
        return False
    if deployment_has_helm_release_metadata(deployment):
        return False

    changed = False
    executor = create_executor_for_deployment(deployment)

    try:
        resource_status = executor.runtime_resource_status(deployment)
    except WorkerExecutionError as exc:
        record_reconcile_event(
            deployment,
            "reconcile.cleanup_failed",
            f"Failed to inspect Kubernetes resources for cleanup: {exc.message}",
            level="error",
            metadata=exc.metadata | {"step": exc.step},
        )
        db.session.commit()
        return False
    except Exception as exc:
        record_reconcile_event(
            deployment,
            "reconcile.cleanup_failed",
            f"Failed to inspect Kubernetes resources for cleanup: {exc}",
            level="error",
            metadata={"action": "kubernetes_resource_cleanup_inspect"},
        )
        db.session.commit()
        return False

    leftovers = []
    if resource_status.get("deployment_exists", False):
        leftovers.append("Deployment")
    if resource_status.get("service_exists", False):
        leftovers.append("Service")
    if resource_status.get("ingress_exists", False):
        leftovers.append("Ingress")
    if not leftovers:
        return False

    try:
        stop_result = executor.stop(deployment)
        diagnostics = {}
        try:
            diagnostics = executor.runtime_pod_diagnostics(deployment, prefix="reconcile_cleanup")
        except Exception as exc:
            current_app.logger.warning(
                "reconcile_kubernetes_cleanup_pod_diagnostics_failed",
                extra={"deployment_id": deployment.id, "error": str(exc)},
            )
        record_reconcile_event(
            deployment,
            "reconcile.kubernetes_resources_removed",
            "Removed leftover Kubernetes resources from non-running deployment",
            metadata=stop_result.metadata
            | {
                "log_path": stop_result.log_path,
                "leftover_resources": leftovers,
                "namespace": resource_status.get("namespace"),
                "deployment_name": resource_status.get("deployment_name"),
                "service_name": resource_status.get("service_name"),
            }
            | diagnostics,
        )
        changed = True
    except WorkerExecutionError as exc:
        record_reconcile_event(
            deployment,
            "reconcile.cleanup_failed",
            f"Failed to remove leftover Kubernetes resources: {exc.message}",
            level="error",
            metadata=exc.metadata
            | {
                "step": exc.step,
                "leftover_resources": leftovers,
                "namespace": resource_status.get("namespace"),
                "deployment_name": resource_status.get("deployment_name"),
                "service_name": resource_status.get("service_name"),
            },
        )
    except Exception as exc:
        record_reconcile_event(
            deployment,
            "reconcile.cleanup_failed",
            f"Failed to remove leftover Kubernetes resources: {exc}",
            level="error",
            metadata={
                "action": "kubernetes_resource_cleanup",
                "leftover_resources": leftovers,
                "namespace": resource_status.get("namespace"),
                "deployment_name": resource_status.get("deployment_name"),
                "service_name": resource_status.get("service_name"),
            },
        )

    if changed:
        db.session.commit()
    else:
        db.session.flush()
    return changed


def reconcile_helm_nonrunning_release(deployment):
    if deployment.deploy_target != "kubernetes" or deployment.status not in {"failed", "stopped"}:
        return False
    if not deployment_has_helm_release_metadata(deployment):
        return False

    executor = create_executor_for_deployment(deployment)
    try:
        helm_status = executor.runtime_helm_status(deployment)
    except WorkerExecutionError as exc:
        record_reconcile_event(
            deployment,
            "reconcile.helm_cleanup_failed",
            f"Failed to inspect Helm release for cleanup: {exc.message}",
            level="error",
            metadata=exc.metadata | {"step": exc.step},
        )
        db.session.commit()
        return False
    except Exception as exc:
        record_reconcile_event(
            deployment,
            "reconcile.helm_cleanup_failed",
            f"Failed to inspect Helm release for cleanup: {exc}",
            level="error",
            metadata={"action": "helm_release_cleanup_inspect"},
        )
        db.session.commit()
        return False

    if helm_status is None or not helm_status.get("release_exists", False):
        return False

    try:
        stop_result = executor.stop(deployment)
        record_reconcile_event(
            deployment,
            "reconcile.helm_release_removed",
            "Removed leftover Helm release from non-running deployment",
            metadata=stop_result.metadata
            | {
                "log_path": stop_result.log_path,
                "helm_release_name": helm_status.get("helm_release_name"),
                "namespace": helm_status.get("namespace"),
                "release_status": helm_status.get("release_status"),
            },
        )
        db.session.commit()
        return True
    except WorkerExecutionError as exc:
        record_reconcile_event(
            deployment,
            "reconcile.helm_cleanup_failed",
            f"Failed to remove leftover Helm release: {exc.message}",
            level="error",
            metadata=exc.metadata
            | {
                "step": exc.step,
                "helm_release_name": helm_status.get("helm_release_name"),
                "namespace": helm_status.get("namespace"),
                "release_status": helm_status.get("release_status"),
            },
        )
    except Exception as exc:
        record_reconcile_event(
            deployment,
            "reconcile.helm_cleanup_failed",
            f"Failed to remove leftover Helm release: {exc}",
            level="error",
            metadata={
                "action": "helm_release_cleanup",
                "helm_release_name": helm_status.get("helm_release_name"),
                "namespace": helm_status.get("namespace"),
                "release_status": helm_status.get("release_status"),
            },
        )

    db.session.flush()
    return False


def reconcile_failed_artifacts(deployment):
    if deployment.status != "failed":
        return False

    changed = False
    executor = create_executor_for_deployment(deployment)

    if deployment.deploy_target == "local-docker":
        try:
            if executor.container_exists(deployment):
                stop_result = executor.stop(deployment)
                record_reconcile_event(
                    deployment,
                    "reconcile.container_removed",
                    "Removed orphan container from failed deployment",
                    metadata=stop_result.metadata | {"log_path": stop_result.log_path},
                )
                changed = True
        except WorkerExecutionError as exc:
            record_reconcile_event(
                deployment,
                "reconcile.cleanup_failed",
                f"Failed to remove orphan container: {exc.message}",
                level="error",
                metadata=exc.metadata | {"step": exc.step},
            )
        except Exception as exc:
            record_reconcile_event(
                deployment,
                "reconcile.cleanup_failed",
                f"Failed to remove orphan container: {exc}",
                level="error",
                metadata={"action": "container_cleanup"},
            )

    try:
        cleanup_metadata = executor.cleanup_workspace(deployment)
        if cleanup_metadata.get("workspace_removed") or cleanup_metadata.get("log_removed"):
            record_reconcile_event(
                deployment,
                "reconcile.workspace_removed",
                "Removed stale workspace artifacts from failed deployment",
                metadata=cleanup_metadata,
            )
            changed = True
    except Exception as exc:
        record_reconcile_event(
            deployment,
            "reconcile.cleanup_failed",
            f"Failed to remove workspace artifacts: {exc}",
            level="error",
            metadata={"action": "workspace_cleanup"},
        )

    if changed:
        db.session.commit()
    else:
        db.session.flush()
    return changed


def reconcile_deployment(deployment):
    changes = 0
    if reconcile_stale_claim(deployment):
        deployment = db.session.get(PlatformDeployment, deployment.id)
        changes += 1
    if reconcile_running_missing_container(deployment):
        deployment = db.session.get(PlatformDeployment, deployment.id)
        changes += 1
    if reconcile_running_missing_helm_release(deployment):
        deployment = db.session.get(PlatformDeployment, deployment.id)
        changes += 1
    if reconcile_running_missing_kubernetes_resource(deployment):
        deployment = db.session.get(PlatformDeployment, deployment.id)
        changes += 1
    if reconcile_helm_nonrunning_release(deployment):
        deployment = db.session.get(PlatformDeployment, deployment.id)
        changes += 1
    if reconcile_kubernetes_nonrunning_resources(deployment):
        deployment = db.session.get(PlatformDeployment, deployment.id)
        changes += 1
    if reconcile_failed_artifacts(deployment):
        changes += 1
    return changes


def reconcile_deployments(*, emitter=None):
    emitter = emitter or (lambda _message: None)
    total_changes = 0
    for deployment in iter_reconcilable_deployments():
        try:
            changes = reconcile_deployment(deployment)
        except Exception as exc:
            current_app.logger.exception("reconcile_deployment_failed", extra={"deployment_id": deployment.id})
            emitter(f"Deployment {deployment.id}: reconciliation error: {exc}")
            db.session.rollback()
            continue
        if changes:
            total_changes += changes
            emitter(f"Deployment {deployment.id}: applied {changes} reconciliation action(s)")
    return total_changes
