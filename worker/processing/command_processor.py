from datetime import datetime, timedelta, timezone

from flask import current_app
from sqlalchemy import and_, or_, select, update

from control_plane.deployment_runtime_metadata import persist_helm_runtime_metadata
from control_plane.extensions import db
from control_plane.models import DeploymentCommand, DeploymentEvent
from control_plane.security import redact_sensitive_data, redact_text, secret_values_from_env_vars
from worker.execution.contracts import WorkerExecutionError
from worker.execution.factory import create_executor_for_deployment


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
    command_type = command.command_type
    step = f"deploy.{command_type}"
    _record_event(
        deployment,
        f"deployment.{command_type}_started",
        deployment.status,
        command.message or f"Processing deployment {command_type}",
        step=step,
        metadata={"command_id": command.id},
    )
    db.session.commit()

    try:
        result = executor.stop(deployment)
        deployment.status = "stopped"
        deployment.service_url = None
        deployment.healthcheck_url = None
        deployment.finished_at = now_utc()
        if result.deploy_target:
            deployment.deploy_target = result.deploy_target
        if result.container_name:
            deployment.container_name = result.container_name
        if result.container_id:
            deployment.container_id = result.container_id
        if result.host_port is not None:
            deployment.host_port = result.host_port
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
        command.completed_at = now_utc()
        command.last_error = None
        db.session.commit()
        return deployment
    except WorkerExecutionError as exc:
        secret_values = secret_values_from_env_vars(deployment.project.env_vars if deployment.project else [])
        command.status = "failed"
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


def process_next_pending_command(executor=None):
    command = claim_next_pending_command()
    if command is None:
        return None
    return process_deployment_command(command, executor=executor)
