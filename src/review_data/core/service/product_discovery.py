"""Data에서 키워드·검색 깊이를 순환하고 기존 수집 큐에 연결한다."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from review_data.core.browser import BrowserCollector
from review_data.core.db.analysis_repository import has_prior_result
from review_data.core.db.models import AnalysisJob, DiscoveryState
from review_data.core.db.pipeline_control import collection_stopped
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
        "닭가슴살",
        "만두",
        "샐러드",
        "떡볶이",
        "라면",
        "소시지",
        "베이컨",
        "요거트",
        "그래놀라",
        "식빵",
        "버터",
        "올리브오일",
        "참기름",
        "된장",
        "고추장",
        "간장",
        "밀키트",
        "냉동피자",
        "도시락",
        "차",
        "주스",
        "생수",
        "탄산수",
        "한우",
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
        "선크림",
        "토너",
        "에센스",
        "세럼",
        "앰플",
        "마스크팩",
        "클렌징폼",
        "각질제거",
        "파운데이션",
        "프라이머",
        "블러셔",
        "아이라이너",
        "립스틱",
        "네일",
        "바디로션",
        "데오드란트",
        "헤어오일",
        "왁스",
        "제모",
        "면도기",
        "영양제",
        "단백질쉐이크",
        "여드름패치",
        "아이크림",
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
        "반팔티",
        "긴팔티",
        "후드티",
        "니트",
        "가디건",
        "청바지",
        "반바지",
        "스커트",
        "원피스",
        "트렌치코트",
        "플리스",
        "베스트",
        "로퍼",
        "부츠",
        "슬리퍼",
        "백팩",
        "토트백",
        "선글라스",
        "목걸이",
        "반지",
        "시계",
        "속옷",
        "잠옷",
        "레인코트",
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
        "섬유유연제",
        "물티슈",
        "기저귀",
        "생리대",
        "칫솔",
        "샴푸",
        "전기포트",
        "에어프라이어",
        "전자레인지",
        "밥솥",
        "블렌더",
        "공기청정기",
        "가습기",
        "제습기",
        "이어폰",
        "헤드셋",
        "보조배터리",
        "스마트워치",
        "태블릿거치대",
        "노트북가방",
        "캠핑의자",
        "요가매트",
        "덤벨",
        "자전거헬멧",
    ),
}
STATE_KEY = "keyword-discovery-v1"
# 2026-10-15 이전 검색어 수. 이후 추가분을 먼저 돈다.
ORIGINAL_KEYWORDS = 16


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
        # 플랫폼을 번갈아 돌며 검색어를 하나씩 쓴다. 검색어 수가 달라도 짧은 쪽은 건너뛴다.
        # 10/15에 늘린 검색어(17번째부터)를 앞에 두어, 위치를 처음부터 다시 돌 때 이미 깊게
        # 검색한 기존 검색어보다 새 검색어가 먼저 나간다.
        rounds = max((len(KEYWORDS[p]) for p in platforms), default=0)
        order = [*range(ORIGINAL_KEYWORDS, rounds), *range(min(ORIGINAL_KEYWORDS, rounds))]
        return [(p, KEYWORDS[p][i]) for i in order for p in platforms if i < len(KEYWORDS[p])]

    async def _room(self, session):
        if (
            await CollectionJobRepository(session).count_pending()
            >= self.settings.schedule_max_pending
        ):
            return False
        # 새 상품이 분석을 기다리는 만큼만 센다. 이미 결과가 있는 상품의 재분석이 쌓인 것만으로
        # 신규 상품 유입을 막으면, 재수집이 계속되는 동안 발굴이 영영 돌지 않는다.
        analysis_pending = await session.scalar(
            select(func.count())
            .select_from(AnalysisJob)
            .where(AnalysisJob.status.in_(("queued", "running")), ~has_prior_result())
        )
        return analysis_pending < self.settings.discovery_max_analysis_pending

    async def _claim(self):
        now = datetime.now(UTC)
        async with self.factory() as session:
            await session.execute(select(func.pg_advisory_xact_lock(_SCHEDULER_LOCK_KEY)))
            from review_data.core.service.analysis_refresh import collection_paused

            if (
                await collection_paused(session, self.settings)
                or await collection_stopped(session)
                or not await self._room(session)
            ):
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
            async with self.factory() as session:
                paused, probing = await CollectionJobRepository(session).blocked_platforms(
                    self.settings.schedule_platform_block_seconds, datetime.now(UTC)
                )
            if platform in paused | probing:
                # 차단된 플랫폼에는 검색도 보내지 않는다. 회복 확인은 정기 수집이 맡는다.
                async with self.factory() as session:
                    await session.execute(
                        select(func.pg_advisory_xact_lock(_SCHEDULER_LOCK_KEY))
                    )
                    state = await session.scalar(
                        select(DiscoveryState)
                        .where(DiscoveryState.key == STATE_KEY)
                        .with_for_update()
                    )
                    if state.lease_token != token:
                        return result
                    position += 1
                    state.position = position
                    await session.commit()
                continue
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
