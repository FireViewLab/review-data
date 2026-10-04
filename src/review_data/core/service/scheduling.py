"""
낡은 상품을 찾아 수집 job 을 예약하는 유스케이스.

조회 API 는 누군가 상품을 조회했을 때만 job 을 만든다. 스케줄러는 조회가 없어도 DB 에
있는 상품을 주기적으로 다시 수집하게 해서, 조회 시점에 이미 최신 데이터가 있게 한다.
실제 크롤링은 워커가 한다. 여기서는 job 을 만들기만 한다.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from review_data.core.db.repository import CollectionJobRepository
from review_data.core.settings import Settings, get_settings

REQUESTED_BY = "scheduler"


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
        pending = await self.jobs.count_pending()
        room = self.settings.schedule_max_pending - pending
        if room <= 0:
            return ScheduleResult(pending_before=pending, created=0)

        candidates = await self.jobs.list_refresh_candidates(
            product_cutoff=now - timedelta(seconds=self.settings.product_ttl_seconds),
            review_cutoff=now - timedelta(seconds=self.settings.review_ttl_seconds),
            failure_cutoff=now
            - timedelta(seconds=self.settings.schedule_failure_cooldown_seconds),
            limit=room,
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
