"""backfill missing platform deployment columns

Revision ID: 9b2f7e6c4a11
Revises: 7c3eb9a6f1b2
Create Date: 2026-05-02 23:10:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "9b2f7e6c4a11"
down_revision = "7c3eb9a6f1b2"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_columns = {column["name"] for column in inspector.get_columns("platform_deployments")}

    missing_columns = [
        ("container_name", sa.Column("container_name", sa.String(length=255), nullable=True)),
        ("container_id", sa.Column("container_id", sa.String(length=255), nullable=True)),
        ("host_port", sa.Column("host_port", sa.Integer(), nullable=True)),
        ("healthcheck_url", sa.Column("healthcheck_url", sa.String(length=1024), nullable=True)),
        ("claimed_at", sa.Column("claimed_at", sa.DateTime(), nullable=True)),
        ("claimed_by", sa.Column("claimed_by", sa.String(length=255), nullable=True)),
    ]

    with op.batch_alter_table("platform_deployments") as batch_op:
        for name, column in missing_columns:
            if name not in existing_columns:
                batch_op.add_column(column)


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_columns = {column["name"] for column in inspector.get_columns("platform_deployments")}

    with op.batch_alter_table("platform_deployments") as batch_op:
        for name in ["claimed_by", "claimed_at", "healthcheck_url", "host_port", "container_id", "container_name"]:
            if name in existing_columns:
                batch_op.drop_column(name)
