"""
TTL 조회 / job 생성·claim / worker 통합 테스트 (issue #10).

conftest.py 의 engine/session fixture가 로컬 Postgres 접속을 시도하며,
접속 실패 시 이 파일의 테스트는 모두 skip된다.
"""

from datetime import UTC, datetime, timedelta

from review_data.core.base import BaseCollector
from review_data.core.db.models import ProductRow
from review_data.core.db.pipeline_control import set_paused
from review_data.core.db.repository import (
    CollectionJobRepository,
    ProductRepository,
    ReviewRepository,
)
from review_data.core.exceptions import CollectorError
from review_data.core.models import Product, Review
from review_data.core.service.collection import CollectionService
from review_data.worker import collection_worker


async def test_fresh_product_returns_immediately(session):
    product = Product(platform="testplat", product_id="ttl-1", name="상품", url="https://x")
    repo = ProductRepository(session)
    await repo.upsert(product)
    # 상품만 신선해서는 fresh 가 아니다. 리뷰 수집도 성공했어야 한다.
    await repo.mark_reviews_collected("testplat", "ttl-1")

    result = await CollectionService(session).get_or_queue("testplat", "ttl-1")

    assert result.status == "fresh"
    assert result.job is None
    assert result.product is not None
    assert result.product.name == "상품"


async def test_ttl_miss_creates_job_once(session):
    platform, pid = "testplat", "ttl-2"
    old = datetime.now(UTC) - timedelta(days=1)
    session.add(
        ProductRow(
            platform=platform,
            product_id=pid,
            name="오래된상품",
            url="https://x",
            last_collected_at=old,
        )
    )
    await session.flush()

    service = CollectionService(session)
    first = await service.get_or_queue(platform, pid)
    second = await service.get_or_queue(platform, pid)

    assert first.status == "stale"
    assert second.status == "stale"
    assert first.job is not None
    # 동시/반복 요청에도 활성 job은 하나만 생성된다 (idempotency).
    assert first.job.id == second.job.id


async def test_paused_collection_returns_stored_product_without_job(session):
    # 수집을 멈춘 동안 만든 job 은 실행되지 않아, 화면이 '새로 수집 중'을 끝없이 그렸다 (#233)
    platform, pid = "testplat", "paused-1"
    old = datetime.now(UTC) - timedelta(days=1)
    session.add(
        ProductRow(
            platform=platform,
            product_id=pid,
            name="오래된상품",
            url="https://x",
            last_collected_at=old,
        )
    )
    await session.flush()
    await set_paused(session, "collection", True)

    result = await CollectionService(session).get_or_queue(platform, pid)

    assert result.status == "stale"
    assert result.product is not None
    assert result.job is None
    assert await CollectionJobRepository(session).get_active(platform, pid) is None


async def test_paused_collection_still_queues_unknown_product(session):
    # 처음 보는 상품은 보여줄 데이터가 없으므로 job 을 만들어 재개 직후 수집되게 둔다
    await set_paused(session, "collection", True)

    result = await CollectionService(session).get_or_queue("testplat", "paused-new")

    assert result.status == "queued"
    assert result.job is not None


async def test_cold_start_returns_queued_without_product(session):
    result = await CollectionService(session).get_or_queue("testplat", "does-not-exist")

    assert result.status == "queued"
    assert result.product is None
    assert result.job is not None


async def test_claim_reclaims_job_after_lease_expires(session):
    jobs = CollectionJobRepository(session)
    job, created = await jobs.create_or_get_active("testplat", "claim-1")
    assert created is True
    await session.commit()

    claimed = await jobs.claim_one("worker-a", lease_seconds=120)
    assert claimed is not None
    assert claimed.id == job.id
    assert claimed.locked_by == "worker-a"
    await session.commit()

    # 워커 크래시를 흉내낸다: lease를 강제로 과거로 되돌린다.
    claimed.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await session.commit()

    reclaimed = await jobs.claim_one("worker-b", lease_seconds=120)
    assert reclaimed is not None
    assert reclaimed.id == job.id
    assert reclaimed.locked_by == "worker-b"
    await session.commit()


async def test_review_upsert_is_idempotent(session):
    await ProductRepository(session).upsert(
        Product(platform="testplat", product_id="rev-1", name="상품", url="https://x")
    )
    reviews_repo = ReviewRepository(session)
    first = Review(
        platform="testplat", product_id="rev-1", review_id="r1", content="처음", rating=4.0
    )
    await reviews_repo.upsert_many("testplat", "rev-1", [first])

    updated = Review(
        platform="testplat", product_id="rev-1", review_id="r1", content="수정", rating=5.0
    )
    await reviews_repo.upsert_many("testplat", "rev-1", [updated])

    rows, _next_cursor = await reviews_repo.list_page("testplat", "rev-1")
    assert len(rows) == 1
    assert rows[0].content == "수정"
    assert float(rows[0].rating) == 5.0


class _StubCollector(BaseCollector):
    platform = "stubplat"

    async def search_products(self, keyword: str, limit: int = 20) -> list[Product]:
        return []

    async def get_product(self, product_id: str) -> Product:
        return Product(platform=self.platform, product_id=product_id, name="스텁상품", url="https://x")

    async def get_reviews(self, product_id: str, limit: int = 50) -> list[Review]:
        return [
            Review(platform=self.platform, product_id=product_id, review_id="r1", content="좋아요")
        ]


class _ReviewFailingCollector(BaseCollector):
    platform = "partialplat"

    async def search_products(self, keyword: str, limit: int = 20) -> list[Product]:
        return []

    async def get_product(self, product_id: str) -> Product:
        return Product(platform=self.platform, product_id=product_id, name="상품", url="https://x")

    async def get_reviews(self, product_id: str, limit: int = 50) -> list[Review]:
        raise CollectorError("리뷰 수집 실패 시뮬레이션")


async def test_worker_processes_job_end_to_end(session_factory, monkeypatch):
    monkeypatch.setattr(collection_worker, "discover", lambda: ({"stubplat": _StubCollector}, []))

    async with session_factory() as s:
        job, _ = await CollectionJobRepository(s).create_or_get_active("stubplat", "worker-1")
        job_id = job.id
        await s.commit()

    processed = await collection_worker.run_once(session_factory, "worker-test")
    assert processed is True

    async with session_factory() as s:
        refreshed = await CollectionJobRepository(s).get(job_id)
        assert refreshed.status == "succeeded"
        assert refreshed.product_status == "succeeded"
        assert refreshed.review_status == "succeeded"

        product_row = await ProductRepository(s).get("stubplat", "worker-1")
        assert product_row is not None
        assert product_row.name == "스텁상품"


async def test_worker_marks_partial_on_review_failure(session_factory, monkeypatch):
    monkeypatch.setattr(
        collection_worker, "discover", lambda: ({"partialplat": _ReviewFailingCollector}, [])
    )

    async with session_factory() as s:
        job, _ = await CollectionJobRepository(s).create_or_get_active("partialplat", "partial-1")
        job_id = job.id
        await s.commit()

    await collection_worker.run_once(session_factory, "worker-test")

    async with session_factory() as s:
        refreshed = await CollectionJobRepository(s).get(job_id)
        assert refreshed.status == "partial"
        assert refreshed.product_status == "succeeded"
        assert refreshed.review_status == "failed"

        # 상품은 리뷰 실패와 무관하게 커밋되어 있어야 한다.
        assert await ProductRepository(s).get("partialplat", "partial-1") is not None


async def test_worker_returns_false_when_no_job_pending(session_factory):
    processed = await collection_worker.run_once(session_factory, "worker-idle")
    assert processed is False
