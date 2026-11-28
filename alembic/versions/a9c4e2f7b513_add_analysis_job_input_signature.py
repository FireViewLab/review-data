"""분석 작업 입력 서명(버전 비교용)

Revision ID: a9c4e2f7b513
Revises: f6a1d3b8c207
"""

import sqlalchemy as sa

from alembic import op

revision = "a9c4e2f7b513"
down_revision = "f6a1d3b8c207"
branch_labels = None
depends_on = None


def upgrade():
    # 기본값 없는 nullable 컬럼이라 즉시 끝난다.
    # 옛 작업은 analysis-fill-input-signature로 나눠 채운다.
    op.add_column("analysis_jobs", sa.Column("input_signature", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("analysis_jobs", "input_signature")
