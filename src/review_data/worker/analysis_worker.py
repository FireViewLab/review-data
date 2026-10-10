"""저장된 입력으로 분석하고 검증된 결과를 한 번에 게시한다."""

import asyncio
import contextlib
import logging

from sqlalchemy.exc import SQLAlchemyError

from review_data.core.analysis_stream import AnalysisStreamClient, AnalysisStreamError
from review_data.core.db.analysis_control import effective_analysis_settings
from review_data.core.db.analysis_repository import AnalysisRepository
from review_data.core.settings import Settings, get_settings

logger = logging.getLogger(__name__)


async def _heartbeat(factory, claim, worker_id, lease_seconds):
    while True:
        await asyncio.sleep(max(lease_seconds / 3, 0.05))
        async with factory() as session:
            owned = await AnalysisRepository(session).renew(claim["id"], worker_id, lease_seconds)
            await session.commit()
        if not owned:
            return


async def run_once(
    factory,
    worker_id: str,
    *,
    settings: Settings | None = None,
    lease_seconds: float = 60,
    client=None,
) -> bool:
    settings = settings or get_settings()
    if not settings.ai_analysis_enabled:
        return False
    async with factory() as session:
        settings = await effective_analysis_settings(session, settings)
        claim = await AnalysisRepository(session).claim(worker_id, lease_seconds)
        await session.commit()
    if claim is None:
        return False
    payload = claim["input_payload"] or {}
    if (
        settings.ai_model_version and payload.get("model_version") != settings.ai_model_version
    ) or (
        settings.ai_policy_version and payload.get("policy_version") != settings.ai_policy_version
    ):
        # 설정 전환 전에 쌓인 요청을 새 버전 큐로 바꾼다. 오래된 모델로 POST하지 않는다.
        async with factory() as session:
            repo = AnalysisRepository(session)
            await repo.lock_product(claim["platform"], claim["product_id"])
            job = await repo._owned(claim["id"], worker_id)
            if job is not None:
                job.status = "stale"
                await session.flush()
                await repo.enqueue(claim["platform"], claim["product_id"], settings)
                await session.commit()
        return True
    heartbeat = None
    request = None
    try:
        if not payload.get("reviews") or not claim["input_hash"]:
            raise AnalysisStreamError("저장된 분석 입력이 없습니다.")
        client = client or AnalysisStreamClient(
            settings.ai_stream_url,
            token=settings.ai_internal_token.get_secret_value()
            if settings.ai_internal_token
            else None,
            timeout=settings.ai_timeout_seconds,
            model_version=payload.get("model_version"),
            policy_version=payload.get("policy_version"),
        )
        heartbeat = asyncio.create_task(_heartbeat(factory, claim, worker_id, lease_seconds))
        request = asyncio.create_task(
            client.analyze(
                platform=claim["platform"],
                product_id=claim["product_id"],
                reviews=payload["reviews"],
                job_id=claim["id"],
                input_hash=claim["input_hash"],
            )
        )
        finished, _ = await asyncio.wait(
            [heartbeat, request],
            timeout=settings.ai_timeout_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if heartbeat in finished:
            # 소유권 상실 또는 갱신 실패 이후에는 응답을 게시하지 않는다.
            heartbeat.result()
            return True
        if request not in finished:
            raise TimeoutError
        response = request.result()
        async with factory() as session:
            await AnalysisRepository(session).complete(claim, worker_id, response, settings)
            await session.commit()
    except (AnalysisStreamError, TimeoutError, SQLAlchemyError) as exc:
        retryable = not isinstance(exc, AnalysisStreamError) or exc.retryable
        async with factory() as session:
            await AnalysisRepository(session).fail(claim["id"], worker_id, retryable)
            await session.commit()
        logger.warning("분석 작업 %s 처리 실패 (재시도=%s)", claim["id"], retryable)
    except Exception:  # noqa: BLE001
        async with factory() as session:
            await AnalysisRepository(session).fail(claim["id"], worker_id, False)
            await session.commit()
        logger.warning("분석 작업 %s 응답 검증 실패", claim["id"])
    finally:
        for task in (request, heartbeat):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
    return True


async def run_forever(
    factory, worker_id: str, *, poll_interval: float = 5, settings: Settings | None = None
):
    settings = settings or get_settings()
    if not settings.ai_analysis_enabled:
        logger.info("분석 연결 비활성화: AI 설정과 계약 확정 후 활성화한다.")
    while True:
        try:
            from review_data.core.service.analysis_refresh import AnalysisRefreshService

            async with factory() as session:
                await AnalysisRefreshService(session, settings).run_once()
                await session.commit()
        except Exception:
            logger.warning("재분석 대상 예약 실패: 다음 주기에 다시 확인한다.")
        try:
            processed = await run_once(factory, worker_id, settings=settings)
        except Exception:  # noqa: BLE001
            logger.warning("분석 워커 처리 실패: 다음 주기에 다시 확인한다.")
            processed = False
        if not processed:
            await asyncio.sleep(poll_interval)
