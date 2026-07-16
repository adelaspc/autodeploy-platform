from datetime import timedelta

from flask import current_app

from control_plane.extensions import db
from worker.processing.claims import now_utc, release_deployment_claim
from worker.reconciliation.events import record_reconcile_event


IN_PROGRESS_STATUSES = {"cloning", "building", "testing", "pushing_image", "deploying"}


def stale_claim_cutoff():
    return now_utc() - timedelta(seconds=current_app.config.get("CONTROL_PLANE_CLAIM_TTL_SECONDS", 300))


def normalize_timestamp(value):
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=now_utc().tzinfo)
    return value


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
        deployment.transition_to("failed")
        deployment.finished_at = now_utc()
        deployment.last_error = "Reconciler marked deployment failed after stale claim"
        deployment.build.transition_to("failed")
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
