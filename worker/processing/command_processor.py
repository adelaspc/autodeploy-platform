from datetime import datetime, timedelta, timezone

from flask import current_app
from sqlalchemy import and_, or_, select, update

from control_plane.deployment_runtime_metadata import persist_helm_runtime_metadata
from control_plane.deployment_spec import project_for_deployment
from control_plane.extensions import db
from control_plane.models import DeploymentCommand, DeploymentEvent
from control_plane.security import redact_sensitive_data, redact_text, secret_values_from_env_vars
from worker.execution.contracts import WorkerExecutionError
from worker.execution.factory import create_executor_for_deployment


INTERNAL_ERROR_MESSAGE = "Worker encountered an unexpected internal error"


class CommandClaimLostError(Exception):
    pass


def now_utc():
    return datetime.now(timezone.utc)


def claim_next_pending_command(*, worker_id=None, claim_ttl_seconds=None):
    worker_name = worker_id or current_app.config.get("CONTROL_PLANE_WORKER_ID", "worker")
    ttl = current_app.config.get("CONTROL_PLANE_CLAIM_TTL_SECONDS", 300) if claim_ttl_seconds is None else claim_ttl_seconds
    stale_before = now_utc() - timedelta(seconds=ttl)
    command_ids = db.session.scalars(
        select(DeploymentCommand.id)
        .where(DeploymentCommand.status.in_(("pending", "claimed")))
        .order_by(DeploymentCommand.requested_at.asc())
    ).all()

    for command_id in command_ids:
        claimed_at = now_utc()
        claimed = db.session.execute(
            update(DeploymentCommand)
            .where(
                and_(
                    DeploymentCommand.id == command_id,
                    DeploymentCommand.status.in_(("pending", "claimed")),
                    or_(DeploymentCommand.claimed_at.is_(None), DeploymentCommand.claimed_at < stale_before),
                )
            )
            .values(status="claimed", claimed_by=worker_name, claimed_at=claimed_at)
            .execution_options(synchronize_session=False)
        ).rowcount
        if claimed:
            db.session.commit()
            return db.session.get(DeploymentCommand, command_id)

    db.session.rollback()
    return None


def command_worker_id():
    return current_app.config.get("CONTROL_PLANE_WORKER_ID", "worker")


def ensure_command_claim_owned(command, *, expected_worker_id=None):
    expected_worker_id = expected_worker_id or command_worker_id()
    claim_state = db.session.execute(
        select(DeploymentCommand.claimed_by, DeploymentCommand.claimed_at, DeploymentCommand.status).where(
            DeploymentCommand.id == command.id
        )
    ).one_or_none()
    if claim_state is None:
        raise CommandClaimLostError("Deployment command no longer exists")
    claimed_by, claimed_at, status = claim_state
    if claimed_by != expected_worker_id or claimed_at is None or status != "claimed":
        raise CommandClaimLostError("Deployment command claim was lost before processing completed")
    return claimed_at


def refresh_command_claim(command, *, expected_worker_id=None):
    expected_worker_id = expected_worker_id or command_worker_id()
    ensure_command_claim_owned(command, expected_worker_id=expected_worker_id)
    claimed_at = now_utc()
    updated = db.session.execute(
        update(DeploymentCommand)
        .where(
            and_(
                DeploymentCommand.id == command.id,
                DeploymentCommand.status == "claimed",
                DeploymentCommand.claimed_by == expected_worker_id,
                DeploymentCommand.claimed_at.is_not(None),
            )
        )
        .values(claimed_at=claimed_at)
        .execution_options(synchronize_session=False)
    ).rowcount
    if not updated:
        raise CommandClaimLostError("Deployment command claim was lost before it could be refreshed")
    command.claimed_at = claimed_at
    return claimed_at


def attach_command_heartbeat(executor, command, *, expected_worker_id=None):
    def heartbeat():
        refresh_command_claim(command, expected_worker_id=expected_worker_id)
        db.session.commit()

    setter = getattr(executor, "set_heartbeat", None)
    if callable(setter):
        setter(heartbeat)
    else:
        setattr(executor, "heartbeat", heartbeat)


def _record_event(deployment, event_type, status, message, *, step, level="info", metadata=None):
    db.session.add(
        DeploymentEvent(
            deployment=deployment,
            event_type=event_type,
            status=status,
            message=message,
            step=step,
            level=level,
            metadata_json=metadata or {},
        )
    )


