"""v4.0 텍스트 모델 결과

Revision ID: e3b7c9d15a42
Revises: d8f2a6c41e93
"""

import sqlalchemy as sa
from alembic import op

revision = "e3b7c9d15a42"
down_revision = "d8f2a6c41e93"
branch_labels = None
depends_on = None

COLUMNS = (
    ("text_probability_v4", sa.Numeric()),
    ("text_score_v4", sa.Numeric()),
    ("policy_index_v4", sa.Numeric()),
    ("level_v4", sa.Text()),
    ("model_version_v4", sa.Text()),
    ("policy_version_v4", sa.Text()),
)


def upgrade():
    # 기본값 없는 nullable 컬럼이라 즉시 끝난다. 제약은 NOT VALID로 걸어 기존 행 검사를 건너뛴다.
    for name, type_ in COLUMNS:
        op.add_column("review_analyses", sa.Column(name, type_, nullable=True))
    op.execute(
        "ALTER TABLE review_analyses ADD CONSTRAINT ck_review_analyses_level_v4 "
        "CHECK (level_v4 IN ('SAFE','WARN','DANGER')) NOT VALID"
    )
    op.execute(
        "ALTER TABLE review_analyses ADD CONSTRAINT ck_review_analyses_policy_index_v4 "
        "CHECK (policy_index_v4 BETWEEN 0 AND 100) NOT VALID"
    )
    op.execute(
        "ALTER TABLE review_analyses ADD CONSTRAINT ck_review_analyses_text_probability_v4 "
        "CHECK (text_probability_v4 BETWEEN 0 AND 1) NOT VALID"
    )


def downgrade():
    for name in (
        "ck_review_analyses_text_probability_v4",
        "ck_review_analyses_policy_index_v4",
        "ck_review_analyses_level_v4",
    ):
        op.drop_constraint(name, "review_analyses")
    for name, _ in reversed(COLUMNS):
        op.drop_column("review_analyses", name)
