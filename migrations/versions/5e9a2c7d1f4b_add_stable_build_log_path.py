"""add stable build log path

Revision ID: 5e9a2c7d1f4b
Revises: 2d6f8a1c4b7e
"""

from alembic import op
import sqlalchemy as sa


revision = "5e9a2c7d1f4b"
down_revision = "2d6f8a1c4b7e"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("builds") as batch_op:
        batch_op.add_column(sa.Column("build_log_path", sa.String(length=1024), nullable=True))


def downgrade():
    with op.batch_alter_table("builds") as batch_op:
        batch_op.drop_column("build_log_path")
