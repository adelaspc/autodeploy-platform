"""add helm runtime metadata

Revision ID: 0f7e8d9c1a2b
Revises: e5f6a7b8c9d0
Create Date: 2026-06-23 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "0f7e8d9c1a2b"
down_revision = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("platform_deployments", schema=None) as batch_op:
        batch_op.add_column(sa.Column("helm_release_name", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("helm_namespace", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("helm_chart_path", sa.String(length=1024), nullable=True))


def downgrade():
    with op.batch_alter_table("platform_deployments", schema=None) as batch_op:
        batch_op.drop_column("helm_chart_path")
        batch_op.drop_column("helm_namespace")
        batch_op.drop_column("helm_release_name")
