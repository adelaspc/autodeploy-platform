"""add worker observability fields

Revision ID: 7c3eb9a6f1b2
Revises: 61e4f29f3e4a
Create Date: 2026-05-02 19:15:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "7c3eb9a6f1b2"
down_revision = "61e4f29f3e4a"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("builds") as batch_op:
        batch_op.add_column(sa.Column("workspace_path", sa.String(length=1024), nullable=True))
        batch_op.add_column(sa.Column("log_path", sa.String(length=1024), nullable=True))
        batch_op.add_column(sa.Column("last_error", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("started_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("finished_at", sa.DateTime(), nullable=True))

    with op.batch_alter_table("platform_deployments") as batch_op:
        batch_op.add_column(sa.Column("deploy_target", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("container_name", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("container_id", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("host_port", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("healthcheck_url", sa.String(length=1024), nullable=True))
        batch_op.add_column(sa.Column("last_error", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("started_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("finished_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("claimed_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("claimed_by", sa.String(length=255), nullable=True))

    with op.batch_alter_table("deployment_events") as batch_op:
        batch_op.add_column(sa.Column("step", sa.String(length=120), nullable=True))
        batch_op.add_column(sa.Column("level", sa.String(length=16), nullable=False, server_default="info"))
        batch_op.add_column(sa.Column("metadata_json", sa.JSON(), nullable=True))


def downgrade():
    with op.batch_alter_table("deployment_events") as batch_op:
        batch_op.drop_column("metadata_json")
        batch_op.drop_column("level")
        batch_op.drop_column("step")

    with op.batch_alter_table("platform_deployments") as batch_op:
        batch_op.drop_column("claimed_by")
        batch_op.drop_column("claimed_at")
        batch_op.drop_column("finished_at")
        batch_op.drop_column("started_at")
        batch_op.drop_column("last_error")
        batch_op.drop_column("healthcheck_url")
        batch_op.drop_column("host_port")
        batch_op.drop_column("container_id")
        batch_op.drop_column("container_name")
        batch_op.drop_column("deploy_target")

    with op.batch_alter_table("builds") as batch_op:
        batch_op.drop_column("finished_at")
        batch_op.drop_column("started_at")
        batch_op.drop_column("last_error")
        batch_op.drop_column("log_path")
        batch_op.drop_column("workspace_path")
