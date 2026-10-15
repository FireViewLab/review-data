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


async def _add_blocked_job(session_factory, platform, product_id, hours_ago, status="partial"):
    async with session_factory() as s:
        s.add(
            CollectionJob(
                platform=platform,
                product_id=product_id,
                idempotency_key="k",
                status=status,
                failure_kind="blocked" if status != "succeeded" else None,
                completed_at=NOW - timedelta(hours=hours_ago),
            )
        )
        await s.commit()


async def test_blocked_platform_pauses_then_probes_one_then_recovers(session_factory):
    other = "otherplat"
    for i in range(3):
        await _add_product(session_factory, f"b{i}", product_age_hours=48, review_age_hours=48)
    async with session_factory() as s:
        await ProductRepository(s).upsert(
            Product(platform=other, product_id="o", name="상품", url="https://x")
        )
        await s.execute(
            update(ProductRow)
            .where(ProductRow.platform == other)
            .values(last_collected_at=NOW - timedelta(hours=48), reviews_last_collected_at=None)
        )
        await s.commit()
    # 다른 상품의 성공 뒤에 차단이 났다. 상품별 대기만 보면 b0~b2 는 예약 대상이다.
    await _add_blocked_job(session_factory, PLATFORM, "b9", hours_ago=0.5)
    await _add_blocked_job(session_factory, PLATFORM, "old-success", 3, status="succeeded")

    settings = SETTINGS.model_copy(update={"schedule_platform_block_seconds": 3600})
    await _schedule(session_factory, settings)
    assert {(j.platform, j.product_id) for j in await _jobs(session_factory)} == {(other, "o")}

    # 대기 시간이 지나면 한 건만 시험한다. 시험 작업이 끝나기 전에는 더 예약하지 않는다.
    later = settings.model_copy(update={"schedule_platform_block_seconds": 600})
    await _schedule(session_factory, later)
    await _schedule(session_factory, later)
    probes = [j for j in await _jobs(session_factory) if j.platform == PLATFORM]
    assert len(probes) == 1

    # 시험이 성공하면 평소대로 남은 상품을 모두 예약한다.
    async with session_factory() as s:
        await s.execute(
            update(CollectionJob)
            .where(CollectionJob.id == probes[0].id)
            .values(status="succeeded", completed_at=NOW - timedelta(minutes=1))
        )
        await s.commit()
    await _schedule(session_factory, later)
    scheduled = [j for j in await _jobs(session_factory) if j.platform == PLATFORM]
    assert len(scheduled) == 3


async def test_paused_platform_keeps_queued_automatic_jobs_and_frees_queue(session_factory):
    other = "otherplat"
    await _add_blocked_job(session_factory, PLATFORM, "x", hours_ago=0)
    async with session_factory() as s:
        # 워커는 실제 현재 시각으로 판정하므로 차단 시각도 지금으로 맞춘다.
        await s.execute(update(CollectionJob).values(completed_at=datetime.now(UTC)))
        await s.commit()
    async with session_factory() as s:
        repo = CollectionJobRepository(s)
        for pid in ("a1", "a2"):
            await ProductRepository(s).upsert(
                Product(platform=PLATFORM, product_id=pid, name="상품", url="https://x")
            )
            await repo.create_or_get_active(PLATFORM, pid, requested_by="scheduler")
        await ProductRepository(s).upsert(
            Product(platform=other, product_id="o", name="상품", url="https://x")
        )
        await repo.create_or_get_active(other, "o", requested_by="scheduler")
        await s.commit()

    async with session_factory() as s:
        repo = CollectionJobRepository(s)
        # 대기 중인 플랫폼의 자동 작업은 큐 여유 계산에서 빠진다.
        paused, _ = await repo.blocked_platforms(3600, datetime.now(UTC))
        assert paused == {PLATFORM} and await repo.count_pending(paused) == 1
        # 워커도 그 작업을 꺼내지 않고 다른 플랫폼 작업만 가져간다.
        job = await repo.claim_one("w", block_pause_seconds=3600)
        assert job.platform == other
        assert await repo.claim_one("w", block_pause_seconds=3600) is None
        await s.commit()

    async with session_factory() as s:
        repo = CollectionJobRepository(s)
        # 사람이 요청한 작업은 차단 중에도 실행한다.
        await repo.create_or_get_active(PLATFORM, "manual")
        job = await repo.claim_one("w", block_pause_seconds=3600)
        assert job.product_id == "manual"
        await s.commit()


async def test_probing_platform_runs_queued_jobs_one_at_a_time(session_factory):
    await _add_blocked_job(session_factory, PLATFORM, "x", hours_ago=0)
    async with session_factory() as s:
        repo = CollectionJobRepository(s)
        for pid in ("a1", "a2"):
            await ProductRepository(s).upsert(
                Product(platform=PLATFORM, product_id=pid, name="상품", url="https://x")
            )
            await repo.create_or_get_active(PLATFORM, pid, requested_by="scheduler")
        await s.commit()
    async with session_factory() as s:
        repo = CollectionJobRepository(s)
        # 대기 시간이 지나 시험 단계가 되면 한 번에 하나씩만 꺼낸다.
        await s.execute(
            update(CollectionJob)
            .where(CollectionJob.product_id == "x")
            .values(completed_at=datetime.now(UTC) - timedelta(hours=2))
        )
        assert (await repo.claim_one("w", block_pause_seconds=3600)).product_id == "a1"
        assert await repo.claim_one("w", block_pause_seconds=3600) is None
        await s.commit()


