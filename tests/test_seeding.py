"""검색 실패와 재실행이 기존 수집 대상을 망가뜨리지 않는지 확인한다."""

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select, update
from typer.testing import CliRunner

from review_data import cli
from review_data.core.base import BaseCollector
from review_data.core.db.models import CollectionJob, ProductRow
from review_data.core.db.repository import ProductRepository
from review_data.core.models import Product, Review
from review_data.core.service.scheduling import SchedulingService
from review_data.core.service.seeding import SeedingService, SeedPlan, load_seed_file
from review_data.core.settings import Settings
from review_data.worker import collection_worker

PLATFORM = "seedplat"
SETTINGS = Settings(_env_file=None, request_delay=0)


class _Collector(BaseCollector):
    platform = PLATFORM
    calls: list[tuple[str, int]] = []

    async def setup(self) -> None:
        pass

    async def search_products(self, keyword: str, limit: int = 20) -> list[Product]:
        self.calls.append((keyword, limit))
        if keyword == "실패":
            raise RuntimeError("검색 실패")
        return [Product(platform=self.platform, product_id="one", name=keyword, url="https://x")]

    async def get_product(self, product_id: str) -> Product:
        return Product(platform=self.platform, product_id=product_id, name="상품", url="https://x")

    async def get_reviews(self, product_id: str, limit: int = 50) -> list[Review]:
        return [
            Review(platform=self.platform, product_id=product_id, review_id="r", content="좋아요")
        ]


@pytest.fixture(autouse=True)
def fake_collector(monkeypatch):
    _Collector.calls = []
    monkeypatch.setattr(_Collector, "polite_wait", AsyncMock())


def _file(tmp_path, content):
    path = tmp_path / "seeds.toml"
    path.write_text(content)
    return path


def test_parse_seed_file(tmp_path):
    plan = load_seed_file(
        _file(
            tmp_path,
            """per_keyword = 3
[keywords]
seedplat = [" 선크림 ", "선크림"]
[products]
seedplat = ["zinus:6000252751"]
""",
        ),
        {PLATFORM: _Collector},
    )
    assert plan == SeedPlan(3, {PLATFORM: ("선크림",)}, {PLATFORM: ("zinus:6000252751",)})


@pytest.mark.parametrize(
    "content",
    [
        "per_keyword = 0",
        "per_keyword = -1",
        "per_keyword = true",
        'per_keyword = "20"',
        "per_keyword = 1.2",
        "keywords = []",
        '[keywords]\nunknown = ["x"]',
        '[products]\nunknown = ["id"]',
        '[keywords]\nseedplat = "x"',
        "[keywords]\nseedplat = []",
        '[keywords]\nseedplat = [" "]',
        "[keywords]\nseedplat = [1]",
        "[products]\nseedplat = [false]",
        '[products]\nseedplat = [""]',
        '[stores]\nseedplat = ["x"]',
        "not toml",
    ],
)
def test_invalid_seed_file_is_rejected(tmp_path, content):
    with pytest.raises(ValueError):
        load_seed_file(_file(tmp_path, content), {PLATFORM: _Collector})


def test_default_seed_matches_spec():
    from review_data.core.discovery import discover

    registry, failures = discover()
    assert not failures
    plan = load_seed_file(Path(__file__).resolve().parents[1] / "seeds.toml", registry)
    assert plan.per_keyword == 20
    assert plan.keywords == {
        "oliveyoung": ("선크림", "토너", "세럼", "클렌징폼", "마스크팩"),
        "musinsa": ("반팔티", "후드티", "청바지", "스니커즈", "백팩"),
        "ably": ("원피스", "블라우스", "니트", "가디건", "스커트"),
        "ohouse": ("침대프레임", "책상", "의자", "조명", "러그"),
        "kurly": ("닭가슴살", "샐러드", "밀키트", "그릭요거트", "만두"),
        "elevenst": ("생수", "물티슈", "이어폰", "보조배터리", "영양제"),
    }
    assert plan.products == {"naver": ("zinus:6000252751",)}
    assert plan.expand == {"naver": 20}


