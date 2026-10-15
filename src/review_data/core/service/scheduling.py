"""
낡은 상품을 찾아 수집 job 을 예약하는 유스케이스.

조회 API 는 누군가 상품을 조회했을 때만 job 을 만든다. 스케줄러는 조회가 없어도 DB 에
있는 상품을 주기적으로 다시 수집하게 해서, 조회 시점에 이미 최신 데이터가 있게 한다.
실제 크롤링은 워커가 한다. 여기서는 job 을 만들기만 한다.
"""

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from review_data.core.db.repository import CollectionJobRepository
from review_data.core.settings import Settings, get_settings

REQUESTED_BY = "scheduler"
# 스케줄러끼리만 쓰는 잠금 번호. 값 자체에 의미는 없고 다른 잠금과 겹치지만 않으면 된다.
_SCHEDULER_LOCK_KEY = 5_050_001


@dataclass
class ScheduleResult:
    pending_before: int
    created: int


class SchedulingService:
    def __init__(self, session: AsyncSession, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.jobs = CollectionJobRepository(session)

    async def run_once(self, now: datetime | None = None) -> ScheduleResult:
        now = now or datetime.now(UTC)
        # 대기 수를 세고 예약하는 사이에 다른 스케줄러가 끼어들면 둘 다 "자리가 있다"고
        # 보고 상한을 넘긴다. 트랜잭션이 끝날 때까지 스케줄러를 한 번에 하나만 돌린다.
        await self.session.execute(select(func.pg_advisory_xact_lock(_SCHEDULER_LOCK_KEY)))
        # 차단된 플랫폼은 통째로 쉬고, 대기가 끝난 플랫폼은 한 건만 시험한다.
        paused, probing = await self.jobs.blocked_platforms(
            self.settings.schedule_platform_block_seconds, now
        )
        pending = await self.jobs.count_pending(paused)
        excluded = self.settings.excluded_platforms()
        # 간격을 둔 플랫폼은 한 주기 동안 워커가 시작할 수 있는 건수만 예약한다. 더 넣으면
        # 그 플랫폼 작업이 큐를 채워 다른 플랫폼의 정기 수집까지 자리를 잃는다.
        paced = {
            platform: math.ceil(self.settings.schedule_interval_seconds / seconds)
            for platform, seconds in self.settings.platform_intervals().items()
            if seconds > 0 and platform not in paused | probing | set(excluded)
        }
        waiting = await self.jobs.count_pending_by_platform(automatic_only=True)
        # 발굴 등으로 몫보다 많이 쌓인 작업은 천천히 빠지므로 큐 여유 계산에서 뺀다.
        pending -= sum(max(0, waiting.get(p, 0) - share) for p, share in paced.items())
        from review_data.core.service.analysis_refresh import collection_paused

        if await collection_paused(self.session, self.settings):
            return ScheduleResult(pending_before=pending, created=0)
        room = self.settings.schedule_max_pending - pending
        if room <= 0:
            return ScheduleResult(pending_before=pending, created=0)

        cutoffs = {
            "product_cutoff": now - timedelta(seconds=self.settings.product_ttl_seconds),
            "review_cutoff": now - timedelta(seconds=self.settings.review_ttl_seconds),
            "failure_cutoff": now
            - timedelta(seconds=self.settings.schedule_failure_cooldown_seconds),
            "now": now,
        }
        probing -= set(excluded)
        candidates = []
        for platform in sorted(probing):
            if len(candidates) >= room:
                break
            if await self.jobs.has_active_job(platform):
                continue
            candidates += await self.jobs.list_refresh_candidates(
                **cutoffs, limit=1, excluded_platforms=excluded, only_platforms=(platform,)
            )
        # 큐 여유가 작을 때 같은 플랫폼만 먼저 받지 않도록 가장 오래 쉰 플랫폼부터 배분한다.
        last_start = await self.jobs.last_start_by_platform()
        oldest_first = sorted(
            paced, key=lambda p: (last_start.get(p) or datetime.min.replace(tzinfo=UTC), p)
        )
        for platform in oldest_first:
            share = min(paced[platform] - waiting.get(platform, 0), room - len(candidates))
            if share > 0:
                candidates += await self.jobs.list_refresh_candidates(
                    **cutoffs, limit=share, excluded_platforms=excluded, only_platforms=(platform,)
                )
        if room > len(candidates):
            candidates += await self.jobs.list_refresh_candidates(
                **cutoffs,
                limit=room - len(candidates),
                excluded_platforms=(*excluded, *sorted(paused | probing | set(paced))),
            )
        created = 0
        for platform, product_id in candidates:
            # 후보를 고른 뒤 조회 API 나 다른 스케줄러가 먼저 job 을 만들었을 수 있다.
            # 그때는 기존 job 을 돌려받으므로 중복되지 않는다.
            _, was_created = await self.jobs.create_or_get_active(
                platform, product_id, requested_by=REQUESTED_BY
            )
            created += was_created
        return ScheduleResult(pending_before=pending, created=created)
