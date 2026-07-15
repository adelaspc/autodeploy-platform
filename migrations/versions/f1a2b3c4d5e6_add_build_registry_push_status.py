"""add build registry push status

Revision ID: f1a2b3c4d5e6
Revises: c4d8e2f1a9b3
Create Date: 2026-05-06 00:00:01.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "f1a2b3c4d5e6"
down_revision = "c4d8e2f1a9b3"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("builds", schema=None) as batch_op:
        batch_op.add_column(sa.Column("registry_push_status", sa.String(length=32), nullable=True))


def downgrade():
    with op.batch_alter_table("builds", schema=None) as batch_op:
        batch_op.drop_column("registry_push_status")
