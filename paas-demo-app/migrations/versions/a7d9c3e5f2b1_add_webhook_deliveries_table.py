"""add webhook deliveries table

Revision ID: a7d9c3e5f2b1
Revises: f1a2b3c4d5e6
Create Date: 2026-05-07 00:00:01.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "a7d9c3e5f2b1"
down_revision = "f1a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "webhook_deliveries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("delivery_id", sa.String(length=255), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("repository_url", sa.String(length=255), nullable=True),
        sa.Column("branch", sa.String(length=255), nullable=True),
        sa.Column("commit_sha", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=True),
        sa.Column("deployment_id", sa.Integer(), nullable=True),
        sa.Column("received_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["deployment_id"], ["platform_deployments.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("delivery_id"),
    )
    with op.batch_alter_table("webhook_deliveries", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_webhook_deliveries_delivery_id"), ["delivery_id"], unique=True)
        batch_op.create_index(batch_op.f("ix_webhook_deliveries_deployment_id"), ["deployment_id"], unique=False)


def downgrade():
    with op.batch_alter_table("webhook_deliveries", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_webhook_deliveries_deployment_id"))
        batch_op.drop_index(batch_op.f("ix_webhook_deliveries_delivery_id"))

    op.drop_table("webhook_deliveries")
