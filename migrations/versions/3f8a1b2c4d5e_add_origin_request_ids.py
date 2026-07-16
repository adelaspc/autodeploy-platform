"""add origin request ids

Revision ID: 3f8a1b2c4d5e
Revises: 8a3d6e1f5c9b
"""

from alembic import op
import sqlalchemy as sa


revision = "3f8a1b2c4d5e"
down_revision = "8a3d6e1f5c9b"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("platform_deployments") as batch_op:
        batch_op.add_column(sa.Column("origin_request_id", sa.String(length=128), nullable=True))
        batch_op.create_index("ix_platform_deployments_origin_request_id", ["origin_request_id"], unique=False)

    with op.batch_alter_table("deployment_commands") as batch_op:
        batch_op.add_column(sa.Column("origin_request_id", sa.String(length=128), nullable=True))
        batch_op.create_index("ix_deployment_commands_origin_request_id", ["origin_request_id"], unique=False)


def downgrade():
    with op.batch_alter_table("deployment_commands") as batch_op:
        batch_op.drop_index("ix_deployment_commands_origin_request_id")
        batch_op.drop_column("origin_request_id")

    with op.batch_alter_table("platform_deployments") as batch_op:
        batch_op.drop_index("ix_platform_deployments_origin_request_id")
        batch_op.drop_column("origin_request_id")
