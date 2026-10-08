"""검색 등록과 분석 요약이 실제 저장 상태를 정확히 반영하는지 확인한다."""

import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select, update

from review_data.core.db.analysis_repository import AnalysisRepository
from review_data.core.db.models import (
    AnalysisJob,
    CollectionJob,
    ProductRow,
    ReviewAnalysisRow,
    ReviewRow,
)
from review_data.core.db.repository import ProductRepository
from review_data.core.models import Product
from review_data.core.service.catalog import catalog_page, register_search_products
from review_data.core.settings import Settings


def config(**values):
    return Settings(
        _env_file=None,
        ai_analysis_enabled=True,
        ai_stream_url="http://test.invalid/stream",
        **values,
    )


def products(*ids):
    return [Product(platform="kurly", product_id=i, name=f"검색 {i}", url="https://x") for i in ids]


async def test_search_registers_and_bounds_queue_without_overwriting_details(session_factory):
    async with session_factory() as session:
        result = await register_search_products(
            session, "kurly", products("a", "b"), config(schedule_max_pending=1)
        )
        await session.commit()
        assert result == {"saved": 2, "queued": 1}
        assert await session.scalar(select(func.count()).select_from(ProductRow)) == 2
        assert await session.scalar(select(func.count()).select_from(CollectionJob)) == 1
        row = await session.get(ProductRow, ("kurly", "a"))
        row.name = "상세 상품명"
        await session.commit()
        assert await register_search_products(
            session, "kurly", products("a", "b"), config(schedule_max_pending=1)
        ) == {"saved": 0, "queued": 0}
        await session.commit()
        await session.refresh(row)
        assert row.name == "상세 상품명"


async def test_fresh_excluded_and_recent_failed_searches_do_not_requeue(session_factory):
    async with session_factory() as session:
        repo = ProductRepository(session)
        for p in products("fresh", "failed", "excluded"):
            await repo.upsert(p)
        await repo.mark_reviews_collected("kurly", "fresh")
        session.add(
            CollectionJob(
                platform="kurly",
                product_id="failed",
                requested_by="search",
                idempotency_key="failed-search",
                status="failed",
                completed_at=datetime.now(UTC),
            )
        )
        await session.commit()
        assert (
            await register_search_products(session, "kurly", products("fresh", "failed"), config())
        )["queued"] == 0
        assert (
            await register_search_products(
                session, "kurly", products("excluded"), config(schedule_excluded_platforms="kurly")
            )
        )["queued"] == 0
        await session.commit()
        assert await session.scalar(select(func.count()).select_from(CollectionJob)) == 1


async def test_search_platform_mismatch_is_not_saved(session_factory):
    async with session_factory() as session:
        with pytest.raises(ValueError):
            await register_search_products(session, "musinsa", products("x"), config())
        assert await session.scalar(select(func.count()).select_from(ProductRow)) == 0


async def test_parallel_searches_share_queue_capacity(session_factory):
    async def register(product_id):
        async with session_factory() as session:
            result = await register_search_products(
                session, "kurly", products(product_id), config(schedule_max_pending=1)
            )
            await session.commit()
            return result

    results = await asyncio.gather(register("a"), register("b"))
    assert sum(r["saved"] for r in results) == 2
    assert sum(r["queued"] for r in results) == 1


async def make_analysis(session, product_id, scores, settings):
    await ProductRepository(session).upsert(products(product_id)[0])
    session.add_all(
        [
            ReviewRow(platform="kurly", product_id=product_id, review_id=f"r{i}", content="리뷰")
            for i in range(len(scores))
        ]
    )
    await session.flush()
    job_id = await AnalysisRepository(session).enqueue("kurly", product_id, settings)
    job = await session.get(AnalysisJob, job_id)
    job.status = "done"
    session.add_all(
        [
            ReviewAnalysisRow(
                analysis_job_id=job_id,
                platform="kurly",
                product_id=product_id,
                review_id=f"r{i}",
                rti=score,
            )
            for i, score in enumerate(scores)
            if f"r{i}" in {r["review_id"] for r in job.input_payload["reviews"]}
        ]
    )
    await session.commit()
    return job


async def test_catalog_uses_latest_real_scores_and_nulls_with_keyset_pages(session_factory):
    settings = config()
    async with session_factory() as session:
        await make_analysis(session, "a", [0, 100, None], settings)
        await make_analysis(session, "b", [None], settings)
        await make_analysis(session, "c", [0], settings)
        first, cursor = await catalog_page(session, settings, 2)
        assert [row.product_id for row, _ in first] == ["a", "b"]
        assert first[0][1]["avg_rti"] == 50
        assert first[0][1]["scored_review_count"] == 2
        assert first[0][1]["source_review_count"] == 3
        assert first[1][1]["status"] == "done"
        assert first[1][1]["avg_rti"] is None
        second, following = await catalog_page(session, settings, 2, cursor)
        assert [row.product_id for row, _ in second] == ["c"]
        assert second[0][1]["avg_rti"] == 0
        assert following is None


async def test_catalog_hides_stale_and_version_mismatched_scores(session_factory):
    async with session_factory() as session:
        job = await make_analysis(session, "a", [80], config())
        page, _ = await catalog_page(session, config(ai_model_version="new"), 10)
        assert page[0][1]["status"] == "stale" and page[0][1]["avg_rti"] is None
        await session.execute(update(ProductRow).values(analysis_input_hash=None))
        await session.commit()
        page, _ = await catalog_page(session, config(), 10)
        assert page[0][1]["status"] == "stale" and page[0][1]["scored_review_count"] == 0
        job.status = "failed"
        await session.commit()


async def test_catalog_reports_sample_scope(session_factory):
    settings = config(ai_max_reviews=1)
    async with session_factory() as session:
        await make_analysis(session, "a", [60, None], settings)
        page, _ = await catalog_page(session, settings, 10)
        summary = page[0][1]
        assert summary["sampled"] is True
        assert summary["review_count"] == 1
        assert summary["source_review_count"] == 2


async def test_catalog_never_reuses_old_scores_during_reanalysis(session_factory):
    async with session_factory() as session:
        old = await make_analysis(session, "a", [85], config())
        new = await AnalysisRepository(session).enqueue("kurly", "a", config(), force=True)
        await session.commit()
        page, _ = await catalog_page(session, config(), 10)
        summary = page[0][1]
        assert new != old.id and summary["job_id"] == new
        assert summary["status"] == "queued"
        assert summary["avg_rti"] is None and summary["scored_review_count"] == 0


@pytest.mark.parametrize(
    "cursor", ["invalid", "W10=", "bnVsbA==", "WyJhIl0=", "WzEsMl0=", "x" * 2049]
)
async def test_catalog_rejects_invalid_cursors(session_factory, cursor):
    async with session_factory() as session:
        with pytest.raises(ValueError):
            await catalog_page(session, config(), 10, cursor)
