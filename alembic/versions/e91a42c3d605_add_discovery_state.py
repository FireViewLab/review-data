"""persist independent product discovery progress

Revision ID: e91a42c3d605
Revises: d80e71a2bc93
"""

import sqlalchemy as sa
from alembic import op

revision = "e91a42c3d605"
down_revision = "d80e71a2bc93"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "discovery_state",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("position", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column(
            "next_run_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("lease_token", sa.Text()),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("last_completed_at", sa.DateTime(timezone=True)),
        sa.Column("last_saved", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_queued", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_failures", sa.Integer(), server_default="0", nullable=False),
    )


def downgrade():
    op.drop_table("discovery_state")