async def test_keywords_saved_and_direct_products_queued_without_duplicates(session_factory):
    service = SeedingService({PLATFORM: _Collector}, session_factory, SETTINGS)
    plan = SeedPlan(3, {PLATFORM: ("선크림",)}, {PLATFORM: ("direct",)})
    first = await service.run(plan)
    second = await service.run(plan)
    # 재실행에서는 이미 있는 상품을 다시 저장하지 않는다.
    assert (first[PLATFORM].saved, second[PLATFORM].saved) == (1, 0)
    assert first[PLATFORM].created == 1 and second[PLATFORM].created == 0
    assert _Collector.calls == [("선크림", 3), ("선크림", 3)]
    async with session_factory() as s:
        assert await s.scalar(select(func.count()).select_from(ProductRow)) == 1
        row = await ProductRepository(s).get(PLATFORM, "one")
        assert row.reviews_last_collected_at is None
        jobs = (await s.execute(select(CollectionJob))).scalars().all()
        assert len(jobs) == 1
        assert (jobs[0].product_id, jobs[0].requested_by) == ("direct", "seed")


async def test_failure_continues_and_waits_between_searches(session_factory):
    class OtherCollector(_Collector):
        platform = "other"

    service = SeedingService(
        {PLATFORM: _Collector, "other": OtherCollector}, session_factory, SETTINGS
    )
    results = await service.run(
        SeedPlan(20, {PLATFORM: ("실패", "성공"), "other": ("다음",)}, {PLATFORM: ("direct",)})
    )
    assert len(results[PLATFORM].errors) == 1
    assert results[PLATFORM].saved == results["other"].saved == 1
    assert results[PLATFORM].created == 1
    assert _Collector.polite_wait.await_count == 2


async def test_setup_failure_does_not_stop_other_platform_or_direct_job(session_factory):
    class BrokenCollector(_Collector):
        async def setup(self):
            raise RuntimeError("인증 실패")

    class OtherCollector(_Collector):
        platform = "other"

    results = await SeedingService(
        {PLATFORM: BrokenCollector, "other": OtherCollector}, session_factory, SETTINGS
    ).run(SeedPlan(20, {PLATFORM: ("x",), "other": ("y",)}, {PLATFORM: ("direct",)}))
    assert len(results[PLATFORM].errors) == 1
    assert results[PLATFORM].created == results["other"].saved == 1


async def test_database_failure_rolls_back_only_one_keyword(session_factory, monkeypatch):
    original = ProductRepository.upsert

    async def fail_after_insert(self, product):
        await original(self, product)
        if product.name == "저장실패":
            raise RuntimeError("DB 실패")

    monkeypatch.setattr(ProductRepository, "upsert", fail_after_insert)
    results = await SeedingService({PLATFORM: _Collector}, session_factory, SETTINGS).run(
        SeedPlan(20, {PLATFORM: ("저장실패", "정상")}, {})
    )
    assert results[PLATFORM].saved == 1 and len(results[PLATFORM].errors) == 1
    async with session_factory() as s:
        assert (await ProductRepository(s).get(PLATFORM, "one")).name == "정상"


async def test_dry_run_does_not_open_collectors_or_sessions():
    class ForbiddenCollector(_Collector):
        def __init__(self, *args, **kwargs):
            pytest.fail("dry-run 에 collector 를 생성했습니다")

    def forbidden_session():
        pytest.fail("dry-run 에 DB 세션을 열었습니다")

    result = await SeedingService({PLATFORM: ForbiddenCollector}, forbidden_session, SETTINGS).run(
        SeedPlan(20, {PLATFORM: ("검색",)}, {PLATFORM: ("direct",)}), dry_run=True
    )
    assert result[PLATFORM].saved == result[PLATFORM].created == 0
    assert not result[PLATFORM].errors


