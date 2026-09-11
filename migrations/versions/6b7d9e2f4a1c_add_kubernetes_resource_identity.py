"""add stable kubernetes manifest resource identity

Revision ID: 6b7d9e2f4a1c
Revises: 4a6c8e1f2b3d
"""

import json

from alembic import op
import sqlalchemy as sa


revision = "6b7d9e2f4a1c"
down_revision = "4a6c8e1f2b3d"
branch_labels = None
depends_on = None


def _metadata(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _legacy_manifest_deployment_name(project_name, deployment_id):
    # Preserve the exact pre-migration algorithm so cleanup can still address
    # resources created by older workers, including names no longer considered valid.
    sanitized = "".join(
        char.lower() if char.isalnum() or char in {"-", "_", "."} else "-"
        for char in str(project_name or "")
    )
    base = sanitized.strip("-") or "app"
    return f"paas-{base}-{deployment_id}"


def upgrade():
    with op.batch_alter_table("platform_deployments") as batch_op:
        batch_op.add_column(sa.Column("kubernetes_deployment_name", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("kubernetes_service_name", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("kubernetes_ingress_name", sa.String(length=255), nullable=True))

    deployments = sa.table(
        "platform_deployments",
        sa.column("id", sa.Integer()),
        sa.column("project_id", sa.Integer()),
        sa.column("deploy_target", sa.String()),
        sa.column("kubernetes_deployment_mode", sa.String()),
        sa.column("spec_snapshot_json", sa.JSON()),
        sa.column("kubernetes_deployment_name", sa.String()),
        sa.column("kubernetes_service_name", sa.String()),
        sa.column("kubernetes_ingress_name", sa.String()),
    )
    projects = sa.table(
        "projects",
        sa.column("id", sa.Integer()),
        sa.column("name", sa.String()),
    )
    events = sa.table(
        "deployment_events",
        sa.column("id", sa.Integer()),
        sa.column("deployment_id", sa.Integer()),
        sa.column("metadata_json", sa.JSON()),
    )
    connection = op.get_bind()

    project_names = dict(connection.execute(sa.select(projects.c.id, projects.c.name)).all())
    historical_names = {}
    event_rows = connection.execute(
        sa.select(events.c.deployment_id, events.c.metadata_json).order_by(events.c.id.asc())
    ).mappings()
    for event_row in event_rows:
        metadata = _metadata(event_row["metadata_json"])
        identity = historical_names.setdefault(event_row["deployment_id"], {})
        for key in ("deployment_name", "service_name", "ingress_name"):
            value = metadata.get(key)
            if key not in identity and isinstance(value, str) and value.strip():
                identity[key] = value.strip()

    rows = connection.execute(
        sa.select(
            deployments.c.id,
            deployments.c.project_id,
            deployments.c.deploy_target,
            deployments.c.kubernetes_deployment_mode,
            deployments.c.spec_snapshot_json,
        )
    ).mappings()
    for row in rows:
        if row["deploy_target"] != "kubernetes" or row["kubernetes_deployment_mode"] != "manifest":
            continue

        history = historical_names.get(row["id"], {})
        snapshot = _metadata(row["spec_snapshot_json"])
        project_snapshot = snapshot.get("project") if isinstance(snapshot.get("project"), dict) else {}
        project_name = project_snapshot.get("name") or project_names.get(row["project_id"])
        deployment_name = history.get("deployment_name") or _legacy_manifest_deployment_name(
            project_name, row["id"]
        )
        connection.execute(
            deployments.update()
            .where(deployments.c.id == row["id"])
            .values(
                kubernetes_deployment_name=deployment_name,
                kubernetes_service_name=history.get("service_name") or f"{deployment_name}-svc",
                kubernetes_ingress_name=history.get("ingress_name") or deployment_name,
            )
        )


def downgrade():
    with op.batch_alter_table("platform_deployments") as batch_op:
        batch_op.drop_column("kubernetes_ingress_name")
        batch_op.drop_column("kubernetes_service_name")
        batch_op.drop_column("kubernetes_deployment_name")
