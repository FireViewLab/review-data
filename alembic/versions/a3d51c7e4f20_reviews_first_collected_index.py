"""리뷰 최초 수집 시각 인덱스

Revision ID: a3d51c7e4f20
Revises: c79fa18d2e60
"""

from alembic import op

revision = "a3d51c7e4f20"
down_revision = "c79fa18d2e60"
branch_labels = None
depends_on = None

INDEX = "idx_reviews_first_collected_at"


def upgrade():
    # 수집 워커가 계속 쓰는 테이블이라 쓰기를 막지 않도록 CONCURRENTLY로 만든다.
    # 트랜잭션 안에서는 실행할 수 없고, 중간에 실패하면 INVALID 인덱스가 남으므로 먼저 지운다.
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}")
        op.create_index(INDEX, "reviews", ["first_collected_at"], postgresql_concurrently=True)


def downgrade():
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}")