async def test_excluded_platform_uses_shared_settings_parser():
    settings = SETTINGS.model_copy(update={"schedule_excluded_platforms": " , seedplat, other, "})
    assert settings.excluded_platforms() == (PLATFORM, "other")

    def forbidden_session():
        pytest.fail("제외 플랫폼에 DB 세션을 열었습니다")

    result = await SeedingService({PLATFORM: _Collector}, forbidden_session, settings).run(
        SeedPlan(20, {PLATFORM: ("검색",)}, {PLATFORM: ("direct",)})
    )
    assert result[PLATFORM].skipped
    assert not _Collector.calls


async def test_existing_review_collection_time_is_preserved(session_factory):
    timestamp = datetime(2026, 1, 1, tzinfo=UTC)
    async with session_factory() as s:
        await ProductRepository(s).upsert(
            Product(platform=PLATFORM, product_id="one", name="기존", url="https://x")
        )
        await s.execute(update(ProductRow).values(reviews_last_collected_at=timestamp))
        await s.commit()
    service = SeedingService({PLATFORM: _Collector}, session_factory, SETTINGS)
    for _ in range(2):
        await service.run(SeedPlan(20, {PLATFORM: ("새이름",)}, {}))
    async with session_factory() as s:
        row = await ProductRepository(s).get(PLATFORM, "one")
        # 이미 있는 상품은 검색 결과로 덮어쓰지 않는다.
        assert row.name == "기존"
        assert row.reviews_last_collected_at == timestamp


async def test_seed_scheduler_worker_collects_reviews_with_fake_collector(
    session_factory, monkeypatch
):
    await SeedingService({PLATFORM: _Collector}, session_factory, SETTINGS).run(
        SeedPlan(20, {PLATFORM: ("상품",)}, {})
    )
    async with session_factory() as s:
        assert (await SchedulingService(s, SETTINGS).run_once()).created == 1
        await s.commit()
    monkeypatch.setattr(collection_worker, "discover", lambda: ({PLATFORM: _Collector}, []))
    assert await collection_worker.run_once(session_factory, "seed-worker", settings=SETTINGS)
    async with session_factory() as s:
        row = await ProductRepository(s).get(PLATFORM, "one")
        assert row.reviews_last_collected_at is not None


