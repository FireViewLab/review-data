"""
TTL 판단 + job 생성 유스케이스.

fresh / stale / queued 세 가지 응답을 조립한다. 실제 크롤링은 worker가 담당하며
이 서비스는 job을 만들기만 하고 기다리지 않는다(비동기 하이브리드 수집 원칙).
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from review_data.core.db.models import CollectionJob, ProductRow, ReviewRow
from review_data.core.db.repository import (
    CollectionJobRepository,
    ProductRepository,
    ReviewRepository,
    validate_review_cursor,
)
from review_data.core.settings import Settings, get_settings

CollectionStatus = Literal["fresh", "stale", "queued"]


@dataclass
class CollectionResult:
    status: CollectionStatus
    product: ProductRow | None
    reviews: list[ReviewRow]
    reviews_next_cursor: str | None
    job: CollectionJob | None


class CollectionService:
    def __init__(self, session: AsyncSession, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.products = ProductRepository(session)
        self.reviews = ReviewRepository(session)
        self.jobs = CollectionJobRepository(session)

    async def get_or_queue(
        self,
        platform: str,
        product_id: str,
        review_limit: int = 20,
        review_cursor: str | None = None,
    ) -> CollectionResult:
        # 상품 유무와 무관하게 입력부터 검증한다.
        if review_cursor is not None:
            validate_review_cursor(review_cursor)

        product = await self.products.get(platform, product_id)

        if product is not None and self._is_fresh(product):
            reviews, next_cursor = await self.reviews.list_page(
                platform, product_id, limit=review_limit, cursor=review_cursor
            )
            return CollectionResult(
                status="fresh",
                product=product,
                reviews=reviews,
                reviews_next_cursor=next_cursor,
                job=None,
            )

        job, _created = await self.jobs.create_or_get_active(platform, product_id)

        if product is None:
            return CollectionResult(
                status="queued", product=None, reviews=[], reviews_next_cursor=None, job=job
            )

        reviews, next_cursor = await self.reviews.list_page(
            platform, product_id, limit=review_limit, cursor=review_cursor
        )
        return CollectionResult(
            status="stale",
            product=product,
            reviews=reviews,
            reviews_next_cursor=next_cursor,
            job=job,
        )

    def _is_fresh(self, product: ProductRow, now: datetime | None = None) -> bool:
        """상품과 리뷰가 '둘 다' 신선할 때만 신선하다고 본다.

        상품만 보고 판단하면, 리뷰 수집이 실패한 부분 실패(partial) 상품이 TTL이
        끝날 때까지 리뷰 없이 fresh로 굳어 재수집이 막힌다.
        """
        now = now or datetime.now(UTC)
        if not self._within(product.last_collected_at, self.settings.product_ttl_seconds, now):
            return False
        return self._within(
            product.reviews_last_collected_at, self.settings.review_ttl_seconds, now
        )

    @staticmethod
    def _within(moment: datetime | None, ttl_seconds: int, now: datetime) -> bool:
        # 한 번도 성공하지 못했으면(None) 신선하지 않다.
        if moment is None:
            return False
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        return now - moment < timedelta(seconds=ttl_seconds)
