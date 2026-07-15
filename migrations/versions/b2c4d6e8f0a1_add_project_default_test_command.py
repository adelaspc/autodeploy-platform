"""add project default test command

Revision ID: b2c4d6e8f0a1
Revises: a7d9c3e5f2b1
Create Date: 2026-05-09 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "b2c4d6e8f0a1"
down_revision = "a7d9c3e5f2b1"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.add_column(sa.Column("default_test_command", sa.String(length=255), nullable=True))


def downgrade():
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.drop_column("default_test_command")
