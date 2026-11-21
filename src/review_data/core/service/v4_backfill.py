"""이미 분석된 작업에 v4.0 결과를 채운다.

현재 목표 모델로 완료된 작업 중 v4 완료 시각이 없는 작업을 ID 순서로 훑는다. 작업마다
저장된 분석 행의 리뷰 본문을 v4 서비스로 보내고 같은 행에 결과를 쓴다. 중간에 멈춰도 다시 실행하면
비어 있는 작업부터 이어간다.
"""

import asyncio
import logging
import time

from sqlalchemy import select

from review_data.core.db.models import AnalysisJob, ReviewAnalysisRow, ReviewRow
from review_data.core.settings import Settings
from review_data.worker.analysis_worker import fill_v4

logger = logging.getLogger(__name__)
PAGE = 200


async def pending_jobs(session, model_version: str, after: int, limit: int = PAGE):
    stmt = (
        select(AnalysisJob.id, AnalysisJob.platform, AnalysisJob.product_id)
        .where(
            AnalysisJob.status == "done",
            AnalysisJob.model_version == model_version,
            AnalysisJob.v4_completed_at.is_(None),
            AnalysisJob.id > after,
        )
        .order_by(AnalysisJob.id)
        .limit(limit)
    )
    return (await session.execute(stmt)).all()


async def job_reviews(session, job_id: int) -> list[dict]:
    stmt = (
        select(ReviewAnalysisRow.review_id, ReviewRow.content)
        .join(
            ReviewRow,
            (ReviewRow.platform == ReviewAnalysisRow.platform)
            & (ReviewRow.product_id == ReviewAnalysisRow.product_id)
            & (ReviewRow.review_id == ReviewAnalysisRow.review_id),
        )
        .where(ReviewAnalysisRow.analysis_job_id == job_id)
    )
    return [
        {"review_id": rid, "content": content} for rid, content in (await session.execute(stmt))
    ]


async def run(factory, settings: Settings, *, concurrency: int = 4, max_jobs: int = 0) -> dict:
    if not settings.v4_analyze_url or not settings.ai_model_version:
        raise ValueError("V4_ANALYZE_URL과 AI_MODEL_VERSION이 필요합니다.")
    semaphore = asyncio.Semaphore(concurrency)
    done = filled = failed = 0
    after, start = 0, time.monotonic()

    async def one(job_id, platform, product_id):
        nonlocal done, filled, failed
        async with semaphore:
            async with factory() as session:
                reviews = await job_reviews(session, job_id)
            count = await fill_v4(factory, settings, job_id, platform, product_id, reviews)
            done += 1
            filled += count
            failed += 0 if count or not reviews else 1

    while True:
        async with factory() as session:
            page = await pending_jobs(session, settings.ai_model_version, after)
        if not page:
            break
        if max_jobs:
            page = page[: max(max_jobs - done, 0)]
        await asyncio.gather(*(one(*row) for row in page))
        after = page[-1][0] if page else after
        rate = done / max(time.monotonic() - start, 1e-6)
        logger.info(
            "v4 backfill 작업 %s건 · 리뷰 %s건 · 실패 %s건 · %.1f작업/초",
            done,
            filled,
            failed,
            rate,
        )
        if max_jobs and done >= max_jobs:
            break
    return {"jobs": done, "reviews": filled, "failed_jobs": failed}
