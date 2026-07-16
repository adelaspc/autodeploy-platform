"""add active deployment command key

Revision ID: 2d6f8a1c4b7e
Revises: 18a4c7d2e9f0
"""

from alembic import op
import sqlalchemy as sa


revision = "2d6f8a1c4b7e"
down_revision = "18a4c7d2e9f0"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("deployment_commands") as batch_op:
        batch_op.add_column(sa.Column("active_key", sa.String(length=16), nullable=True))

    op.execute(
        "UPDATE deployment_commands SET active_key = 'active' "
        "WHERE status IN ('pending', 'claimed')"
    )

    with op.batch_alter_table("deployment_commands") as batch_op:
        batch_op.create_unique_constraint(
            "uq_deployment_commands_active_type",
            ["deployment_id", "command_type", "active_key"],
        )


def downgrade():
    with op.batch_alter_table("deployment_commands") as batch_op:
        batch_op.drop_constraint("uq_deployment_commands_active_type", type_="unique")
        batch_op.drop_column("active_key")
