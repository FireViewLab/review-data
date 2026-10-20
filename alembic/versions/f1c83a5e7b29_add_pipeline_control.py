"""운영 제어(수집·AI 분석 요청 중지) 상태

Revision ID: f1c83a5e7b29
Revises: e9a4b6c2d815
"""

from alembic import op

revision = "f1c83a5e7b29"
down_revision = "e9a4b6c2d815"
branch_labels = None
depends_on = None


def upgrade():
    # 장애 중 배포할 때 워커가 뜨자마자 요청을 보내지 않도록 운영에서 이 표를 먼저 만들고
    # 중지 상태를 넣어 둘 수 있다. 그 경우 기존 행을 그대로 둔다.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS pipeline_control (
            id SMALLINT DEFAULT 1 NOT NULL,
            collection_paused BOOLEAN DEFAULT false NOT NULL,
            analysis_paused BOOLEAN DEFAULT false NOT NULL,
            collection_updated_at TIMESTAMP WITH TIME ZONE,
            analysis_updated_at TIMESTAMP WITH TIME ZONE,
            CONSTRAINT pipeline_control_pkey PRIMARY KEY (id),
            CONSTRAINT pipeline_control_single_row CHECK (id = 1)
        )
        """
    )
    op.execute("INSERT INTO pipeline_control (id) VALUES (1) ON CONFLICT (id) DO NOTHING")


def downgrade():
    op.execute("DROP TABLE IF EXISTS pipeline_control")
