"""Load deployment aggregates with the relationships required by API services."""

from flask import abort
from sqlalchemy.orm import selectinload

from control_plane.extensions import db
from control_plane.models import PlatformDeployment, Project


def get_project_or_404(project_id):
    project = db.session.get(Project, project_id, populate_existing=True)
    if project is None:
        abort(404)
    return project


def get_project_deployment_or_404(project_id, deployment_id):
    deployment = (
        PlatformDeployment.query.options(
            selectinload(PlatformDeployment.project),
            selectinload(PlatformDeployment.build),
            selectinload(PlatformDeployment.events),
        )
        .populate_existing()
        .filter_by(id=deployment_id, project_id=project_id)
        .first()
    )
    if deployment is None:
        abort(404)
    return deployment
