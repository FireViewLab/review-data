"""add durable analysis input snapshot and retry schedule

Revision ID: d80e71a2bc93
Revises: c6f4a28d901b
Create Date: 2026-10-07
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "d80e71a2bc93"
down_revision = "c6f4a28d901b"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("products", sa.Column("analysis_input_hash", sa.Text(), nullable=True))
    op.add_column("analysis_jobs", sa.Column("input_payload", JSONB(), nullable=True))
    for name in ("input_hash", "model_version", "policy_version"):
        op.add_column("analysis_jobs", sa.Column(name, sa.Text(), nullable=True))
    op.add_column(
        "analysis_jobs",
        sa.Column(
            "available_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )


def downgrade():
    op.drop_column("products", "analysis_input_hash")
    for name in ("available_at", "policy_version", "model_version", "input_hash", "input_payload"):
        op.drop_column("analysis_jobs", name)
