"""수집 스케줄러 통합 테스트 (issue #50).

핵심은 두 가지다. 낡은 상품은 조회가 없어도 다시 수집되도록 예약돼야 하고, 그 과정에서
큐를 넘치게 하거나 실패한 상품을 계속 두드려서는 안 된다.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from review_data.core.db.models import CollectionJob, ProductRow
from review_data.core.db.repository import CollectionJobRepository, ProductRepository
from review_data.core.models import Product
from review_data.core.service.collection import CollectionService
from review_data.core.service.scheduling import SchedulingService
from review_data.core.settings import Settings

PLATFORM = "schedplat"
NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
SETTINGS = Settings(
    product_ttl_seconds=24 * 3600,
    review_ttl_seconds=6 * 3600,
    schedule_max_pending=20,
    schedule_failure_cooldown_seconds=6 * 3600,
)


async def _add_product(
    session_factory,
    product_id: str,
    *,
    product_age_hours: float,
    review_age_hours: float | None,
) -> None:
    """수집된 지 주어진 시간만큼 지난 상품을 만든다. review_age_hours=None 은 리뷰 미수집."""
    async with session_factory() as s:
        await ProductRepository(s).upsert(
            Product(platform=PLATFORM, product_id=product_id, name="상품", url="https://x")
        )
        await s.execute(
            update(ProductRow)
            .where(ProductRow.platform == PLATFORM, ProductRow.product_id == product_id)
            .values(
                last_collected_at=NOW - timedelta(hours=product_age_hours),
                reviews_last_collected_at=(
                    None if review_age_hours is None else NOW - timedelta(hours=review_age_hours)
                ),
            )
        )
        await s.commit()


async def _add_finished_job(
    session_factory, product_id: str, status: str, hours_ago: float
) -> None:
    async with session_factory() as s:
        s.add(
            CollectionJob(
                platform=PLATFORM,
                product_id=product_id,
                idempotency_key="k",
                status=status,
                completed_at=NOW - timedelta(hours=hours_ago),
            )
        )
        await s.commit()


async def _schedule(session_factory, settings: Settings = SETTINGS):
    async with session_factory() as s:
        result = await SchedulingService(s, settings).run_once(now=NOW)
        await s.commit()
    return result


async def _jobs(session_factory) -> list[CollectionJob]:
    async with session_factory() as s:
        rows = await s.execute(
            select(CollectionJob)
            .where(CollectionJob.status == "pending")
            .order_by(CollectionJob.id)
        )
        return list(rows.scalars())


async def test_stale_product_gets_job_marked_as_scheduler(session_factory):
    await _add_product(session_factory, "old", product_age_hours=30, review_age_hours=30)

    result = await _schedule(session_factory)

    jobs = await _jobs(session_factory)
    assert result.created == 1
    assert [(j.product_id, j.requested_by) for j in jobs] == [("old", "scheduler")]


async def test_fresh_product_is_left_alone(session_factory):
    await _add_product(session_factory, "fresh", product_age_hours=1, review_age_hours=1)

    result = await _schedule(session_factory)

    assert result.created == 0
    assert await _jobs(session_factory) == []


async def test_stale_reviews_alone_are_enough(session_factory):
    # 상품은 신선해도 리뷰가 낡았거나(7h) 한 번도 못 받았으면 다시 수집해야 한다.
    await _add_product(session_factory, "rev-old", product_age_hours=1, review_age_hours=7)
    await _add_product(session_factory, "rev-none", product_age_hours=1, review_age_hours=None)

    await _schedule(session_factory)

    assert {j.product_id for j in await _jobs(session_factory)} == {"rev-old", "rev-none"}


@pytest.mark.parametrize(
    ("product_age_hours", "review_age_hours"),
    [
        (23.99, 5.99),  # 둘 다 TTL 직전
        (24, 1),  # 상품이 정확히 TTL
        (24.01, 1),
        (1, 6),  # 리뷰가 정확히 TTL
        (1, 6.01),
        (1, None),  # 리뷰 미수집
    ],
)
async def test_agrees_with_api_freshness_rule(
    session_factory, product_age_hours, review_age_hours
):
    """같은 시각·같은 상품에 대해 "조회에서 stale" 과 "스케줄 대상" 이 항상 같아야 한다.

    어긋나면 스케줄러가 돌았는데도 조회에서 stale 로 나오거나, 그 반대가 된다.
    """
    await _add_product(
        session_factory,
        "edge",
        product_age_hours=product_age_hours,
        review_age_hours=review_age_hours,
    )

    result = await _schedule(session_factory)

    async with session_factory() as s:
        service = CollectionService(s, SETTINGS)
        product = await service.products.get(PLATFORM, "edge")
        stale_for_api = not service._is_fresh(product, now=NOW)
    assert bool(result.created) is stale_for_api


async def test_product_with_active_job_is_skipped(session_factory):
    await _add_product(session_factory, "busy", product_age_hours=30, review_age_hours=30)
    async with session_factory() as s:
        await CollectionJobRepository(s).create_or_get_active(PLATFORM, "busy")
        await s.commit()

    result = await _schedule(session_factory)

    jobs = await _jobs(session_factory)
    assert result.created == 0
    assert len(jobs) == 1 and jobs[0].requested_by is None


async def test_recently_failed_product_waits_for_cooldown(session_factory):
    await _add_product(session_factory, "blocked", product_age_hours=30, review_age_hours=None)
    await _add_product(session_factory, "partial", product_age_hours=1, review_age_hours=None)
    await _add_product(session_factory, "retry", product_age_hours=30, review_age_hours=None)
    await _add_finished_job(session_factory, "blocked", "failed", hours_ago=1)
    await _add_finished_job(session_factory, "partial", "partial", hours_ago=1)
    # 대기 시간(6h)이 지난 실패는 다시 시도한다.
    await _add_finished_job(session_factory, "retry", "failed", hours_ago=7)

    await _schedule(session_factory)

    assert [j.product_id for j in await _jobs(session_factory)] == ["retry"]


async def test_old_success_does_not_block(session_factory):
    await _add_product(session_factory, "ok", product_age_hours=30, review_age_hours=30)
    await _add_finished_job(session_factory, "ok", "succeeded", hours_ago=1)

    result = await _schedule(session_factory)

    assert result.created == 1


async def test_fills_only_up_to_pending_limit_oldest_first(session_factory):
    settings = SETTINGS.model_copy(update={"schedule_max_pending": 3})
    for i, age in enumerate([10, 50, 30, 40, 20]):
        await _add_product(session_factory, f"p{i}", product_age_hours=age, review_age_hours=age)
    # 조회로 이미 대기 중인 job 이 하나 있다.
    await _add_product(session_factory, "waiting", product_age_hours=99, review_age_hours=99)
    async with session_factory() as s:
        await CollectionJobRepository(s).create_or_get_active(PLATFORM, "waiting")
        await s.commit()

    result = await _schedule(session_factory, settings)

    scheduled = [j.product_id for j in await _jobs(session_factory) if j.requested_by]
    assert result.pending_before == 1
    assert result.created == 2
    # 50h, 40h 순으로 오래된 두 개만 채운다.
    assert scheduled == ["p1", "p3"]


async def test_never_reviewed_products_come_first(session_factory):
    settings = SETTINGS.model_copy(update={"schedule_max_pending": 1})
    await _add_product(session_factory, "very-old", product_age_hours=90, review_age_hours=90)
    await _add_product(session_factory, "no-reviews", product_age_hours=2, review_age_hours=None)

    await _schedule(session_factory, settings)

    assert [j.product_id for j in await _jobs(session_factory)] == ["no-reviews"]


async def test_does_nothing_when_queue_is_full(session_factory):
    settings = SETTINGS.model_copy(update={"schedule_max_pending": 1})
    await _add_product(session_factory, "a", product_age_hours=30, review_age_hours=30)
    await _add_product(session_factory, "b", product_age_hours=30, review_age_hours=30)

    first = await _schedule(session_factory, settings)
    second = await _schedule(session_factory, settings)

    assert (first.created, second.created) == (1, 0)
    assert len(await _jobs(session_factory)) == 1


async def test_running_twice_does_not_duplicate(session_factory):
    await _add_product(session_factory, "x", product_age_hours=30, review_age_hours=30)
    await _add_product(session_factory, "y", product_age_hours=30, review_age_hours=30)

    first = await _schedule(session_factory)
    second = await _schedule(session_factory)

    assert (first.created, second.created) == (2, 0)
    assert len(await _jobs(session_factory)) == 2


async def test_failure_without_completed_at_still_waits(session_factory):
    # 완료 시각 없이 실패로 남은 이력(수동 정리 등)도 대기 시간을 지켜야 한다.
    await _add_product(session_factory, "manual", product_age_hours=30, review_age_hours=None)
    async with session_factory() as s:
        s.add(
            CollectionJob(
                platform=PLATFORM, product_id="manual", idempotency_key="k", status="failed"
            )
        )
        await s.commit()

    result = await _schedule(session_factory)

    assert result.created == 0


async def test_excluded_platform_is_never_scheduled(session_factory):
    settings = SETTINGS.model_copy(
        update={"schedule_excluded_platforms": f"other, {PLATFORM} "}
    )
    await _add_product(session_factory, "blocked", product_age_hours=30, review_age_hours=30)

    result = await _schedule(session_factory, settings)

    assert result.created == 0


async def test_concurrent_schedulers_respect_pending_limit(session_factory):
    """두 스케줄러가 동시에 돌아도 대기 상한을 넘기지 않는다."""
    settings = SETTINGS.model_copy(update={"schedule_max_pending": 1})
    for name in ("a", "b", "c"):
        await _add_product(session_factory, name, product_age_hours=30, review_age_hours=30)

    results = await asyncio.gather(
        *(_schedule(session_factory, settings) for _ in range(4))
    )

    assert sum(r.created for r in results) == 1
    assert len(await _jobs(session_factory)) == 1


@pytest.mark.parametrize("value", [0, -1])
def test_interval_must_be_positive(value):
    with pytest.raises(ValueError):
        Settings(schedule_interval_seconds=value)
