"""Data에서 키워드·검색 깊이를 순환하고 기존 수집 큐에 연결한다."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from review_data.core.browser import BrowserCollector
from review_data.core.db.models import AnalysisJob, DiscoveryState
from review_data.core.db.repository import CollectionJobRepository
from review_data.core.service.catalog import register_search_products
from review_data.core.service.scheduling import _SCHEDULER_LOCK_KEY

KEYWORDS = {
    "kurly": (
        "두부",
        "계란",
        "우유",
        "치즈",
        "김치",
        "쌀",
        "견과류",
        "커피",
        "과일",
        "새우",
        "연어",
        "국",
        "파스타",
        "과자",
        "아이스크림",
        "고구마",
    ),
    "oliveyoung": (
        "수분크림",
        "클렌징오일",
        "립밤",
        "샴푸",
        "트리트먼트",
        "바디워시",
        "핸드크림",
        "쿠션",
        "컨실러",
        "아이섀도",
        "마스카라",
        "립틴트",
        "헤어에센스",
        "치약",
        "향수",
        "미스트",
    ),
    "musinsa": (
        "맨투맨",
        "셔츠",
        "슬랙스",
        "조거팬츠",
        "운동화",
        "샌들",
        "양말",
        "모자",
        "벨트",
        "지갑",
        "크로스백",
        "재킷",
        "바람막이",
        "코트",
        "패딩",
        "트레이닝복",
    ),
    "elevenst": (
        "세탁세제",
        "주방세제",
        "화장지",
        "키친타월",
        "텀블러",
        "프라이팬",
        "수건",
        "청소기",
        "선풍기",
        "키보드",
        "마우스",
        "충전기",
        "모니터",
        "비타민",
        "유산균",
        "반려동물사료",
    ),
}
STATE_KEY = "keyword-discovery-v1"


class ProductDiscoveryService:
    def __init__(self, factory, registry, settings):
        self.factory, self.registry, self.settings = factory, registry, settings

    def tasks(self):
        allowed = {p.strip() for p in self.settings.discovery_platforms.split(",")}
        excluded = set(self.settings.excluded_platforms())
        platforms = [
            p
            for p in KEYWORDS
            if p in allowed
            and p not in excluded
            and p in self.registry
            and not issubclass(self.registry[p], BrowserCollector)
        ]
        return [(p, KEYWORDS[p][i]) for i in range(16) for p in platforms]

    async def _room(self, session):
        if (
            await CollectionJobRepository(session).count_pending()
            >= self.settings.schedule_max_pending
        ):
            return False
        analysis_pending = await session.scalar(
            select(func.count())
            .select_from(AnalysisJob)
            .where(AnalysisJob.status.in_(("queued", "running")))
        )
        return analysis_pending < self.settings.discovery_max_analysis_pending

    async def _claim(self):
        now = datetime.now(UTC)
        async with self.factory() as session:
            await session.execute(select(func.pg_advisory_xact_lock(_SCHEDULER_LOCK_KEY)))
            if not await self._room(session):
                return None
            await session.execute(
                insert(DiscoveryState)
                .values(key=STATE_KEY, next_run_at=now)
                .on_conflict_do_nothing()
            )
            state = await session.scalar(
                select(DiscoveryState).where(DiscoveryState.key == STATE_KEY).with_for_update()
            )
            if state.next_run_at > now or (state.lease_expires_at and state.lease_expires_at > now):
                await session.commit()
                return None
            state.lease_token = str(uuid4())
            seconds = (
                self.settings.discovery_queries_per_cycle
                * (self.settings.discovery_search_timeout_seconds + 5)
                + 60
            )
            state.lease_expires_at = now + timedelta(seconds=seconds)
            claim = (state.position, state.lease_token)
            await session.commit()
            return claim

    async def run_once(self):
        result = {"attempted": 0, "saved": 0, "queued": 0, "failed": 0}
        tasks = self.tasks()
        if not self.settings.discovery_enabled or not tasks:
            return result
        claim = await self._claim()
        if claim is None:
            return result
        position, token = claim
        for _ in range(self.settings.discovery_queries_per_cycle):
            async with self.factory() as session:
                if not await self._room(session):
                    break
            platform, keyword = tasks[position % len(tasks)]
            limit = min(
                self.settings.discovery_max_limit,
                self.settings.discovery_initial_limit
                + (position // len(tasks)) * self.settings.discovery_limit_step,
            )

            async def search(platform=platform, keyword=keyword, limit=limit):
                async with self.registry[platform](settings=self.settings) as collector:
                    return list(await collector.search_products(keyword, limit=limit))[:limit]

            products = []
            try:
                products = await asyncio.wait_for(
                    search(), self.settings.discovery_search_timeout_seconds
                )
            except Exception:  # noqa: BLE001 - 한 몰의 실패는 다음 키워드까지 막지 않는다
                result["failed"] += 1
            async with self.factory() as session:
                await session.execute(select(func.pg_advisory_xact_lock(_SCHEDULER_LOCK_KEY)))
                state = await session.scalar(
                    select(DiscoveryState).where(DiscoveryState.key == STATE_KEY).with_for_update()
                )
                if (
                    state.lease_token != token
                    or not state.lease_expires_at
                    or state.lease_expires_at <= datetime.now(UTC)
                ):
                    return result
                try:
                    # 검색 결과가 잘못된 플랫폼이면 해당 배치만 되돌린다.
                    async with session.begin_nested():
                        pending = await CollectionJobRepository(session).count_pending()
                        quota = max(
                            1,
                            self.settings.schedule_max_pending
                            // self.settings.discovery_queries_per_cycle,
                        )
                        registration_settings = self.settings.model_copy(
                            update={
                                "schedule_max_pending": min(
                                    self.settings.schedule_max_pending, pending + quota
                                )
                            }
                        )
                        saved = await register_search_products(
                            session, platform, products, registration_settings
                        )
                    result["saved"] += saved["saved"]
                    result["queued"] += saved["queued"]
                except ValueError:
                    result["failed"] += 1
                position += 1
                result["attempted"] += 1
                state.position = position
                await session.commit()
        async with self.factory() as session:
            state = await session.scalar(
                select(DiscoveryState).where(DiscoveryState.key == STATE_KEY).with_for_update()
            )
            if state.lease_token == token:
                state.next_run_at = datetime.now(UTC) + timedelta(
                    seconds=self.settings.discovery_interval_seconds
                )
                state.lease_token = state.lease_expires_at = None
                state.last_completed_at = datetime.now(UTC)
                state.last_saved = result["saved"]
                state.last_queued = result["queued"]
                state.last_failures = result["failed"]
                await session.commit()
        return result