async def test_legacy_unknown_403_history_counts_as_block(session_factory):
    async with session_factory() as s:
        s.add(
            CollectionJob(
                platform=PLATFORM,
                product_id="legacy",
                idempotency_key="k",
                status="partial",
                failure_kind="unknown",
                last_error="unexpected: HTTPStatusError: Client error '403 Forbidden' for url",
                completed_at=datetime.now(UTC),
            )
        )
        await s.commit()
        paused, _ = await CollectionJobRepository(s).blocked_platforms(3600, datetime.now(UTC))
        assert paused == {PLATFORM}


def test_platform_intervals_parse():
    settings = Settings(_env_file=None, collection_platform_intervals=" a=80, b=0,")
    assert settings.platform_intervals() == {"a": 80, "b": 0}


@pytest.mark.parametrize("value", ["a=80s", "a", "a=-80", "=80", "a=3601"])
def test_invalid_platform_intervals_are_rejected_at_startup(value):
    # 실행 중에 알면 모든 플랫폼 수집이 멈추므로 설정을 읽을 때 거부한다.
    with pytest.raises(ValueError):
        Settings(_env_file=None, collection_platform_intervals=value)


async def test_paced_platform_starts_automatic_jobs_at_interval(session_factory):
    other = "otherplat"
    async with session_factory() as s:
        repo = CollectionJobRepository(s)
        for platform, pid in ((PLATFORM, "a1"), (PLATFORM, "a2"), (other, "o")):
            await ProductRepository(s).upsert(
                Product(platform=platform, product_id=pid, name="상품", url="https://x")
            )
            await repo.create_or_get_active(platform, pid, requested_by="scheduler")
        await s.commit()
    intervals = {PLATFORM: 600}
    async with session_factory() as s:
        repo = CollectionJobRepository(s)
        assert (await repo.claim_one("w", platform_intervals=intervals)).product_id == "a1"
        # 간격이 지나기 전에는 같은 플랫폼의 다음 자동 작업 대신 다른 플랫폼을 꺼낸다.
        assert (await repo.claim_one("w", platform_intervals=intervals)).platform == other
        assert await repo.claim_one("w", platform_intervals=intervals) is None
        # 사람이 요청한 작업은 간격과 무관하게 실행한다.
        await ProductRepository(s).upsert(
            Product(platform=PLATFORM, product_id="m", name="상품", url="https://x")
        )
        await repo.create_or_get_active(PLATFORM, "m")
        assert (await repo.claim_one("w", platform_intervals=intervals)).product_id == "m"
        # 간격이 지나면 다음 자동 작업을 시작한다.
        await s.execute(
            update(CollectionJob)
            .where(CollectionJob.platform == PLATFORM, CollectionJob.status == "running")
            .values(locked_at=datetime.now(UTC) - timedelta(seconds=601))
        )
        assert (await repo.claim_one("w", platform_intervals=intervals)).product_id == "a2"
        await s.commit()


async def test_scheduler_queues_paced_platform_share_and_fills_rest(session_factory):
    other = "otherplat"
    for i in range(10):
        await _add_product(session_factory, f"p{i}", product_age_hours=48, review_age_hours=None)
    async with session_factory() as s:
        for i in range(10):
            await ProductRepository(s).upsert(
                Product(platform=other, product_id=f"o{i}", name="상품", url="https://x")
            )
        await s.execute(
            update(ProductRow)
            .where(ProductRow.platform == other)
            .values(last_collected_at=NOW - timedelta(hours=1), reviews_last_collected_at=NOW)
        )
        await s.execute(
            update(ProductRow)
            .where(ProductRow.platform == other)
            .values(reviews_last_collected_at=NOW - timedelta(hours=7))
        )
        await s.commit()
    # 5분 주기에 100초 간격이면 한 주기에 3건만 예약하고 나머지 자리는 다른 플랫폼이 쓴다.
    settings = SETTINGS.model_copy(
        update={"collection_platform_intervals": f"{PLATFORM}=100", "schedule_max_pending": 8}
    )
    await _schedule(session_factory, settings)
    jobs = await _jobs(session_factory)
    assert sum(j.platform == PLATFORM for j in jobs) == 3
    assert sum(j.platform == other for j in jobs) == 5
    # 아직 시작하지 않은 몫이 남아 있으면 그만큼 덜 예약한다.
    await _schedule(session_factory, settings.model_copy(update={"schedule_max_pending": 20}))
    jobs = await _jobs(session_factory)
    assert sum(j.platform == PLATFORM for j in jobs) == 3


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
