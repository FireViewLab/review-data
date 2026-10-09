"""새 발굴은 짧은 DB 트랜잭션·lease·backpressure·저장된 순환 위치를 지킨다."""

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from review_data.core.base import BaseCollector
from review_data.core.db.models import AnalysisJob, CollectionJob, DiscoveryState, ProductRow
from review_data.core.models import Product
from review_data.core.service.product_discovery import KEYWORDS, STATE_KEY, ProductDiscoveryService
from review_data.core.settings import Settings


class Fake(BaseCollector):
    platform = "kurly"
    calls = []

    async def search_products(self, keyword, limit=20):
        Fake.calls.append((self.platform, keyword, limit))
        return [Product(platform=self.platform, product_id=keyword, name=keyword, url="https://x")]

    async def get_product(self, product_id):
        raise NotImplementedError

    async def get_reviews(self, product_id, limit=50):
        return []


def config(**values):
    return Settings(_env_file=None, discovery_enabled=True, request_delay=0, **values)


async def test_rotates_platforms_and_respects_shared_queue_limit(session_factory):
    registry = {p: type(p, (Fake,), {"platform": p}) for p in KEYWORDS}
    result = await ProductDiscoveryService(
        session_factory, registry, config(schedule_max_pending=2)
    ).run_once()
    assert result == {"attempted": 2, "saved": 2, "queued": 2, "failed": 0}
    async with session_factory() as s:
        assert await s.scalar(select(func.count()).select_from(CollectionJob)) == 2
        state = await s.get(DiscoveryState, STATE_KEY)
        assert state.position == 2 and state.lease_token is None
        assert state.next_run_at > datetime.now(UTC)
    assert (await ProductDiscoveryService(session_factory, registry, config()).run_once())[
        "attempted"
    ] == 0


async def test_completed_rotation_increases_search_depth(session_factory):
    Fake.calls = []
    service = ProductDiscoveryService(
        session_factory,
        {"kurly": Fake},
        config(discovery_queries_per_cycle=16, schedule_max_pending=100),
    )
    await service.run_once()
    assert {x[2] for x in Fake.calls} == {20}
    async with session_factory() as s:
        state = await s.get(DiscoveryState, STATE_KEY)
        state.next_run_at = datetime.now(UTC) - timedelta(seconds=1)
        await s.commit()
    Fake.calls = []
    await service.run_once()
    assert {x[2] for x in Fake.calls} == {40}
    async with session_factory() as s:
        assert await s.scalar(select(func.count()).select_from(ProductRow)) == 16


async def test_second_scheduler_cannot_duplicate_inflight_search(session_factory):
    started, release = asyncio.Event(), asyncio.Event()

    class Slow(Fake):
        async def search_products(self, keyword, limit=20):
            started.set()
            await release.wait()
            return await super().search_products(keyword, limit)

    service = ProductDiscoveryService(
        session_factory, {"kurly": Slow}, config(discovery_queries_per_cycle=1)
    )
    first = asyncio.create_task(service.run_once())
    await asyncio.wait_for(started.wait(), 5)
    second = await service.run_once()
    assert second["attempted"] == 0
    release.set()
    assert (await first)["saved"] == 1


async def test_restart_recovers_expired_lease_and_retains_position(session_factory):
    async with session_factory() as s:
        past = datetime.now(UTC) - timedelta(seconds=1)
        s.add(
            DiscoveryState(
                key=STATE_KEY,
                position=5,
                next_run_at=past,
                lease_token="previous-process",
                lease_expires_at=past,
            )
        )
        await s.commit()
    Fake.calls = []
    service = ProductDiscoveryService(
        session_factory, {"kurly": Fake}, config(discovery_queries_per_cycle=1)
    )
    assert (await service.run_once())["attempted"] == 1
    assert Fake.calls[0][1] == KEYWORDS["kurly"][5]


async def test_one_failed_platform_does_not_block_next_platform(session_factory):
    class Failed(Fake):
        async def search_products(self, keyword, limit=20):
            raise RuntimeError("network failed")

    registry = {"kurly": Failed, "oliveyoung": type("Olive", (Fake,), {"platform": "oliveyoung"})}
    result = await ProductDiscoveryService(
        session_factory, registry, config(discovery_queries_per_cycle=2)
    ).run_once()
    assert result == {"attempted": 2, "saved": 1, "queued": 1, "failed": 1}


async def test_analysis_backlog_pauses_discovery_without_advancing(session_factory):
    async with session_factory() as s:
        s.add(ProductRow(platform="kurly", product_id="p", name="상품", url="https://x"))
        await s.flush()
        s.add(AnalysisJob(platform="kurly", product_id="p", status="queued"))
        await s.commit()
    service = ProductDiscoveryService(
        session_factory, {"kurly": Fake}, config(discovery_max_analysis_pending=1)
    )
    assert (await service.run_once())["attempted"] == 0
    async with session_factory() as s:
        assert await s.get(DiscoveryState, STATE_KEY) is None


async def test_disabled_or_excluded_discovery_does_not_search(session_factory):
    assert (
        await ProductDiscoveryService(
            session_factory, {"kurly": Fake}, Settings(_env_file=None)
        ).run_once()
    )["attempted"] == 0

    assert (
        await ProductDiscoveryService(
            session_factory, {"kurly": Fake}, config(schedule_excluded_platforms="kurly")
        ).run_once()
    )["attempted"] == 0


async def test_dense_searches_share_capacity_across_four_platforms(session_factory):
    class Dense(Fake):
        async def search_products(self, keyword, limit=20):
            return [
                Product(
                    platform=self.platform,
                    product_id=f"{keyword}-{i}",
                    name=keyword,
                    url="https://x",
                )
                for i in range(limit)
            ]

    registry = {p: type(p, (Dense,), {"platform": p}) for p in KEYWORDS}
    result = await ProductDiscoveryService(session_factory, registry, config()).run_once()
    assert result["attempted"] == 4 and result["saved"] == 80 and result["queued"] == 20
    async with session_factory() as s:
        rows = (
            await s.execute(
                select(CollectionJob.platform, func.count()).group_by(CollectionJob.platform)
            )
        ).all()
        assert dict(rows) == {p: 5 for p in KEYWORDS}


async def test_lost_lease_discards_late_network_result(session_factory):
    class Lost(Fake):
        async def search_products(self, keyword, limit=20):
            async with session_factory() as s:
                state = await s.get(DiscoveryState, STATE_KEY)
                state.lease_token = "replacement-owner"
                await s.commit()
            return await super().search_products(keyword, limit)

    result = await ProductDiscoveryService(
        session_factory, {"kurly": Lost}, config(discovery_queries_per_cycle=1)
    ).run_once()
    assert result["saved"] == 0
    async with session_factory() as s:
        assert await s.scalar(select(func.count()).select_from(ProductRow)) == 0
        assert (await s.get(DiscoveryState, STATE_KEY)).lease_token == "replacement-owner"


async def test_timeout_advances_to_next_keyword_and_releases_lease(session_factory):
    class Timeout(Fake):
        async def search_products(self, keyword, limit=20):
            await asyncio.sleep(1)

    result = await ProductDiscoveryService(
        session_factory,
        {"kurly": Timeout},
        config(discovery_queries_per_cycle=1, discovery_search_timeout_seconds=0.01),
    ).run_once()
    assert result == {"attempted": 1, "saved": 0, "queued": 0, "failed": 1}
    async with session_factory() as s:
        state = await s.get(DiscoveryState, STATE_KEY)
        assert state.position == 1 and state.lease_token is None
