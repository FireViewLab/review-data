"""수집 실패 분류 및 정기 재예약 대기

Revision ID: c79fa18d2e60
Revises: b42d8e93fa15
"""

import sqlalchemy as sa
from alembic import op

revision = "c79fa18d2e60"
down_revision = "b42d8e93fa15"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("collection_jobs", sa.Column("failure_kind", sa.Text(), nullable=True))
    op.add_column(
        "collection_jobs", sa.Column("retry_after", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index(
        "idx_collection_jobs_product_history", "collection_jobs", ["platform", "product_id", "id"]
    )


def downgrade():
    op.drop_index("idx_collection_jobs_product_history", table_name="collection_jobs")
    op.drop_column("collection_jobs", "retry_after")
    op.drop_column("collection_jobs", "failure_kind")
