"""add deployment commands

Revision ID: 18a4c7d2e9f0
Revises: 0f7e8d9c1a2b
"""

from alembic import op
import sqlalchemy as sa


revision = "18a4c7d2e9f0"
down_revision = "0f7e8d9c1a2b"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "deployment_commands",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("deployment_id", sa.Integer(), nullable=False),
        sa.Column("command_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("claimed_by", sa.String(length=255), nullable=True),
        sa.Column("claimed_at", sa.DateTime(), nullable=True),
        sa.Column("requested_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["deployment_id"], ["platform_deployments.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_deployment_commands_deployment_id", "deployment_commands", ["deployment_id"])
    op.create_index("ix_deployment_commands_command_type", "deployment_commands", ["command_type"])
    op.create_index("ix_deployment_commands_status", "deployment_commands", ["status"])
    op.create_index("ix_deployment_commands_requested_at", "deployment_commands", ["requested_at"])


def downgrade():
    op.drop_index("ix_deployment_commands_requested_at", table_name="deployment_commands")
    op.drop_index("ix_deployment_commands_status", table_name="deployment_commands")
    op.drop_index("ix_deployment_commands_command_type", table_name="deployment_commands")
    op.drop_index("ix_deployment_commands_deployment_id", table_name="deployment_commands")
    op.drop_table("deployment_commands")
