"""Detect running deployments whose local Docker container disappeared."""

from control_plane.extensions import db
from worker.processing.claims import now_utc, release_deployment_claim
from worker.reconciliation.events import record_reconcile_event


def reconcile_running_missing_container(deployment, *, executor_factory):
    if deployment.status != "running" or deployment.deploy_target != "local-docker":
        return False

    executor = executor_factory(deployment)
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

    deployment.transition_to("failed")
    deployment.finished_at = now_utc()
    deployment.last_error = "Reconciler detected missing runtime container for running deployment"
    deployment.build.transition_to("failed")
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
