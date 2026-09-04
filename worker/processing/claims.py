"""Coordinate deployment ownership across one or more worker processes."""

from datetime import datetime, timedelta, timezone

from flask import current_app
from sqlalchemy import and_, or_, select, update

from control_plane.extensions import db
from control_plane.models import DeploymentCommand, PlatformDeployment
from worker.processing.events import record_event


class ClaimLostError(Exception):
    def __init__(self, deployment_id, worker_id, current_claimed_by, current_claimed_at, status):
        super().__init__("Deployment claim was lost before worker completed processing")
        self.deployment_id = deployment_id
        self.worker_id = worker_id
        self.current_claimed_by = current_claimed_by
        self.current_claimed_at = current_claimed_at
        self.status = status

    @property
    def metadata(self):
        return {
            "worker_id": self.worker_id,
            "current_claimed_by": self.current_claimed_by,
            "current_claimed_at": self.current_claimed_at.isoformat() if self.current_claimed_at else None,
        }


class DeploymentCancellationRequested(Exception):
    def __init__(self, deployment_id, command_id, command_type):
        super().__init__(f"Deployment {command_type} command requested while deployment was processing")
        self.deployment_id = deployment_id
        self.command_id = command_id
        self.command_type = command_type

    @property
    def metadata(self):
        return {
            "command_id": self.command_id,
            "command_type": self.command_type,
        }


def now_utc():
    return datetime.now(timezone.utc)


def worker_id():
    return current_app.config.get("CONTROL_PLANE_WORKER_ID", "worker")


def log_claim(action, deployment_id, **fields):
    current_app.logger.info("claim_%s", action, extra={"deployment_id": deployment_id, **fields})


def write_claim_event(deployment, event_type, message, *, level="info", metadata=None):
    record_event(
        deployment,
        event_type,
        deployment.status,
        message,
        step="claim",
        level=level,
        metadata=metadata,
    )


def release_deployment_claim(deployment):
    previous_claimed_at = deployment.claimed_at
    previous_claimed_by = deployment.claimed_by
    deployment.claimed_at = None
    deployment.claimed_by = None
    log_claim(
        "cleared",
        deployment.id,
        worker_id=previous_claimed_by,
        previous_claimed_at=previous_claimed_at.isoformat() if previous_claimed_at else None,
    )


def ensure_claim_owned(deployment, *, expected_worker_id=None):
    # Long-running steps check the database again before important writes. A
    # worker that lost ownership must not overwrite the new owner's result.
    expected_worker_id = expected_worker_id or worker_id()
    claim_state = db.session.execute(
        select(
            PlatformDeployment.claimed_by,
            PlatformDeployment.claimed_at,
            PlatformDeployment.status,
        ).where(PlatformDeployment.id == deployment.id)
    ).one_or_none()
    if claim_state is None:
        raise ClaimLostError(deployment.id, expected_worker_id, None, None, deployment.status)

    current_claimed_by, current_claimed_at, current_status = claim_state
    if current_claimed_by != expected_worker_id or current_claimed_at is None:
        raise ClaimLostError(
            deployment.id,
            expected_worker_id,
            current_claimed_by,
            current_claimed_at,
            current_status,
        )

    cancellation = db.session.execute(
        select(DeploymentCommand.id, DeploymentCommand.command_type)
        .where(
            DeploymentCommand.deployment_id == deployment.id,
            DeploymentCommand.command_type == "stop",
            DeploymentCommand.status.in_(("pending", "claimed")),
            DeploymentCommand.active_key == "active",
        )
        .order_by(DeploymentCommand.requested_at.asc())
        .limit(1)
    ).one_or_none()
    if cancellation is not None:
        command_id, command_type = cancellation
        raise DeploymentCancellationRequested(deployment.id, command_id, command_type)
    return current_claimed_at


def refresh_claim(deployment, *, expected_worker_id=None):
    expected_worker_id = expected_worker_id or worker_id()
    previous_claimed_at = ensure_claim_owned(deployment, expected_worker_id=expected_worker_id)
    new_claimed_at = now_utc()
    updated = db.session.execute(
        update(PlatformDeployment)
        .where(
            and_(
                PlatformDeployment.id == deployment.id,
                PlatformDeployment.claimed_by == expected_worker_id,
                PlatformDeployment.claimed_at.is_not(None),
            )
        )
        .values(claimed_at=new_claimed_at)
        .execution_options(synchronize_session=False)
    ).rowcount
    if not updated:
        raise ClaimLostError(deployment.id, expected_worker_id, None, None, deployment.status)
    deployment.claimed_at = new_claimed_at
    log_claim(
        "refreshed",
        deployment.id,
        worker_id=expected_worker_id,
        previous_claimed_at=previous_claimed_at.isoformat() if previous_claimed_at else None,
        new_claimed_at=new_claimed_at.isoformat(),
    )
    return new_claimed_at


