"""검색 상품 등록과 저장된 상품의 최신 분석 요약을 제공한다."""

import base64
import binascii
import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import exists, func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from review_data.core.db.analysis_repository import (
    AnalysisRepository,
    published_condition,
    published_order,
    sampling_info,
)
from review_data.core.db.models import (
    AnalysisJob,
    CollectionJob,
    ProductRow,
    ReviewAnalysisRow,
    ReviewRow,
)
from review_data.core.db.repository import CollectionJobRepository, ProductRepository
from review_data.core.models import Product
from review_data.core.service.collection import CollectionService
from review_data.core.service.scheduling import _SCHEDULER_LOCK_KEY
from review_data.core.settings import Settings


async def register_search_products(
    session: AsyncSession, platform: str, products: list[Product], settings: Settings
) -> dict:
    if any(product.platform != platform for product in products):
        raise ValueError("검색 결과 플랫폼이 요청과 다릅니다.")
    # 스케줄러와 같은 잠금으로 검색 여러 건이 동시에 큐 상한을 넘기는 것을 막는다.
    await session.execute(select(func.pg_advisory_xact_lock(_SCHEDULER_LOCK_KEY)))
    jobs = CollectionJobRepository(session)
    repo = ProductRepository(session)
    room = max(0, settings.schedule_max_pending - await jobs.count_pending())
    saved = queued = 0
    cutoff = datetime.now(UTC) - timedelta(seconds=settings.schedule_failure_cooldown_seconds)
    for product in products:
        row = await repo.get(platform, product.product_id)
        if row is None:
            inserted = await repo.insert_search_product(product)
            row = await repo.get(platform, product.product_id)
            saved += inserted
        # 검색의 제한된 정보로 기존 상세나 수집 시각을 덮지 않는다.
        if not room or platform in settings.excluded_platforms():
            continue
        if CollectionService(session, settings)._is_fresh(row):
            continue
        failed = await session.scalar(
            select(CollectionJob.id)
            .where(
                CollectionJob.platform == platform,
                CollectionJob.product_id == product.product_id,
                CollectionJob.status.in_(["failed", "partial"]),
                func.coalesce(CollectionJob.completed_at, CollectionJob.updated_at) > cutoff,
            )
            .limit(1)
        )
        if failed is not None:
            continue
        _, created = await jobs.create_or_get_active(
            platform, product.product_id, requested_by="search"
        )
        if created:
            queued += 1
            room -= 1
    return {"saved": saved, "queued": queued}


def encode_catalog_cursor(platform: str, product_id: str) -> str:
    return base64.urlsafe_b64encode(
        json.dumps([platform, product_id], ensure_ascii=False).encode()
    ).decode()


def decode_catalog_cursor(cursor: str) -> tuple[str, str]:
    try:
        if len(cursor) > 2048:
            raise ValueError
        value = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
        if not isinstance(value, list) or len(value) != 2:
            raise ValueError
        if any(not isinstance(part, str) or not part for part in value):
            raise ValueError
        return tuple(value)
    except (ValueError, TypeError, binascii.Error, UnicodeDecodeError) as exc:
        raise ValueError("상품 목록 cursor 값이 올바르지 않습니다.") from exc


async def catalog_page(session: AsyncSession, settings: Settings, limit: int, cursor=None):
    latest = (
        select(AnalysisJob.id)
        .where(
            AnalysisJob.platform == ProductRow.platform,
            AnalysisJob.product_id == ProductRow.product_id,
        )
        .order_by(AnalysisJob.id.desc())
        .limit(1)
        .correlate(ProductRow)
        .scalar_subquery()
    )
    published = (
        select(AnalysisJob.id)
        .where(
            AnalysisJob.platform == ProductRow.platform,
            AnalysisJob.product_id == ProductRow.product_id,
            published_condition(),
        )
        .order_by(*published_order(settings))
        .limit(1)
        .correlate(ProductRow)
        .scalar_subquery()
    )
    latest_job = aliased(AnalysisJob)
    average = (
        select(func.avg(ReviewAnalysisRow.rti))
        .where(ReviewAnalysisRow.analysis_job_id == AnalysisJob.id)
        .correlate(AnalysisJob)
        .scalar_subquery()
    )
    scored = (
        select(func.count(ReviewAnalysisRow.rti))
        .where(ReviewAnalysisRow.analysis_job_id == AnalysisJob.id)
        .correlate(AnalysisJob)
        .scalar_subquery()
    )
    stmt = (
        select(ProductRow, AnalysisJob, average, scored, latest_job.id, latest_job.status)
        .outerjoin(AnalysisJob, AnalysisJob.id == func.coalesce(published, latest))
        .outerjoin(latest_job, latest_job.id == latest)
        .where(
            exists().where(
                ReviewRow.platform == ProductRow.platform,
                ReviewRow.product_id == ProductRow.product_id,
            )
        )
        .order_by(ProductRow.platform, ProductRow.product_id)
        .limit(limit + 1)
    )
    if cursor:
        stmt = stmt.where(
            tuple_(ProductRow.platform, ProductRow.product_id) > decode_catalog_cursor(cursor)
        )
    rows = (await session.execute(stmt)).all()
    items = []
    for product, job, avg, count, latest_id, latest_status in rows[:limit]:
        is_current = bool(
            job
            and product.analysis_input_hash
            and AnalysisRepository._same(job, product.analysis_input_hash, settings)
        )
        if job is None:
            status = "not_analyzed" if settings.ai_analysis_enabled else "disabled"
            sampling = {"sampled": False, "source_review_count": 0, "analyzed_review_count": 0}
        else:
            was_published = job.status == "done" or (
                job.status == "stale" and job.completed_at is not None and job.result is not None
            )
            status = "done" if was_published else (job.status if is_current else "stale")
            sampling = sampling_info(job)
        items.append(
            (
                product,
                {
                    "status": status,
                    "is_current": is_current,
                    "refresh_job": {"id": latest_id, "status": latest_status}
                    if job and latest_id != job.id
                    else None,
                    "target_model_version": settings.ai_model_version,
                    "target_policy_version": settings.ai_policy_version,
                    "job_id": job.id if job else None,
                    "avg_rti": float(avg) if status == "done" and avg is not None else None,
                    "scored_review_count": count if status == "done" else 0,
                    "review_count": sampling["analyzed_review_count"],
                    "source_review_count": sampling["source_review_count"],
                    "sampled": sampling["sampled"],
                    "model_version": job.model_version if job else None,
                    "policy_version": job.policy_version if job else None,
                },
            )
        )
    next_cursor = None
    if len(rows) > limit:
        last = rows[limit - 1][0]
        next_cursor = encode_catalog_cursor(last.platform, last.product_id)
    return items, next_cursor


async def products_with_reviews(
    session: AsyncSession, platform: str, products: list[Product]
) -> list[Product]:
    if not products:
        return []
    available = set(
        await session.scalars(
            select(ReviewRow.product_id)
            .where(
                ReviewRow.platform == platform,
                ReviewRow.product_id.in_([p.product_id for p in products]),
            )
            .distinct()
        )
    )
    return [p for p in products if p.product_id in available]
