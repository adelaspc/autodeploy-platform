from sqlalchemy import select

from control_plane.application.deployments.orchestration import create_deployment_event
from control_plane.extensions import db
from control_plane.models import DeploymentCommand


ACTIVE_COMMAND_STATUSES = ("pending", "claimed")


def request_deployment_command(deployment, command_type, *, message=None):
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
        return existing, False

    command = DeploymentCommand(
        deployment=deployment,
        command_type=command_type,
        status="pending",
        message=message,
    )
    db.session.add(command)
    create_deployment_event(
        deployment.id,
        f"deployment.{command_type}_requested",
        deployment.status,
        message or f"Deployment {command_type} requested",
        step=f"deploy.{command_type}",
        metadata_json={"command_type": command_type},
    )
    db.session.flush()
    return command, True
