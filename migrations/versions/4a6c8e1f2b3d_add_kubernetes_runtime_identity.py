"""add stable kubernetes runtime identity

Revision ID: 4a6c8e1f2b3d
Revises: 3f8a1b2c4d5e
"""

import json

from alembic import op
import sqlalchemy as sa


revision = "4a6c8e1f2b3d"
down_revision = "3f8a1b2c4d5e"
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


def upgrade():
    with op.batch_alter_table("platform_deployments") as batch_op:
        batch_op.add_column(sa.Column("kubernetes_deployment_mode", sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column("kubernetes_namespace", sa.String(length=255), nullable=True))

    deployments = sa.table(
        "platform_deployments",
        sa.column("id", sa.Integer()),
        sa.column("deploy_target", sa.String()),
        sa.column("helm_release_name", sa.String()),
        sa.column("helm_namespace", sa.String()),
        sa.column("preflight_metadata_json", sa.JSON()),
        sa.column("kubernetes_deployment_mode", sa.String()),
        sa.column("kubernetes_namespace", sa.String()),
    )
    events = sa.table(
        "deployment_events",
        sa.column("id", sa.Integer()),
        sa.column("deployment_id", sa.Integer()),
        sa.column("metadata_json", sa.JSON()),
    )
    connection = op.get_bind()

    historical_identity = {}
    event_rows = connection.execute(
        sa.select(events.c.deployment_id, events.c.metadata_json).order_by(events.c.id.asc())
    ).mappings()
    for event_row in event_rows:
        metadata = _metadata(event_row["metadata_json"])
        identity = historical_identity.setdefault(event_row["deployment_id"], {})
        for key in ("deployment_mode", "namespace"):
            value = metadata.get(key)
            if key not in identity and isinstance(value, str) and value.strip():
                identity[key] = value.strip()

    rows = connection.execute(
        sa.select(
            deployments.c.id,
            deployments.c.deploy_target,
            deployments.c.helm_release_name,
            deployments.c.helm_namespace,
            deployments.c.preflight_metadata_json,
        )
    ).mappings()
    for row in rows:
        if row["deploy_target"] != "kubernetes":
            continue
        preflight = _metadata(row["preflight_metadata_json"])
        history = historical_identity.get(row["id"], {})
        historical_mode = preflight.get("deployment_mode") or history.get("deployment_mode")
        mode = historical_mode if historical_mode in {"manifest", "helm"} else None
        if mode is None:
            mode = "helm" if row["helm_release_name"] else "manifest"

        namespace = row["helm_namespace"] or preflight.get("namespace") or history.get("namespace")
        values = {"kubernetes_deployment_mode": mode}
        if isinstance(namespace, str) and namespace.strip():
            values["kubernetes_namespace"] = namespace.strip()
        connection.execute(deployments.update().where(deployments.c.id == row["id"]).values(**values))


def downgrade():
    with op.batch_alter_table("platform_deployments") as batch_op:
        batch_op.drop_column("kubernetes_namespace")
        batch_op.drop_column("kubernetes_deployment_mode")
