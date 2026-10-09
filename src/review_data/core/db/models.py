"""
PostgreSQL 테이블 정의 (ORM).

⚠️ core/models.py 의 Pydantic Product/Review(팀 계약)와는 별개입니다.
   여기는 DB 저장 형태만 표현하고, service/repository 계층이 Pydantic ↔ ORM 을
   변환합니다.
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    ARRAY,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Numeric,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from review_data.core.db.base import Base


class ProductRow(Base):
    __tablename__ = "products"
    __table_args__ = (PrimaryKeyConstraint("platform", "product_id"),)

    platform: Mapped[str] = mapped_column(Text)
    product_id: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    brand: Mapped[str | None] = mapped_column(Text)
    manufacturer: Mapped[str | None] = mapped_column(Text)
    seller: Mapped[str | None] = mapped_column(Text)
    price: Mapped[int | None]
    thumbnail_url: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(Text)
    review_count: Mapped[int | None]
    rating: Mapped[float | None] = mapped_column(Numeric(3, 2))
    raw_payload: Mapped[dict | None] = mapped_column(JSONB)
    first_collected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_collected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    analysis_input_hash: Mapped[str | None] = mapped_column(Text)

    # 이 상품의 리뷰를 마지막으로 '성공적으로' 수집한 시각. 리뷰 수집이 실패하면
    # 갱신하지 않으므로, 상품만 성공한 부분 실패를 신선하다고 오판하지 않는다.
    # 리뷰가 실제로 0건이어도 수집 자체가 성공했으면 갱신한다.
    reviews_last_collected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ReviewRow(Base):
    __tablename__ = "reviews"
    __table_args__ = (
        PrimaryKeyConstraint("platform", "product_id", "review_id"),
        ForeignKeyConstraint(
            ["platform", "product_id"],
            ["products.platform", "products.product_id"],
            ondelete="CASCADE",
        ),
        # 조회 정렬(written_at DESC NULLS LAST, review_id DESC)과 정확히 맞춘다.
        # 방향이 어긋나면 같은 시각의 리뷰가 많은 상품에서 별도 정렬 비용이 붙는다.
        Index(
            "idx_reviews_cursor",
            "platform",
            "product_id",
            text("written_at DESC NULLS LAST"),
            text("review_id DESC"),
        ),
    )

    platform: Mapped[str] = mapped_column(Text)
    product_id: Mapped[str] = mapped_column(Text)
    review_id: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    rating: Mapped[float | None] = mapped_column(Numeric(3, 2))
    author: Mapped[str | None] = mapped_column(Text)
    written_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    option: Mapped[str | None] = mapped_column("option", Text)
    images: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default="{}")
    helpful_count: Mapped[int | None]
    raw_payload: Mapped[dict | None] = mapped_column(JSONB)
    first_collected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_collected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class CollectionJob(Base):
    """수집 job. product/review 결과는 하나의 row 안에서 서브 상태로 구분한다(부분 실패 표현)."""

    __tablename__ = "collection_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','running','succeeded','partial','failed')",
            name="ck_collection_jobs_status",
        ),
        CheckConstraint(
            "product_status IN ('pending','succeeded','failed','skipped')",
            name="ck_collection_jobs_product_status",
        ),
        CheckConstraint(
            "review_status IN ('pending','succeeded','failed','skipped')",
            name="ck_collection_jobs_review_status",
        ),
        # 동시 요청에도 활성 job 은 하나만 존재 (idempotency)
        Index(
            "uq_collection_jobs_inflight",
            "platform",
            "product_id",
            unique=True,
            postgresql_where=text("status IN ('pending', 'running')"),
        ),
        # claim 쿼리(FOR UPDATE SKIP LOCKED) 지원
        Index("idx_collection_jobs_claim", "status", "lease_expires_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    platform: Mapped[str] = mapped_column(Text)
    product_id: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default="pending")
    product_status: Mapped[str] = mapped_column(Text, server_default="pending")
    review_status: Mapped[str] = mapped_column(Text, server_default="pending")
    idempotency_key: Mapped[str] = mapped_column(Text)
    attempt_count: Mapped[int] = mapped_column(server_default="0")
    max_attempts: Mapped[int] = mapped_column(server_default="3")
    locked_by: Mapped[str | None] = mapped_column(Text)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    requested_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DiscoveryState(Base):
    """독립 발굴의 순환 위치와 네트워크 작업 lease를 재시작 사이에 보존한다."""

    __tablename__ = "discovery_state"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    position: Mapped[int] = mapped_column(BigInteger, server_default="0")
    next_run_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    lease_token: Mapped[str | None] = mapped_column(Text)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_saved: Mapped[int] = mapped_column(server_default="0")
    last_queued: Mapped[int] = mapped_column(server_default="0")
    last_failures: Mapped[int] = mapped_column(server_default="0")


class AnalysisJob(Base):
    """AI 분석 job. collection_jobs 와 별도 테이블 — 리뷰 갱신 시 기존 done row 를 stale 로
    마크하고 새 row 를 queued 로 추가하는 방식으로 재분석을 표현한다."""

    __tablename__ = "analysis_jobs"
    __table_args__ = (
        UniqueConstraint("id", "platform", "product_id", name="uq_analysis_jobs_identity"),
        CheckConstraint(
            "status IN ('queued','running','done','failed','stale')",
            name="ck_analysis_jobs_status",
        ),
        ForeignKeyConstraint(
            ["platform", "product_id"],
            ["products.platform", "products.product_id"],
            ondelete="CASCADE",
        ),
        Index(
            "uq_analysis_jobs_inflight",
            "platform",
            "product_id",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
        Index("idx_analysis_jobs_claim", "status", "lease_expires_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    platform: Mapped[str] = mapped_column(Text)
    product_id: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default="queued")
    input_payload: Mapped[dict | None] = mapped_column(JSONB)
    input_hash: Mapped[str | None] = mapped_column(Text)
    model_version: Mapped[str | None] = mapped_column(Text)
    policy_version: Mapped[str | None] = mapped_column(Text)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    trigger_collection_job_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("collection_jobs.id")
    )
    input_review_count: Mapped[int | None]
    result: Mapped[dict | None] = mapped_column(JSONB)
    last_error: Mapped[str | None] = mapped_column(Text)
    attempt_count: Mapped[int] = mapped_column(server_default="0")
    max_attempts: Mapped[int] = mapped_column(server_default="3")
    locked_by: Mapped[str | None] = mapped_column(Text)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ReviewAnalysisRow(Base):
    """리뷰별 AI 결과. 분석 실행마다 보존하며 근거 부족은 NULL로 저장한다."""

    __tablename__ = "review_analyses"
    __table_args__ = (
        PrimaryKeyConstraint("analysis_job_id", "review_id"),
        ForeignKeyConstraint(
            ["analysis_job_id", "platform", "product_id"],
            ["analysis_jobs.id", "analysis_jobs.platform", "analysis_jobs.product_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["platform", "product_id", "review_id"],
            ["reviews.platform", "reviews.product_id", "reviews.review_id"],
            ondelete="CASCADE",
        ),
        CheckConstraint("rti BETWEEN 0 AND 100", name="ck_review_analyses_rti"),
        CheckConstraint("level IN ('safe','warn','danger')", name="ck_review_analyses_level"),
        CheckConstraint("array_position(reasons, NULL) IS NULL", name="ck_review_analyses_reasons"),
        Index(
            "idx_review_analyses_review",
            "platform",
            "product_id",
            "review_id",
            "analysis_job_id",
        ),
    )

    analysis_job_id: Mapped[int] = mapped_column(BigInteger)
    platform: Mapped[str] = mapped_column(Text)
    product_id: Mapped[str] = mapped_column(Text)
    review_id: Mapped[str] = mapped_column(Text)
    rti: Mapped[Decimal | None] = mapped_column(Numeric)
    level: Mapped[str | None] = mapped_column(Text)
    text_score: Mapped[Decimal | None] = mapped_column(Numeric)
    behavior_score: Mapped[Decimal | None] = mapped_column(Numeric)
    network_score: Mapped[Decimal | None] = mapped_column(Numeric)
    reasons: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default="{}")
    model_version: Mapped[str | None] = mapped_column(Text)
    input_hash: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"

    worker_id: Mapped[str] = mapped_column(Text, primary_key=True)
    role: Mapped[str] = mapped_column(Text)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
