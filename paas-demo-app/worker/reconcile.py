from __future__ import annotations

from datetime import timedelta

from flask import current_app

from backend.extensions import db
from backend.models import DeploymentEvent, PlatformDeployment
from worker.executor import WorkerExecutionError, create_executor_for_deployment
from worker.service import now_utc, release_deployment_claim


IN_PROGRESS_STATUSES = {"cloning", "building", "testing", "pushing_image", "deploying"}


def record_reconcile_event(deployment, event_type, message, *, level="info", metadata=None):
    db.session.add(
        DeploymentEvent(
            deployment_id=deployment.id,
            event_type=event_type,
            step="reconcile",
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