def process_deployment_command(command, executor=None):
    deployment = command.deployment
    executor = executor or create_executor_for_deployment(deployment)
    expected_worker_id = command.claimed_by or command_worker_id()
    attach_command_heartbeat(executor, command, expected_worker_id=expected_worker_id)
    command_type = command.command_type
    step = f"deploy.{command_type}"

    try:
        refresh_command_claim(command, expected_worker_id=expected_worker_id)
        _record_event(
            deployment,
            f"deployment.{command_type}_started",
            deployment.status,
            command.message or f"Processing deployment {command_type}",
            step=step,
            metadata={"command_id": command.id},
        )
        db.session.commit()
        result = executor.stop(deployment)
        refresh_command_claim(command, expected_worker_id=expected_worker_id)
        deployment.transition_to("stopped")
        deployment.service_url = None
        deployment.healthcheck_url = None
        deployment.container_name = None
        deployment.container_id = None
        deployment.host_port = None
        deployment.finished_at = now_utc()
        if result.deploy_target:
            deployment.deploy_target = result.deploy_target
        persist_helm_runtime_metadata(deployment, result.metadata)
        for event in result.events or ():
            _record_event(
                deployment,
                event["event_type"],
                event["status"],
                event.get("message"),
                step=event.get("step") or step,
                level=event.get("level", "info"),
                metadata=event.get("metadata_json"),
            )
        terminal_event = "deployment.stopped" if command_type == "stop" else "deployment.cleanup_succeeded"
        _record_event(
            deployment,
            terminal_event,
            "stopped",
            result.message,
            step=step,
            metadata=(result.metadata or {}) | {"command_id": command.id, "log_path": result.log_path},
        )
        command.status = "succeeded"
        command.active_key = None
        command.claimed_by = None
        command.claimed_at = None
        command.completed_at = now_utc()
        command.last_error = None
        db.session.commit()
        return deployment
    except WorkerExecutionError as exc:
        try:
            refresh_command_claim(command, expected_worker_id=expected_worker_id)
        except CommandClaimLostError:
            db.session.rollback()
            return db.session.get(type(deployment), deployment.id)
        secret_values = secret_values_from_env_vars(project_for_deployment(deployment).env_vars)
        command.status = "failed"
        command.active_key = None
        command.claimed_by = None
        command.claimed_at = None
        command.last_error = redact_text(exc.message, secret_values=secret_values)
        command.completed_at = now_utc()
        _record_event(
            deployment,
            f"deployment.{command_type}_failed",
            deployment.status,
            command.last_error,
            step=exc.step or step,
            level="error",
            metadata=redact_sensitive_data(exc.metadata, secret_values=secret_values),
        )
        db.session.commit()
        return deployment
    except CommandClaimLostError:
        db.session.rollback()
        return db.session.get(type(deployment), deployment.id)
    except Exception as exc:
        current_app.logger.exception(
            "deployment_command_unexpected_failure",
            extra={"command_id": command.id, "deployment_id": deployment.id, "command_type": command_type},
        )
        command_id = command.id
        deployment_id = deployment.id
        db.session.rollback()
        command = db.session.get(DeploymentCommand, command_id)
        deployment = command.deployment if command is not None else None
        if command is None or deployment is None:
            return None
        try:
            refresh_command_claim(command, expected_worker_id=expected_worker_id)
        except CommandClaimLostError:
            db.session.rollback()
            return db.session.get(type(deployment), deployment_id)

        secret_values = secret_values_from_env_vars(project_for_deployment(deployment).env_vars)
        command.status = "failed"
        command.active_key = None
        command.claimed_by = None
        command.claimed_at = None
        command.last_error = INTERNAL_ERROR_MESSAGE
        command.completed_at = now_utc()
        _record_event(
            deployment,
            f"deployment.{command_type}_failed",
            deployment.status,
            INTERNAL_ERROR_MESSAGE,
            step=step,
            level="error",
            metadata=redact_sensitive_data(
                {"command_id": command.id, "error_type": type(exc).__name__},
                secret_values=secret_values,
            ),
        )
        db.session.commit()
        return deployment


def process_next_pending_command(executor=None):
    command = claim_next_pending_command()
    if command is None:
        return None
    return process_deployment_command(command, executor=executor)
