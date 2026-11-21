"""재수집 때 빠진 리뷰 표시

Revision ID: d8f2a6c41e93
Revises: c4d1e8a3f572
"""

import sqlalchemy as sa
from alembic import op

revision = "d8f2a6c41e93"
down_revision = "c4d1e8a3f572"
branch_labels = None
depends_on = None


def upgrade():
    # 기본값 없는 nullable 컬럼이라 대용량 테이블에서도 즉시 끝난다.
    op.add_column("reviews", sa.Column("missing_since", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    op.drop_column("reviews", "missing_since")
