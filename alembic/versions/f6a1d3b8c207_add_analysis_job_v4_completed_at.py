"""분석 작업별 v4 결과 완료 시각

Revision ID: f6a1d3b8c207
Revises: e3b7c9d15a42
"""

import sqlalchemy as sa
from alembic import op

revision = "f6a1d3b8c207"
down_revision = "e3b7c9d15a42"
branch_labels = None
depends_on = None


def upgrade():
    # 기본값 없는 nullable 컬럼이라 즉시 끝난다. 이미 채운 작업의 표시는 운영에서 나눠서 찍는다.
    op.add_column(
        "analysis_jobs", sa.Column("v4_completed_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade():
    op.drop_column("analysis_jobs", "v4_completed_at")
