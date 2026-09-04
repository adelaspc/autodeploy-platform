"""Reconcile persisted deployments with Helm releases that use stable names."""

from control_plane.extensions import db
from control_plane.models import PlatformDeployment
from worker.execution.contracts import WorkerExecutionError
from worker.processing.claims import now_utc, release_deployment_claim
from worker.reconciliation.events import record_reconcile_event


def deployment_has_helm_release_metadata(deployment):
    if getattr(deployment, "helm_release_name", None):
        return True
    for event in getattr(deployment, "events", []) or []:
        metadata = event.metadata_json or {}
        if isinstance(metadata, dict) and metadata.get("helm_release_name"):
            return True
    return False


def newer_deployment_owns_workload(deployment):
    # Helm releases are stable per project/environment, so only the newest
    # deployment may clean one up. End the current read transaction first: on
    # MySQL's repeatable-read isolation means a long reconciliation pass could
    # otherwise miss a deployment created after the pass started.
    deployment_id = deployment.id
    project_id = deployment.project_id
    environment = deployment.environment
    db.session.commit()
    return (
        PlatformDeployment.query.filter(
            PlatformDeployment.id > deployment_id,
            PlatformDeployment.project_id == project_id,
            PlatformDeployment.environment == environment,
        ).first()
        is not None
    )


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
    if newer_deployment_owns_workload(deployment):
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
