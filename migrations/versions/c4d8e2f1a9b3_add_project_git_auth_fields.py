"""add project git auth fields

Revision ID: c4d8e2f1a9b3
Revises: 9b2f7e6c4a11
Create Date: 2026-05-06 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "c4d8e2f1a9b3"
down_revision = "9b2f7e6c4a11"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.add_column(sa.Column("git_auth_type", sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column("git_secret_ref", sa.String(length=120), nullable=True))

    op.execute("UPDATE projects SET git_auth_type = 'none' WHERE git_auth_type IS NULL")

    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.alter_column("git_auth_type", existing_type=sa.String(length=32), nullable=False)


def downgrade():
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.drop_column("git_secret_ref")
        batch_op.drop_column("git_auth_type")
