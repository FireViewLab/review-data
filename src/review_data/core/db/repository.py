"""
products/reviews/collection_jobs 에 대한 DB 접근 계층.

Pydantic Product/Review(core/models.py) ↔ ORM row(core/db/models.py) 변환은
이 모듈이 전담한다. 상위 계층(service)은 SQLAlchemy를 직접 알 필요가 없다.
"""

import base64
import hashlib
import json
from datetime import UTC, datetime

from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import text

from review_data.core.db.models import CollectionJob, ProductRow, ReviewRow
from review_data.core.models import Product, Review

# 워커가 job 하나를 붙잡고 있을 수 있는 기본 시간. 이 시간을 넘기면 다른 워커가
# 죽은 워커로 간주하고 job을 회수한다. 수집이 길어질 때는 heartbeat로 연장한다.
DEFAULT_LEASE_SECONDS = 120

# 플랫폼별 상한이 따로 지정되지 않았을 때 쓰는 기본 동시 실행 상한.
DEFAULT_PLATFORM_CAP = 4

# ON CONFLICT DO UPDATE 로 덮어쓸 컬럼. updated_at 이 빠지면 재수집해도 갱신되지
# 않는다 — 모델의 onupdate 는 ORM UPDATE 에만 걸린다.
_REVIEW_UPDATE_COLUMNS = (
    "content",
    "rating",
    "author",
    "written_at",
    "option",
    "images",
    "helpful_count",
    "last_collected_at",
    "updated_at",
)


def _encode_review_cursor(written_at: datetime | None, review_id: str) -> str:
    payload = json.dumps([written_at.isoformat() if written_at else None, review_id])
    return base64.urlsafe_b64encode(payload.encode()).decode()


class InvalidCursorError(ValueError):
    """cursor 가 손상됐거나 이 API 가 만든 값이 아닐 때.

    cursor 는 클라이언트가 그대로 돌려주는 값이라 얼마든지 변조될 수 있다. 디코딩
    실패를 그대로 흘려보내면 클라이언트 입력 오류가 500 으로 보고된다.
    """


def _decode_review_cursor(cursor: str) -> tuple[datetime | None, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        written_at_raw, review_id = json.loads(raw)
        written_at = datetime.fromisoformat(written_at_raw) if written_at_raw else None
    except (ValueError, TypeError, UnicodeDecodeError) as exc:
        raise InvalidCursorError(str(exc)) from exc

    if not isinstance(review_id, str):
        raise InvalidCursorError("review_id 가 문자열이 아닙니다.")
    if written_at is not None and written_at.tzinfo is None:
        # 우리가 만든 cursor 는 항상 timezone 을 포함한다. 없으면 변조된 값이며,
        # 그대로 쓰면 DB 의 timestamptz 와 비교하다 터진다.
        raise InvalidCursorError("timezone 정보가 없는 시각입니다.")
    return written_at, review_id


def validate_review_cursor(cursor: str) -> None:
    """cursor 형식만 검증한다.

    조회 결과가 없더라도(상품이 아직 없더라도) 잘못된 cursor 는 잘못된 입력이다.
    list_page 안에서만 검증하면 cold start 경로에서는 검증이 통째로 생략된다.
    """
    _decode_review_cursor(cursor)


class ProductRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, platform: str, product_id: str) -> ProductRow | None:
        return await self.session.get(ProductRow, (platform, product_id))

    async def upsert(self, product: Product) -> None:
        values = {
            "name": product.name,
            "url": product.url,
            "brand": product.brand,
            "manufacturer": product.manufacturer,
            "seller": product.seller,
            "price": product.price,
            "thumbnail_url": product.thumbnail_url,
            "category": product.category,
            "review_count": product.review_count,
            "rating": product.rating,
            "last_collected_at": datetime.now(UTC),
            # ON CONFLICT DO UPDATE 에는 모델의 onupdate 가 적용되지 않는다. 직접 넣지
            # 않으면 몇 번을 다시 수집해도 updated_at 이 최초 insert 시각에 머문다.
            "updated_at": datetime.now(UTC),
        }
        stmt = pg_insert(ProductRow).values(
            platform=product.platform, product_id=product.product_id, **values
        )
        stmt = stmt.on_conflict_do_update(index_elements=["platform", "product_id"], set_=values)
        await self.session.execute(stmt)

    async def mark_reviews_collected(self, platform: str, product_id: str) -> None:
        """리뷰 수집이 성공했음을 기록한다. 수집 결과가 0건이어도 호출한다.

        '리뷰가 없는 상품'과 '리뷰를 못 가져온 상품'을 구분하기 위한 표시다. 후자는
        이 시각이 갱신되지 않아 다음 조회에서 재수집 대상이 된다.
        """
        stmt = (
            update(ProductRow)
            .where(ProductRow.platform == platform, ProductRow.product_id == product_id)
            .values(reviews_last_collected_at=datetime.now(UTC), updated_at=datetime.now(UTC))
        )
        await self.session.execute(stmt)


class ReviewRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_page(
        self,
        platform: str,
        product_id: str,
        limit: int = 20,
        cursor: str | None = None,
    ) -> tuple[list[ReviewRow], str | None]:
        """written_at 내림차순 cursor 페이지네이션. limit+1개를 가져와 다음 페이지 여부를 판단."""
        stmt = select(ReviewRow).where(
            ReviewRow.platform == platform, ReviewRow.product_id == product_id
        )
        if cursor is not None:
            written_at, review_id = _decode_review_cursor(cursor)
            if written_at is not None:
                stmt = stmt.where(
                    or_(
                        ReviewRow.written_at < written_at,
                        and_(ReviewRow.written_at == written_at, ReviewRow.review_id < review_id),
                        ReviewRow.written_at.is_(None),
                    )
                )
            else:
                stmt = stmt.where(ReviewRow.written_at.is_(None), ReviewRow.review_id < review_id)

        stmt = stmt.order_by(
            ReviewRow.written_at.desc().nulls_last(), ReviewRow.review_id.desc()
        ).limit(limit + 1)

        result = await self.session.execute(stmt)
        rows = list(result.scalars())

        next_cursor = None
        if len(rows) > limit:
            rows = rows[:limit]
            last = rows[-1]
            next_cursor = _encode_review_cursor(last.written_at, last.review_id)
        return rows, next_cursor

    async def upsert_many(self, platform: str, product_id: str, reviews: list[Review]) -> None:
        if not reviews:
            return
        await self.session.scalar(
            select(ProductRow)
            .where(ProductRow.platform == platform, ProductRow.product_id == product_id)
            .with_for_update()
        )
        now = datetime.now(UTC)
        rows = [
            {
                "platform": platform,
                "product_id": product_id,
                "review_id": review.review_id,
                "content": review.content,
                "rating": review.rating,
                "author": review.author,
                "written_at": review.written_at,
                "option": review.option,
                "images": review.images,
                "helpful_count": review.helpful_count,
                "last_collected_at": now,
                "updated_at": now,
            }
            for review in reviews
        ]
        stmt = pg_insert(ReviewRow).values(rows)
        update_cols = {col: getattr(stmt.excluded, col) for col in _REVIEW_UPDATE_COLUMNS}
        stmt = stmt.on_conflict_do_update(
            index_elements=["platform", "product_id", "review_id"], set_=update_cols
        )
        await self.session.execute(stmt)
        await self.session.execute(
            update(ProductRow)
            .where(ProductRow.platform == platform, ProductRow.product_id == product_id)
            .values(analysis_input_hash=None)
        )


class CollectionJobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, job_id: int) -> CollectionJob | None:
        return await self.session.get(CollectionJob, job_id)

    async def get_active(self, platform: str, product_id: str) -> CollectionJob | None:
        stmt = select(CollectionJob).where(
            CollectionJob.platform == platform,
            CollectionJob.product_id == product_id,
            CollectionJob.status.in_(("pending", "running")),
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def count_pending(self) -> int:
        """아직 워커가 집지 않은 job 수. 스케줄러가 큐를 넘치게 하지 않으려고 본다."""
        stmt = (
            select(func.count()).select_from(CollectionJob).where(CollectionJob.status == "pending")
        )
        return (await self.session.execute(stmt)).scalar_one()

    async def list_refresh_candidates(
        self,
        *,
        product_cutoff: datetime,
        review_cutoff: datetime,
        failure_cutoff: datetime,
        limit: int,
        excluded_platforms: tuple[str, ...] = (),
    ) -> list[tuple[str, str]]:
        """다시 수집할 상품을 오래된 순으로 고른다.

        낡았는지는 조회 API(CollectionService._is_fresh)와 같은 기준으로 본다. 기준이
        다르면 스케줄러가 돌았는데도 조회 때 stale 로 나온다.

        최근에 실패한 상품은 failure_cutoff 이후로 다시 예약하지 않는다. 차단된 플랫폼의
        상품이 주기마다 예약되어 같은 사이트를 계속 두드리는 것을 막는다.
        """
        same_product = and_(
            CollectionJob.platform == ProductRow.platform,
            CollectionJob.product_id == ProductRow.product_id,
        )
        has_active_job = exists().where(
            same_product, CollectionJob.status.in_(("pending", "running"))
        )
        failed_recently = exists().where(
            same_product,
            CollectionJob.status.in_(("failed", "partial")),
            # 완료 시각이 비어 있는 실패 이력(수동 정리 등)이 대기를 건너뛰지 않게 한다.
            func.coalesce(CollectionJob.completed_at, CollectionJob.updated_at) > failure_cutoff,
        )
        stmt = (
            select(ProductRow.platform, ProductRow.product_id)
            .where(
                # 조회 API 는 "지난 시간 < TTL" 일 때만 신선하다고 본다. 정확히 TTL 인
                # 상품도 낡은 것이므로 <= 로 맞춘다.
                or_(
                    ProductRow.last_collected_at <= product_cutoff,
                    ProductRow.reviews_last_collected_at.is_(None),
                    ProductRow.reviews_last_collected_at <= review_cutoff,
                ),
                ProductRow.platform.not_in(excluded_platforms),
                ~has_active_job,
                ~failed_recently,
            )
            # 리뷰를 한 번도 못 받은 상품이 가장 불완전하므로 먼저 채운다.
            .order_by(
                ProductRow.reviews_last_collected_at.asc().nulls_first(),
                ProductRow.last_collected_at.asc(),
                ProductRow.platform,
                ProductRow.product_id,
            )
            .limit(limit)
        )
        return [(row.platform, row.product_id) for row in await self.session.execute(stmt)]

    async def create_or_get_active(
        self, platform: str, product_id: str, *, requested_by: str | None = None
    ) -> tuple[CollectionJob, bool]:
        """활성 job이 있으면 그걸 반환(created=False), 없으면 새로 만든다(created=True).

        동시 요청에도 partial unique index(uq_collection_jobs_inflight)가 중복 생성을
        막아준다 — SAVEPOINT 안에서 insert 해보고 충돌하면 기존 job을 재조회한다.
        """
        idempotency_key = hashlib.sha256(f"{platform}:{product_id}".encode()).hexdigest()[:32]
        try:
            async with self.session.begin_nested():
                job = CollectionJob(
                    platform=platform,
                    product_id=product_id,
                    idempotency_key=idempotency_key,
                    requested_by=requested_by,
                )
                self.session.add(job)
                await self.session.flush()
            return job, True
        except IntegrityError:
            existing = await self.get_active(platform, product_id)
            if existing is None:
                raise
            return existing, False

    async def claim_one(
        self,
        worker_id: str,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        *,
        platform_caps: dict[str, int] | None = None,
        default_cap: int = DEFAULT_PLATFORM_CAP,
    ) -> CollectionJob | None:
        """PENDING이거나 lease가 만료된 RUNNING job 하나를 원자적으로 가져간다.

        시도 횟수를 이미 채운 job은 후보에서 뺀다. 그러지 않으면 워커를 계속 죽이는
        job 하나가 영원히 재시도되며 큐를 막는다. 빠진 job은 fail_exhausted()가 정리한다.

        platform_caps 로 플랫폼별 동시 실행 상한을 건다. 브라우저를 띄우는 collector 가
        여러 워커에서 한꺼번에 도는 것을 막기 위한 것이다. 상한 검사와 row 잠금이 한
        트랜잭션 안에서 원자적이지는 않아서, 여러 워커가 정확히 같은 순간에 claim 하면
        상한을 1~2개 넘길 수 있다. 자원 보호가 목적이라 이 정도 오차는 허용한다.
        """
        stmt = text(
            """
            UPDATE collection_jobs
            SET status = 'running',
                locked_by = :worker_id,
                locked_at = now(),
                lease_expires_at = now() + make_interval(secs => :lease_seconds),
                attempt_count = attempt_count + 1,
                updated_at = now()
            WHERE id = (
                SELECT j.id FROM collection_jobs j
                WHERE j.attempt_count < j.max_attempts
                  AND (
                      j.status = 'pending'
                      OR (j.status = 'running' AND j.lease_expires_at < now())
                  )
                  AND (
                      SELECT count(*) FROM collection_jobs busy
                      WHERE busy.platform = j.platform
                        AND busy.status = 'running'
                        AND busy.lease_expires_at > now()
                  ) < coalesce(
                      (cast(:platform_caps as jsonb) ->> j.platform)::int, :default_cap
                  )
                ORDER BY j.created_at
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            RETURNING id
            """
        )
        result = await self.session.execute(
            stmt,
            {
                "worker_id": worker_id,
                "lease_seconds": lease_seconds,
                "platform_caps": json.dumps(platform_caps or {}),
                "default_cap": default_cap,
            },
        )
        row = result.first()
        if row is None:
            return None
        # 원시 SQL UPDATE는 세션의 identity map을 갱신하지 않으므로, 이미 로드돼
        # 있던 객체가 있다면 populate_existing으로 DB 최신값을 강제로 다시 읽는다.
        return await self.session.get(CollectionJob, row.id, populate_existing=True)

    async def renew_lease(
        self, job_id: int, worker_id: str, lease_seconds: int = DEFAULT_LEASE_SECONDS
    ) -> bool:
        """아직 이 워커가 소유자일 때만 lease를 연장하고, 연장했는지 여부를 반환한다.

        이미 lease가 만료돼 다른 워커가 가져갔다면 False다. 호출자는 이때 작업을
        포기해야 한다 — 계속 진행해봐야 완료 기록이 거부된다.
        """
        stmt = text(
            """
            UPDATE collection_jobs
            SET lease_expires_at = now() + make_interval(secs => :lease_seconds),
                updated_at = now()
            WHERE id = :job_id
              AND locked_by = :worker_id
              AND status = 'running'
              AND lease_expires_at > now()
            """
        )
        result = await self.session.execute(
            stmt,
            {"job_id": job_id, "worker_id": worker_id, "lease_seconds": lease_seconds},
        )
        return result.rowcount == 1

    async def mark_completed(
        self,
        job_id: int,
        *,
        worker_id: str,
        product_status: str,
        review_status: str,
        error: str | None = None,
    ) -> bool:
        """소유권이 유지될 때만 완료 상태로 전이하고, 전이했는지 여부를 반환한다.

        lease가 만료돼 다른 워커가 같은 job을 이미 가져간 뒤라면 아무것도 바꾸지 않는다.
        늦게 끝난 워커가 새 워커의 진행 상태를 덮어쓰는 것을 막기 위함이다.
        """
        if product_status == "succeeded" and review_status == "succeeded":
            status = "succeeded"
        elif product_status == "failed" and review_status == "failed":
            status = "failed"
        else:
            status = "partial"

        stmt = text(
            """
            UPDATE collection_jobs
            SET status = :status,
                product_status = :product_status,
                review_status = :review_status,
                last_error = :error,
                completed_at = now(),
                updated_at = now()
            WHERE id = :job_id
              AND locked_by = :worker_id
              AND status = 'running'
              AND lease_expires_at > now()
            """
        )
        result = await self.session.execute(
            stmt,
            {
                "job_id": job_id,
                "worker_id": worker_id,
                "status": status,
                "product_status": product_status,
                "review_status": review_status,
                "error": error,
            },
        )
        return result.rowcount == 1

    async def fail_exhausted(self) -> int:
        """시도 상한을 채운 채 lease가 만료된 job을 failed로 정리하고 건수를 반환한다.

        claim_one이 이런 job을 더 이상 집지 않으므로, 정리해주지 않으면 running 상태로
        영원히 남는다.
        """
        stmt = text(
            """
            UPDATE collection_jobs
            SET status = 'failed',
                completed_at = now(),
                updated_at = now(),
                last_error = coalesce(last_error || ' / ', '') || '최대 시도 횟수 초과'
            WHERE status = 'running'
              AND lease_expires_at < now()
              AND attempt_count >= max_attempts
            """
        )
        result = await self.session.execute(stmt)
        return result.rowcount
