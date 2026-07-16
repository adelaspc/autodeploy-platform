"""add deployment spec snapshot

Revision ID: 8a3d6e1f5c9b
Revises: 5e9a2c7d1f4b
"""

from alembic import op
import sqlalchemy as sa


revision = "8a3d6e1f5c9b"
down_revision = "5e9a2c7d1f4b"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("platform_deployments") as batch_op:
        batch_op.add_column(sa.Column("spec_snapshot_json", sa.JSON(), nullable=True))

    projects = sa.table(
        "projects",
        sa.column("id", sa.Integer()),
        sa.column("name", sa.String()),
        sa.column("repo_url", sa.String()),
        sa.column("branch", sa.String()),
        sa.column("git_auth_type", sa.String()),
        sa.column("git_secret_ref", sa.String()),
        sa.column("dockerfile_path", sa.String()),
        sa.column("build_context", sa.String()),
        sa.column("port", sa.Integer()),
        sa.column("healthcheck_path", sa.String()),
        sa.column("env_vars", sa.JSON()),
        sa.column("cpu", sa.String()),
        sa.column("memory", sa.String()),
        sa.column("runtime", sa.String()),
    )
    deployments = sa.table(
        "platform_deployments",
        sa.column("id", sa.Integer()),
        sa.column("project_id", sa.Integer()),
        sa.column("spec_snapshot_json", sa.JSON()),
    )
    connection = op.get_bind()
    project_fields = (
        "id",
        "name",
        "repo_url",
        "branch",
        "git_auth_type",
        "git_secret_ref",
        "dockerfile_path",
        "build_context",
        "port",
        "healthcheck_path",
        "env_vars",
        "cpu",
        "memory",
        "runtime",
    )
    rows = connection.execute(
        sa.select(
            deployments.c.id.label("deployment_id"),
            *(projects.c[field].label(f"project_{field}") for field in project_fields),
        ).select_from(deployments.join(projects, deployments.c.project_id == projects.c.id))
    ).mappings()
    for row in rows:
        snapshot = {
            "version": 1,
            "project": {field: row[f"project_{field}"] for field in project_fields},
        }
        connection.execute(
            deployments.update()
            .where(deployments.c.id == row["deployment_id"])
            .values(spec_snapshot_json=snapshot)
        )


def downgrade():
    with op.batch_alter_table("platform_deployments") as batch_op:
        batch_op.drop_column("spec_snapshot_json")