def test_cli_dry_run_prints_plan_without_network(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_load_registry", lambda: {PLATFORM: _Collector})
    monkeypatch.setattr(cli, "_require_env", lambda: None)
    path = _file(tmp_path, '[keywords]\nseedplat = ["키워드"]\n[products]\nseedplat=["direct"]')
    result = CliRunner().invoke(cli.app, ["seed", "--file", str(path), "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "검색 예정: 키워드" in result.output
    assert "예약 예정: direct" in result.output
    assert "저장 0건, 예약 0건, 실패 0건" in result.output
    assert not _Collector.calls


def test_cli_invalid_file_exits_nonzero(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_load_registry", lambda: {PLATFORM: _Collector})
    monkeypatch.setattr(cli, "_require_env", lambda: None)
    result = CliRunner().invoke(cli.app, ["seed", "--file", str(tmp_path / "absent")])
    assert result.exit_code == 1
    assert "시드 파일 오류" in result.output


async def test_search_limit_is_enforced_even_if_collector_returns_more(session_factory):
    class ManyCollector(_Collector):
        async def search_products(self, keyword, limit=20):
            return [
                Product(platform=self.platform, product_id=str(i), name="상품", url="https://x")
                for i in range(5)
            ]

    results = await SeedingService({PLATFORM: ManyCollector}, session_factory, SETTINGS).run(
        SeedPlan(2, {PLATFORM: ("x",)}, {})
    )
    assert results[PLATFORM].saved == 2
    async with session_factory() as s:
        assert await s.scalar(select(func.count()).select_from(ProductRow)) == 2


async def test_direct_job_failure_does_not_stop_next_product(session_factory, monkeypatch):
    from review_data.core.db.repository import CollectionJobRepository

    original = CollectionJobRepository.create_or_get_active

    async def fail_one(self, platform, product_id, **kwargs):
        if product_id == "bad":
            raise RuntimeError("예약 실패")
        return await original(self, platform, product_id, **kwargs)

    monkeypatch.setattr(CollectionJobRepository, "create_or_get_active", fail_one)
    results = await SeedingService({PLATFORM: _Collector}, session_factory, SETTINGS).run(
        SeedPlan(20, {}, {PLATFORM: ("bad", "good")})
    )
    assert results[PLATFORM].created == 1 and len(results[PLATFORM].errors) == 1
    async with session_factory() as s:
        assert await CollectionJobRepository(s).get_active(PLATFORM, "good") is not None


async def test_rerun_does_not_overwrite_detailed_product(session_factory):
    """검색 결과는 상세 수집보다 정보가 적다. 재실행이 상세 정보를 지우면 안 된다."""
    collected_at = datetime(2026, 10, 1, tzinfo=UTC)
    async with session_factory() as s:
        await ProductRepository(s).upsert(
            Product(
                platform=PLATFORM,
                product_id="one",
                name="상세 이름",
                url="https://x",
                brand="브랜드",
            )
        )
        await s.execute(update(ProductRow).values(last_collected_at=collected_at))
        await s.commit()

    result = await SeedingService({PLATFORM: _Collector}, session_factory, SETTINGS).run(
        SeedPlan(3, {PLATFORM: ("선크림",)}, {})
    )

    assert result[PLATFORM].saved == 0
    async with session_factory() as s:
        row = await ProductRepository(s).get(PLATFORM, "one")
        assert (row.name, row.brand, row.last_collected_at) == ("상세 이름", "브랜드", collected_at)


class _RelatedCollector(_Collector):
    async def related_products(self, product_id: str, limit: int = 20) -> list[str]:
        if product_id == "broken":
            raise RuntimeError("차단")
        return [f"{product_id}-r{i}" for i in range(limit + 5)]


async def test_expand_queues_related_products_up_to_limit(session_factory):
    service = SeedingService({PLATFORM: _RelatedCollector}, session_factory, SETTINGS)
    plan = SeedPlan(3, {}, {PLATFORM: ("a", "broken", "b")}, {PLATFORM: 2})

    first = await service.run(plan)
    second = await service.run(plan)

    # 지정 상품 3개 + 관련 상품 2개씩(a, b). broken 의 실패는 나머지를 막지 않는다.
    assert first[PLATFORM].created == 7
    assert len(first[PLATFORM].errors) == 1 and "broken" in first[PLATFORM].errors[0]
    assert second[PLATFORM].created == 0
    async with session_factory() as s:
        ids = set((await s.execute(select(CollectionJob.product_id))).scalars())
    assert ids == {"a", "broken", "b", "a-r0", "a-r1", "b-r0", "b-r1"}


async def test_expand_reports_unsupported_platform(session_factory):
    service = SeedingService({PLATFORM: _Collector}, session_factory, SETTINGS)

    result = await service.run(SeedPlan(3, {}, {PLATFORM: ("a",)}, {PLATFORM: 2}))

    assert result[PLATFORM].created == 1
    assert "지원하지 않습니다" in result[PLATFORM].errors[0]


async def test_dry_run_does_not_expand():
    service = SeedingService({PLATFORM: _RelatedCollector}, None, SETTINGS)

    result = await service.run(SeedPlan(3, {}, {PLATFORM: ("a",)}, {PLATFORM: 2}), dry_run=True)

    assert result[PLATFORM].created == 0


@pytest.mark.parametrize(
    "content",
    [
        'per_keyword = 1\n[expand]\nnaver = 5',  # products 없이 expand 만
        'per_keyword = 1\n[products]\nnaver = ["a:1"]\n[expand]\nnaver = 0',
        'per_keyword = 1\n[products]\nnaver = ["a:1"]\n[expand]\nnaver = "많이"',
    ],
)
def test_invalid_expand_is_rejected(tmp_path, content):
    with pytest.raises(ValueError):
        load_seed_file(_file(tmp_path, content), {"naver": _Collector})
