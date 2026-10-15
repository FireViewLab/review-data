"""발굴 검색어 확장에 맞춰 순환 위치 초기화

Revision ID: b7e2c94d1a03
Revises: a3d51c7e4f20
"""

from alembic import op

revision = "b7e2c94d1a03"
down_revision = "a3d51c7e4f20"
branch_labels = None
depends_on = None


def upgrade():
    # 저장된 위치는 이전 목록(플랫폼당 16개) 기준 누적값이다. 새 목록 길이로 나누면 회차와
    # 다음 검색어가 어긋나므로 처음부터 다시 돈다. 새 검색어가 목록 앞에 있다.
    op.execute("UPDATE discovery_state SET position = 0 WHERE key = 'keyword-discovery-v1'")


def downgrade():
    # 이전 위치는 복원할 수 없다. 처음부터 다시 도는 것은 이전 코드에서도 안전하다.
    pass
