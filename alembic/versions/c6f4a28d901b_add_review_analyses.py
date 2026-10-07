"""add per-review analysis results

Revision ID: c6f4a28d901b
Revises: b1cf29089ede
Create Date: 2026-10-06
"""

import sqlalchemy as sa

from alembic import op

revision = "c6f4a28d901b"
down_revision = "b1cf29089ede"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_analysis_jobs_identity", "analysis_jobs", ["id", "platform", "product_id"]
    )
    op.create_table(
        "review_analyses",
        sa.Column("analysis_job_id", sa.BigInteger(), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("product_id", sa.Text(), nullable=False),
        sa.Column("review_id", sa.Text(), nullable=False),
        sa.Column("rti", sa.Numeric(), nullable=True),
        sa.Column("level", sa.Text(), nullable=True),
        sa.Column("text_score", sa.Numeric(), nullable=True),
        sa.Column("behavior_score", sa.Numeric(), nullable=True),
        sa.Column("network_score", sa.Numeric(), nullable=True),
        sa.Column("reasons", sa.ARRAY(sa.Text()), server_default="{}", nullable=False),
        sa.Column("model_version", sa.Text(), nullable=True),
        sa.Column("input_hash", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("analysis_job_id", "review_id"),
        sa.ForeignKeyConstraint(
            ["analysis_job_id", "platform", "product_id"],
            ["analysis_jobs.id", "analysis_jobs.platform", "analysis_jobs.product_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["platform", "product_id", "review_id"],
            ["reviews.platform", "reviews.product_id", "reviews.review_id"],
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("rti BETWEEN 0 AND 100", name="ck_review_analyses_rti"),
        sa.CheckConstraint("level IN ('safe','warn','danger')", name="ck_review_analyses_level"),
        sa.CheckConstraint(
            "array_position(reasons, NULL) IS NULL", name="ck_review_analyses_reasons"
        ),
    )
    op.create_index(
        "idx_review_analyses_review",
        "review_analyses",
        ["platform", "product_id", "review_id", "analysis_job_id"],
    )


def downgrade() -> None:
    op.drop_table("review_analyses")
    op.drop_constraint("uq_analysis_jobs_identity", "analysis_jobs", type_="unique")
