"""add deployment preflight fields

Revision ID: d4e5f6a7b8c9
Revises: b2c4d6e8f0a1
Create Date: 2026-05-12 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "d4e5f6a7b8c9"
down_revision = "b2c4d6e8f0a1"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("platform_deployments", schema=None) as batch_op:
        batch_op.add_column(sa.Column("preflight_status", sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column("preflight_summary", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("preflight_metadata_json", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("preflight_completed_at", sa.DateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table("platform_deployments", schema=None) as batch_op:
        batch_op.drop_column("preflight_completed_at")
        batch_op.drop_column("preflight_metadata_json")
        batch_op.drop_column("preflight_summary")
        batch_op.drop_column("preflight_status")
