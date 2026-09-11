"""Reconcile persisted deployments with Helm releases that use stable names."""

from control_plane.extensions import db
from worker.execution.contracts import WorkerExecutionError
from worker.helm.ownership import deployment_has_helm_release_metadata, newer_helm_workload_owner_id
from worker.processing.claims import now_utc, release_deployment_claim
from worker.reconciliation.events import record_reconcile_event


def reconcile_running_missing_helm_release(deployment, *, executor_factory):
    if deployment.status != "running" or deployment.deploy_target != "kubernetes":
        return False
    if not deployment_has_helm_release_metadata(deployment):
        return False

    executor = executor_factory(deployment)
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

    deployment.transition_to("failed")
    deployment.finished_at = now_utc()
    deployment.last_error = "Reconciler detected missing Helm release for running deployment"
    deployment.build.transition_to("failed")
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


def reconcile_helm_nonrunning_release(deployment, *, executor_factory):
    if deployment.deploy_target != "kubernetes" or deployment.status not in {"failed", "stopped"}:
        return False
    if not deployment_has_helm_release_metadata(deployment):
        return False
    if newer_helm_workload_owner_id(deployment) is not None:
        return False

    executor = executor_factory(deployment)
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

    db.session.commit()
    return False
