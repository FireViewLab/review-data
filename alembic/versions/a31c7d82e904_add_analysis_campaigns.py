"""모델 버전별 재분석 진행 상태

Revision ID: a31c7d82e904
Revises: f2a83b1d9604
"""
import sqlalchemy as sa
from alembic import op

revision = "a31c7d82e904"
down_revision = "f2a83b1d9604"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "analysis_campaigns",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("target_key", sa.Text(), nullable=False, unique=True),
        sa.Column("model_version", sa.Text(), nullable=False),
        sa.Column("policy_version", sa.Text(), nullable=False),
        sa.Column("sampling_version", sa.Text(), nullable=False),
        sa.Column("max_reviews", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), server_default="running", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "analysis_campaign_items",
        sa.Column("campaign_id", sa.BigInteger(),
                  sa.ForeignKey("analysis_campaigns.id", ondelete="CASCADE"),nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("product_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("analysis_job_id", sa.BigInteger(), sa.ForeignKey("analysis_jobs.id")),
        sa.PrimaryKeyConstraint("campaign_id", "platform", "product_id"),
        sa.ForeignKeyConstraint(["platform", "product_id"],
                                ["products.platform", "products.product_id"], ondelete="CASCADE"),
    )
    op.create_index("idx_campaign_items_status", "analysis_campaign_items", ["campaign_id","status"])


def downgrade():
    op.drop_table("analysis_campaign_items")
    op.drop_table("analysis_campaigns")
