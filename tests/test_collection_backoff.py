"""실패 유형, 연속 실패 대기 및 성공 후 해제의 DB 통합 검증."""

from datetime import UTC, datetime, timedelta

import pytest

from review_data.core.collection_failure import failure_kind, retry_delay
from review_data.core.db.models import CollectionJob, ProductRow
from review_data.core.db.repository import CollectionJobRepository
from review_data.core.exceptions import AccessBlockedError, MissingCredentialError, ParseError
from review_data.core.service.operations import operational_status


@pytest.mark.parametrize(
    "error,kind",
    [
        (AccessBlockedError("접근 불가"), "blocked"),
        (MissingCredentialError("키 미설정"), "credentials"),
        (ParseError("리뷰 구조 변경"), "parse"),
        (TimeoutError(), "timeout"),
        ("product: HTTP 404", "not_found"),
        ("review 저장 실패", "storage"),
    ],
)
def test_classification(error, kind):
    assert failure_kind(error) == kind


def test_backoff_is_bounded_and_zero_disables():
    assert retry_delay("blocked", 1) == 21600
    assert retry_delay("blocked", 2) == 43200
    assert retry_delay("blocked", 100) == 86400
    assert retry_delay("timeout", 1) == 600
    assert retry_delay("blocked", 1, 0) == 0


async def test_scheduler_cooldown_growth_and_success_reset(session):
    now = datetime.now(UTC)
    session.add(
        ProductRow(
            platform="naver",
            product_id="p",
            name="상품",
            url="https://x",
            last_collected_at=now - timedelta(days=3),
        )
    )
    await session.flush()
    repo = CollectionJobRepository(session)

    async def finish(status, kind=None):
        job, _ = await repo.create_or_get_active("naver", "p")
        await session.flush()
        job.status = "running"
        job.locked_by = "worker"
        job.lease_expires_at = now + timedelta(hours=1)
        await session.flush()
        assert await repo.mark_completed(
            job.id,
            worker_id="worker",
            product_status=status,
            review_status=status,
            failure_kind=kind,
        )
        await session.refresh(job)
        return job

    first = await finish("failed", "blocked")
    assert 21500 < (first.retry_after - now).total_seconds() < 21700
    candidates = dict(
        product_cutoff=now, review_cutoff=now, failure_cutoff=now - timedelta(hours=6), limit=10
    )
    assert await repo.list_refresh_candidates(**candidates) == []
    second = await finish("failed", "blocked")
    assert 43100 < (second.retry_after - now).total_seconds() < 43300
    result = await operational_status(session)
    assert result["collection_failure_causes"] == [
        {
            "platform": "naver",
            "failure_kind": "blocked",
            "count": 1,
            "cooling_down": 1,
            "next_retry_at": second.retry_after,
        }
    ]
    success = await finish("succeeded")
    assert success.retry_after is None
    assert success.failure_kind is None
    assert await repo.list_refresh_candidates(**candidates) == [("naver", "p")]
    third = await finish("failed", "blocked")
    assert 21500 < (third.retry_after - now).total_seconds() < 21700


async def test_lost_owner_cannot_replace_failure_or_retry_time(session):
    now = datetime.now(UTC)
    job = CollectionJob(
        platform="naver",
        product_id="p",
        idempotency_key="p",
        status="running",
        locked_by="new-worker",
        lease_expires_at=now + timedelta(hours=1),
    )
    session.add(job)
    await session.flush()
    assert not await CollectionJobRepository(session).mark_completed(
        job.id,
        worker_id="old-worker",
        product_status="failed",
        review_status="failed",
        failure_kind="blocked",
    )
    await session.refresh(job)
    assert job.status == "running"
    assert job.failure_kind is None
    assert job.retry_after is None


async def test_blocked_product_stops_followup_review_request(monkeypatch):
    from unittest.mock import AsyncMock

    from review_data.core.base import BaseCollector
    from review_data.core.settings import Settings
    from review_data.worker.collection_worker import _Claim, _collect

    review_request = AsyncMock()

    class BlockedCollector(BaseCollector):
        platform = "blocked"

        async def search_products(self, keyword, limit=20):
            return []

        async def get_product(self, product_id):
            raise AccessBlockedError("정상 접근 거부")

        get_reviews = review_request

    result = await _collect(
        BlockedCollector, _Claim(1, "blocked", "p", "w"), Settings(_env_file=None)
    )
    assert result.failure_kinds == ["blocked"]
    assert result.reviews is None
    review_request.assert_not_awaited()


async def test_zero_reviews_remains_success_without_cooldown(session_factory, monkeypatch):
    from review_data.core.base import BaseCollector
    from review_data.core.models import Product
    from review_data.worker import collection_worker

    class EmptyCollector(BaseCollector):
        platform = "empty"

        async def search_products(self, keyword, limit=20):
            return []

        async def get_product(self, product_id):
            return Product(
                platform=self.platform, product_id=product_id, name="상품", url="https://x"
            )

        async def get_reviews(self, product_id, limit=50):
            return []

    monkeypatch.setattr(collection_worker, "discover", lambda: ({"empty": EmptyCollector}, []))
    async with session_factory() as s:
        job, _ = await CollectionJobRepository(s).create_or_get_active("empty", "p")
        job_id = job.id
        await s.commit()
    assert await collection_worker.run_once(session_factory, "worker")
    async with session_factory() as s:
        job = await s.get(CollectionJob, job_id)
        assert job.status == "succeeded"
        assert job.failure_kind is None
        assert job.retry_after is None
        product = await s.get(ProductRow, ("empty", "p"))
        assert product.reviews_last_collected_at is not None
