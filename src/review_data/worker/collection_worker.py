"""
collection_jobs 를 claim 해서 collector 를 실행하는 워커.

이 모듈이 지키는 두 가지 규칙:

1. 네트워크 수집 중에는 DB 트랜잭션을 들고 있지 않는다.
   claim / 결과 저장 / 완료 기록을 각각 짧은 트랜잭션으로 나눈다. 한 트랜잭션으로
   묶으면 느린 수집 하나가 커넥션과 row lock 을 수 분씩 잡아 동시 워커 수를 올릴 수 없다.
2. product / review 는 서로 독립적으로 성공·실패를 기록한다(부분 실패 정책).
   하나가 실패해도 다른 하나는 저장되어야 한다.

수집이 lease 보다 길어질 수 있으므로 수집 중에는 heartbeat 로 lease 를 연장한다.
소유권을 잃은 뒤의 완료 기록은 repository 쪽 소유권 조건에서 거부된다.
"""

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from review_data.core.base import BaseCollector
from review_data.core.browser import BrowserCollector
from review_data.core.db.analysis_repository import AnalysisRepository
from review_data.core.db.models import CollectionJob
from review_data.core.db.repository import (
    DEFAULT_LEASE_SECONDS,
    DEFAULT_PLATFORM_CAP,
    CollectionJobRepository,
    ProductRepository,
    ReviewRepository,
)
from review_data.core.discovery import discover
from review_data.core.exceptions import CollectorError
from review_data.core.models import Product, Review
from review_data.core.settings import Settings, get_settings

logger = logging.getLogger(__name__)

POLL_INTERVAL = 5.0

SessionFactory = async_sessionmaker[AsyncSession]


@dataclass(frozen=True)
class _Claim:
    """claim 한 job 의 스냅샷. ORM 객체를 트랜잭션 밖으로 들고 다니지 않기 위함."""

    job_id: int
    platform: str
    product_id: str
    worker_id: str


@dataclass
class _Collected:
    """수집 단계 결과. None 은 '수집 자체를 못 했다'는 뜻이라 빈 리스트와 구분한다."""

    product: Product | None = None
    reviews: list[Review] | None = None
    errors: list[str] = field(default_factory=list)


def _is_browser_based(collector_cls: type[BaseCollector]) -> bool:
    """Chromium 을 띄우는 collector 인지. 플랫폼 이름을 하드코딩하지 않기 위해 클래스로 판별한다."""
    return issubclass(collector_cls, BrowserCollector)


def _platform_caps(registry: dict[str, type[BaseCollector]], settings: Settings) -> dict[str, int]:
    """플랫폼별 동시 실행 상한. 브라우저 기반은 자원을 훨씬 많이 쓰므로 따로 잡는다."""
    return {
        platform: (
            settings.max_concurrent_browser_jobs_per_platform
            if _is_browser_based(cls)
            else settings.max_concurrent_jobs_per_platform
        )
        for platform, cls in registry.items()
    }


def _collect_timeout(collector_cls: type[BaseCollector], settings: Settings) -> float:
    if _is_browser_based(collector_cls):
        return settings.browser_collect_timeout_seconds
    return settings.collect_timeout_seconds


async def run_once(
    session_factory: SessionFactory,
    worker_id: str,
    *,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    settings: Settings | None = None,
) -> bool:
    """job 하나를 claim 해서 처리한다. 처리할 job이 없으면 False를 반환한다."""
    settings = settings or get_settings()
    await _fail_exhausted(session_factory)

    registry, _ = discover()
    claim = await _claim(
        session_factory,
        worker_id,
        lease_seconds,
        platform_caps=_platform_caps(registry, settings),
        default_cap=settings.max_concurrent_jobs_per_platform,
    )
    if claim is None:
        return False

    collector_cls = registry.get(claim.platform)
    if collector_cls is None:
        await _finish(
            session_factory,
            claim,
            product_status="failed",
            review_status="failed",
            errors=[f"'{claim.platform}' collector가 등록되어 있지 않습니다."],
        )
        return True

    timeout = _collect_timeout(collector_cls, settings)
    heartbeat = asyncio.create_task(_heartbeat(session_factory, claim, lease_seconds))
    try:
        # 멈춘 브라우저 페이지 하나가 워커를 영원히 붙잡지 않게 한다. heartbeat 가
        # lease 를 계속 연장하므로 타임아웃이 없으면 job 이 끝나지 않는다.
        collected = await asyncio.wait_for(_collect(collector_cls, claim, settings), timeout)
    except TimeoutError:
        collected = _Collected(errors=[f"수집 시간 초과 ({timeout:.0f}초)"])
        logger.warning(
            "[%s/%s] 수집이 %.0f초를 넘겨 중단했습니다.",
            claim.platform,
            claim.product_id,
            timeout,
        )
    finally:
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat

    product_status, review_status, errors = await _persist(
        session_factory, claim, collected, settings=settings
    )
    await _finish(
        session_factory,
        claim,
        product_status=product_status,
        review_status=review_status,
        errors=errors,
    )
    return True


