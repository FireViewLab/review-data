"""분석 PC 지표

Revision ID: c4d1e8a3f572
Revises: b7e3c9d41a20
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "c4d1e8a3f572"
down_revision = "b7e3c9d41a20"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "analysis_nodes",
        sa.Column("node_id", sa.Text(), primary_key=True),
        sa.Column("reported_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("metrics", JSONB(), nullable=False),
    )


def downgrade():
    op.drop_table("analysis_nodes")
