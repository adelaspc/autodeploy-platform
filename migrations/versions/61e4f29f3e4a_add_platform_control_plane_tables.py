"""add platform control plane tables

Revision ID: 61e4f29f3e4a
Revises: 322374a72d98
Create Date: 2026-04-29 19:10:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "61e4f29f3e4a"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "projects",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("repo_url", sa.String(length=255), nullable=False),
        sa.Column("branch", sa.String(length=120), nullable=False),
        sa.Column("dockerfile_path", sa.String(length=255), nullable=False),
        sa.Column("build_context", sa.String(length=255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("healthcheck_path", sa.String(length=255), nullable=False),
        sa.Column("env_vars", sa.JSON(), nullable=False),
        sa.Column("migration_command", sa.String(length=255), nullable=True),
        sa.Column("cpu", sa.String(length=32), nullable=True),
        sa.Column("memory", sa.String(length=32), nullable=True),
        sa.Column("trigger", sa.String(length=32), nullable=False),
        sa.Column("runtime", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "builds",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("commit_sha", sa.String(length=64), nullable=False),
        sa.Column("registry", sa.String(length=255), nullable=True),
        sa.Column("image_name", sa.String(length=255), nullable=True),
        sa.Column("image_tag", sa.String(length=255), nullable=True),
        sa.Column("image_ref", sa.String(length=512), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("test_command", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_builds_project_id"), "builds", ["project_id"], unique=False)
    op.create_table(
        "platform_deployments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("build_id", sa.Integer(), nullable=False),
        sa.Column("environment", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("service_url", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["build_id"], ["builds.id"]),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_platform_deployments_build_id"), "platform_deployments", ["build_id"], unique=False)
    op.create_index(op.f("ix_platform_deployments_project_id"), "platform_deployments", ["project_id"], unique=False)
    op.create_table(
        "deployment_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("deployment_id", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["deployment_id"], ["platform_deployments.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_deployment_events_deployment_id"), "deployment_events", ["deployment_id"], unique=False)


def downgrade():
    op.drop_index(op.f("ix_deployment_events_deployment_id"), table_name="deployment_events")
    op.drop_table("deployment_events")
    op.drop_index(op.f("ix_platform_deployments_project_id"), table_name="platform_deployments")
    op.drop_index(op.f("ix_platform_deployments_build_id"), table_name="platform_deployments")
    op.drop_table("platform_deployments")
    op.drop_index(op.f("ix_builds_project_id"), table_name="builds")
    op.drop_table("builds")
    op.drop_table("projects")