async def run_forever(
    session_factory: SessionFactory,
    worker_id: str,
    *,
    poll_interval: float = POLL_INTERVAL,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    settings: Settings | None = None,
) -> None:
    """처리할 job 이 나올 때까지 폴링하며 계속 돈다.

    job 하나가 예상치 못하게 실패해도 워커 전체를 내리지 않는다 — 한 플랫폼의
    문제가 다른 플랫폼 수집까지 멈추면 안 되기 때문이다.
    """
    logger.info("워커 시작 (id=%s)", worker_id)
    while True:
        try:
            processed = await run_once(
                session_factory, worker_id, lease_seconds=lease_seconds, settings=settings
            )
        except Exception:  # noqa: BLE001 - 워커는 어떤 job 오류에도 계속 살아 있어야 한다
            logger.exception("job 처리 중 예상치 못한 오류. 워커는 계속 실행합니다.")
            processed = False
        if not processed:
            await asyncio.sleep(poll_interval)


async def _fail_exhausted(session_factory: SessionFactory) -> None:
    """시도 상한을 채운 좀비 job 정리. 실패해도 이번 수집 자체는 계속 진행한다."""
    try:
        async with session_factory() as session:
            reaped = await CollectionJobRepository(session).fail_exhausted()
            await session.commit()
    except SQLAlchemyError:
        logger.warning("시도 상한 초과 job 정리 실패", exc_info=True)
        return
    if reaped:
        logger.info("시도 상한을 넘긴 job %d건을 failed 로 정리했습니다.", reaped)


async def _claim(
    session_factory: SessionFactory,
    worker_id: str,
    lease_seconds: int,
    *,
    platform_caps: dict[str, int] | None = None,
    default_cap: int = DEFAULT_PLATFORM_CAP,
) -> _Claim | None:
    async with session_factory() as session:
        job = await CollectionJobRepository(session).claim_one(
            worker_id,
            lease_seconds,
            platform_caps=platform_caps,
            default_cap=default_cap,
        )
        if job is None:
            return None
        claim = _Claim(
            job_id=job.id,
            platform=job.platform,
            product_id=job.product_id,
            worker_id=worker_id,
        )
        # claim 을 먼저 커밋해야 다른 워커가 이 job 을 다시 집어가지 않는다.
        await session.commit()
    return claim


async def _heartbeat(session_factory: SessionFactory, claim: _Claim, lease_seconds: int) -> None:
    """수집이 길어져도 lease 가 만료되지 않도록 주기적으로 연장한다.

    소유권을 잃으면 조용히 멈춘다. 뒤이은 완료 기록은 어차피 소유권 조건에서 거부되므로
    여기서 수집을 강제로 중단시키지는 않는다.
    """
    interval = max(lease_seconds / 3, 1.0)
    while True:
        await asyncio.sleep(interval)
        try:
            async with session_factory() as session:
                renewed = await CollectionJobRepository(session).renew_lease(
                    claim.job_id, claim.worker_id, lease_seconds
                )
                await session.commit()
        except SQLAlchemyError:
            logger.warning("[job %s] lease 연장 중 DB 오류", claim.job_id, exc_info=True)
            return
        if not renewed:
            logger.warning(
                "[job %s] lease 소유권을 잃었습니다. 이 워커의 결과는 버려집니다.",
                claim.job_id,
            )
            return


