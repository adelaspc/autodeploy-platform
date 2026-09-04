"""Reconcile manifest-managed deployments with their Kubernetes resources."""

from flask import current_app

from control_plane.extensions import db
from worker.execution.contracts import WorkerExecutionError
from worker.processing.claims import now_utc, release_deployment_claim
from worker.reconciliation.events import record_reconcile_event
from worker.reconciliation.helm import deployment_has_helm_release_metadata


def reconcile_running_missing_kubernetes_resource(deployment, *, executor_factory):
    if deployment.status != "running" or deployment.deploy_target != "kubernetes":
        return False
    if deployment_has_helm_release_metadata(deployment):
        return False

    executor = executor_factory(deployment)
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
    deployment.transition_to("failed")
    deployment.finished_at = now_utc()
    deployment.last_error = f"Reconciler detected missing Kubernetes resource(s): {missing_message}"
    deployment.build.transition_to("failed")
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


def reconcile_kubernetes_nonrunning_resources(deployment, *, executor_factory):
    if deployment.deploy_target != "kubernetes" or deployment.status not in {"failed", "stopped"}:
        return False
    if deployment_has_helm_release_metadata(deployment):
        return False

    changed = False
    executor = executor_factory(deployment)

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

    db.session.commit()
    return changed
