"""AI v2 계약(v3.1 모델) 분석 결과 컬럼

Revision ID: b7e3c9d41a20
Revises: a8d4e2f07c61
"""

from alembic import op

revision = "b7e3c9d41a20"
down_revision = "a8d4e2f07c61"
branch_labels = None
depends_on = None


def upgrade():
    # 행이 많아 기본값 없는 컬럼과 상수 기본값만 더해 테이블을 다시 쓰지 않는다. 새 컬럼은 모두
    # 비어 있으므로 제약도 기존 행 검사(NOT VALID) 없이 붙인다.
    op.execute(
        """
        ALTER TABLE review_analyses
            ADD COLUMN policy_index_v3 NUMERIC,
            ADD COLUMN evidence_level_v3 TEXT,
            ADD COLUMN analysis_summary_ko TEXT,
            ADD COLUMN rules_reasons_ko TEXT[] NOT NULL DEFAULT '{}',
            ADD COLUMN evidence_reasons_v3 TEXT[] NOT NULL DEFAULT '{}',
            ADD COLUMN reason_source TEXT
        """
    )
    op.execute(
        """
        ALTER TABLE review_analyses
            ADD CONSTRAINT ck_review_analyses_policy_index_v3
                CHECK (policy_index_v3 BETWEEN 0 AND 100) NOT VALID,
            ADD CONSTRAINT ck_review_analyses_evidence_level_v3
                CHECK (evidence_level_v3 IN ('SAFE','WARN','DANGER')) NOT VALID,
            ADD CONSTRAINT ck_review_analyses_reason_source
                CHECK (reason_source IN ('groq','python','rules')) NOT VALID
        """
    )


def downgrade():
    op.execute(
        """
        ALTER TABLE review_analyses
            DROP COLUMN policy_index_v3,
            DROP COLUMN evidence_level_v3,
            DROP COLUMN analysis_summary_ko,
            DROP COLUMN rules_reasons_ko,
            DROP COLUMN evidence_reasons_v3,
            DROP COLUMN reason_source
        """
    )