async def _collect(
    collector_cls: type[BaseCollector], claim: _Claim, settings: Settings
) -> _Collected:
    """네트워크 수집만 수행한다. 이 함수는 DB 를 건드리지 않는다."""
    collected = _Collected()
    try:
        async with collector_cls() as collector:
            try:
                collected.product = await collector.get_product(claim.product_id)
            except CollectorError as exc:
                collected.errors.append(f"product: {exc}")
                logger.warning("[%s/%s] 상품 수집 실패: %s", claim.platform, claim.product_id, exc)

            try:
                collected.reviews = await collector.get_reviews(
                    claim.product_id,
                    limit=(
                        settings.browser_review_collect_limit
                        if _is_browser_based(collector_cls)
                        else settings.review_collect_limit
                    ),
                )
            except CollectorError as exc:
                collected.errors.append(f"review: {exc}")
                logger.warning("[%s/%s] 리뷰 수집 실패: %s", claim.platform, claim.product_id, exc)
    except Exception as exc:  # noqa: BLE001 - job 을 실패로 남기고 워커는 계속 돈다
        # 메시지가 비어 있는 예외(타임아웃 등)도 있어서 종류를 함께 남긴다.
        collected.errors.append(f"unexpected: {type(exc).__name__}: {exc}")
        logger.exception(
            "[%s/%s] collector 실행 중 예상치 못한 오류", claim.platform, claim.product_id
        )
    return collected


async def _persist(
    session_factory: SessionFactory,
    claim: _Claim,
    collected: _Collected,
    *,
    settings: Settings | None = None,
) -> tuple[str, str, list[str]]:
    """수집 결과를 각각 독립된 트랜잭션으로 저장한다.

    product 저장이 실패해도 review 저장을 시도하고, 그 반대도 마찬가지다. 실패한
    트랜잭션의 세션을 다시 쓰지 않으므로 뒤따르는 완료 기록이 말려들지 않는다.
    """
    settings = settings or get_settings()
    errors = list(collected.errors)
    product_status = "failed"
    review_status = "failed"

    if collected.product is not None:
        try:
            async with session_factory() as session:
                await ProductRepository(session).upsert(collected.product)
                if not await _owns_collection(session, claim):
                    await session.rollback()
                    return "failed", "failed", ["수집 작업 소유권 상실"]
                await session.commit()
            product_status = "succeeded"
        except SQLAlchemyError as exc:
            errors.append(f"product 저장 실패: {exc}")
            logger.warning(
                "[%s/%s] 상품 저장 실패", claim.platform, claim.product_id, exc_info=True
            )

    if collected.reviews is not None:
        try:
            async with session_factory() as session:
                await AnalysisRepository(session).lock_product(claim.platform, claim.product_id)
                if not await _owns_collection(session, claim):
                    return product_status, "failed", [*errors, "수집 작업 소유권 상실"]
                await ReviewRepository(session).upsert_many(
                    claim.platform, claim.product_id, collected.reviews
                )
                # 0건이어도 '수집 성공'이므로 기록한다. 이걸 빠뜨리면 리뷰 없는
                # 상품이 매번 재수집 대상이 된다.
                await ProductRepository(session).mark_reviews_collected(
                    claim.platform, claim.product_id
                )
                await AnalysisRepository(session).enqueue(
                    claim.platform, claim.product_id, settings, trigger=claim.job_id
                )
                await session.commit()
            review_status = "succeeded"
        except SQLAlchemyError as exc:
            errors.append(f"review 저장 실패: {exc}")
            logger.warning(
                "[%s/%s] 리뷰 저장 실패", claim.platform, claim.product_id, exc_info=True
            )

    return product_status, review_status, errors


async def _finish(
    session_factory: SessionFactory,
    claim: _Claim,
    *,
    product_status: str,
    review_status: str,
    errors: list[str],
) -> None:
    try:
        async with session_factory() as session:
            owned = await CollectionJobRepository(session).mark_completed(
                claim.job_id,
                worker_id=claim.worker_id,
                product_status=product_status,
                review_status=review_status,
                error="; ".join(errors) if errors else None,
            )
            await session.commit()
    except SQLAlchemyError:
        logger.exception("[job %s] 완료 상태 기록 실패", claim.job_id)
        return

    if not owned:
        logger.warning("[job %s] lease 를 잃어 완료 상태를 기록하지 않았습니다.", claim.job_id)


async def _owns_collection(session, claim: _Claim) -> bool:
    job = await session.scalar(
        select(CollectionJob)
        .where(
            CollectionJob.id == claim.job_id,
            CollectionJob.status == "running",
            CollectionJob.locked_by == claim.worker_id,
            CollectionJob.lease_expires_at > datetime.now(UTC),
        )
        .with_for_update(key_share=True)
    )
    return job is not None
