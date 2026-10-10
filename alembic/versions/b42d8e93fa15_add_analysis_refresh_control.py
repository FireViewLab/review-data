"""대시보드 분석 목표 공유

Revision ID: b42d8e93fa15
Revises: a31c7d82e904
"""

import sqlalchemy as sa
from alembic import op

revision = "b42d8e93fa15"
down_revision = "a31c7d82e904"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "analysis_refresh_control",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("model_version", sa.Text(), nullable=False),
        sa.Column("policy_version", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("id = 1", name="single_analysis_refresh_control"),
    )


def downgrade():
    op.drop_table("analysis_refresh_control")
