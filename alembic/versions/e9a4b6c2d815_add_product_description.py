"""상품 소개 텍스트와 이미지 주소 추가

Revision ID: e9a4b6c2d815
Revises: b7e2c94d1a03
"""

import sqlalchemy as sa
from alembic import op

revision = "e9a4b6c2d815"
down_revision = "b7e2c94d1a03"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("products", sa.Column("description_text", sa.Text(), nullable=True))
    op.add_column(
        "products",
        sa.Column("description_images", sa.ARRAY(sa.Text()), server_default="{}", nullable=False),
    )


def downgrade():
    op.drop_column("products", "description_images")
    op.drop_column("products", "description_text")