def persist_claim_loss(deployment_id, error):
    db.session.rollback()
    deployment = db.session.get(PlatformDeployment, deployment_id)
    if deployment is None:
        return None

    log_claim(
        "lost",
        deployment_id,
        worker_id=error.worker_id,
        current_claimed_by=error.current_claimed_by,
        current_claimed_at=error.current_claimed_at.isoformat() if error.current_claimed_at else None,
    )
    write_claim_event(deployment, "claim_lost", str(error), level="warning", metadata=error.metadata)
    db.session.commit()
    return deployment


def persist_cancellation_acknowledgement(deployment_id, error):
    db.session.rollback()
    deployment = db.session.get(PlatformDeployment, deployment_id)
    if deployment is None:
        return None

    write_claim_event(
        deployment,
        "deployment.cancellation_acknowledged",
        "Worker stopped deployment processing after observing a pending stop command",
        level="warning",
        metadata=error.metadata,
    )
    release_deployment_claim(deployment)
    db.session.commit()
    return deployment


def claim_heartbeat(deployment, *, expected_worker_id=None):
    refresh_claim(deployment, expected_worker_id=expected_worker_id)
    db.session.commit()


def attach_claim_heartbeat(executor, deployment, *, expected_worker_id=None):
    def heartbeat():
        claim_heartbeat(deployment, expected_worker_id=expected_worker_id)

    setter = getattr(executor, "set_heartbeat", None)
    if callable(setter):
        setter(heartbeat)
    else:
        setattr(executor, "heartbeat", heartbeat)


def claim_next_pending_deployment(*, worker_id=None, claim_ttl_seconds=None):
    """Atomically claim the oldest pending deployment available to this worker."""
    worker_name = worker_id or current_app.config.get("CONTROL_PLANE_WORKER_ID", "worker")
    claim_ttl_seconds = (
        current_app.config.get("CONTROL_PLANE_CLAIM_TTL_SECONDS", 300)
        if claim_ttl_seconds is None
        else claim_ttl_seconds
    )
    stale_before = now_utc() - timedelta(seconds=claim_ttl_seconds)

    candidate_rows = list(
        db.session.execute(
            select(
                PlatformDeployment.id,
                PlatformDeployment.claimed_at,
                PlatformDeployment.claimed_by,
            )
            .where(PlatformDeployment.status == "pending")
            .order_by(PlatformDeployment.created_at.asc())
        )
    )

    claimed_at = now_utc()
    for deployment_id, previous_claimed_at, previous_claimed_by in candidate_rows:
        # The guarded update is the actual lock. Candidate reads may race, but
        # only one worker can change an unclaimed or expired row.
        claimed_count = db.session.execute(
            update(PlatformDeployment)
            .where(
                and_(
                    PlatformDeployment.id == deployment_id,
                    PlatformDeployment.status == "pending",
                    or_(
                        PlatformDeployment.claimed_at.is_(None),
                        PlatformDeployment.claimed_at < stale_before,
                    ),
                )
            )
            .values(claimed_at=claimed_at, claimed_by=worker_name)
            .execution_options(synchronize_session=False)
        ).rowcount
        if claimed_count:
            db.session.commit()
            deployment = db.session.get(PlatformDeployment, deployment_id)
            metadata = {
                "worker_id": worker_name,
                "claimed_at": claimed_at.isoformat(),
                "previous_claimed_by": previous_claimed_by,
                "previous_claimed_at": previous_claimed_at.isoformat() if previous_claimed_at else None,
                "reclaimed_stale": previous_claimed_at is not None,
            }
            write_claim_event(deployment, "claim_acquired", "Worker claimed pending deployment", metadata=metadata)
            db.session.commit()
            log_claim(
                "acquired",
                deployment_id,
                worker_id=worker_name,
                previous_claimed_by=previous_claimed_by,
                previous_claimed_at=previous_claimed_at.isoformat() if previous_claimed_at else None,
                new_claimed_at=claimed_at.isoformat(),
                reclaimed_stale=previous_claimed_at is not None,
            )
            return deployment

    db.session.rollback()
    return None
