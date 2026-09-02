from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from control_plane.application.deployments.orchestration import create_deployment_event
from control_plane.extensions import db
from control_plane.models import DeploymentCommand
from control_plane.api.request_context import current_request_id


ACTIVE_COMMAND_STATUSES = ("pending", "claimed")


def request_deployment_command(deployment, command_type, *, message=None):
    # Stop and cleanup cross the same database boundary as deployments. The API
    # records the request here; a worker performs the runtime operation later.
    existing = db.session.scalar(
        select(DeploymentCommand)
        .where(
            DeploymentCommand.deployment_id == deployment.id,
            DeploymentCommand.command_type == command_type,
            DeploymentCommand.status.in_(ACTIVE_COMMAND_STATUSES),
        )
        .order_by(DeploymentCommand.requested_at.asc())
    )
    if existing is not None:
        # Reuse active commands so repeated clicks do not schedule duplicate work.
        return existing, False

    command = DeploymentCommand(
        deployment=deployment,
        command_type=command_type,
        status="pending",
        active_key="active",
        message=message,
        origin_request_id=current_request_id(),
    )
    db.session.add(command)
    create_deployment_event(
        deployment.id,
        f"deployment.{command_type}_requested",
        deployment.status,
        message or f"Deployment {command_type} requested",
        step=f"deploy.{command_type}",
        metadata_json={"command_type": command_type, "origin_request_id": command.origin_request_id},
    )
    try:
        db.session.flush()
    except IntegrityError:
        db.session.rollback()
        existing = db.session.scalar(
            select(DeploymentCommand)
            .where(
                DeploymentCommand.deployment_id == deployment.id,
                DeploymentCommand.command_type == command_type,
                DeploymentCommand.active_key == "active",
            )
            .order_by(DeploymentCommand.requested_at.asc())
        )
        if existing is None:
            raise
        return existing, False
    return command, True
