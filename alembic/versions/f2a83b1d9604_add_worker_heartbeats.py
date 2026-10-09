"""운영 워커 heartbeat

Revision ID: f2a83b1d9604
Revises: e91a42c3d605
"""
import sqlalchemy as sa
from alembic import op

revision = "f2a83b1d9604"
down_revision = "e91a42c3d605"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "worker_heartbeats",
        sa.Column("worker_id", sa.Text(), primary_key=True),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("stopped_at", sa.DateTime(timezone=True)),
    )


def downgrade():
    op.drop_table("worker_heartbeats")
