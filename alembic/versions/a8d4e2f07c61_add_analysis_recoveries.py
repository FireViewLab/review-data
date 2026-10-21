"""실패 분석 복구 기록

Revision ID: a8d4e2f07c61
Revises: f1c83a5e7b29
"""

import sqlalchemy as sa
from alembic import op

revision = "a8d4e2f07c61"
down_revision = "f1c83a5e7b29"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "analysis_recoveries",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("hours", sa.Integer(), nullable=False),
        sa.Column(
            "job_ids", sa.ARRAY(sa.BigInteger()), server_default="{}", nullable=False
        ),
    )


def downgrade():
    op.drop_table("analysis_recoveries")
