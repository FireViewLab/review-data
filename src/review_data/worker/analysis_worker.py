"""저장된 입력으로 분석하고 검증된 결과를 한 번에 게시한다."""

import asyncio
import contextlib
import logging
import time

from sqlalchemy.exc import SQLAlchemyError

from review_data.core.analysis_stream import AnalysisStreamClient, AnalysisStreamError
from review_data.core.db.analysis_control import effective_analysis_settings
from review_data.core.db.analysis_repository import AnalysisRepository
from review_data.core.db.pipeline_control import analysis_stopped
from review_data.core.settings import Settings, get_settings

logger = logging.getLogger(__name__)


# AI 서버가 응답하지 못한 실패. 응답 검증 실패나 처리 중 충돌은 AI가 살아 있다는 뜻이다.
OUTAGE_KINDS = ("http", "transport", "timeout")


class OutageGuard:
    """AI 서버 장애가 이어지면 요청을 잠시 멈춘다. 워커 프로세스마다 따로 판단한다."""

    def __init__(self, settings: Settings, clock=time.monotonic):
        self.threshold = settings.ai_outage_failure_threshold
        self.base_pause = settings.ai_outage_pause_seconds
        self.max_pause = max(settings.ai_outage_max_pause_seconds, self.base_pause)
        self.clock = clock
        self.failures = 0
        self.pause = self.base_pause
        self.until = 0.0
        self.tripped = False

    def wait_seconds(self) -> float:
        return max(self.until - self.clock(), 0.0)

    def record(self, outage: bool) -> None:
        if not outage:
            if self.tripped:
                logger.info("AI 서버 응답 회복: 분석 요청을 다시 보낸다.")
            self.tripped = False
            self.failures = 0
            self.pause = self.base_pause
            return
        self.failures += 1
        if self.failures < self.threshold:
            return
        self.until = self.clock() + self.pause
        self.tripped = True
        logger.warning(
            "AI 서버 장애가 이어져 분석 요청을 %d초 멈춘다 (연속 실패 %d건)",
            self.pause,
            self.failures,
        )
        self.pause = min(self.pause * 2, self.max_pause)
        # 기다린 뒤에는 한 건만 보내 보고, 그것도 실패하면 바로 다시 멈춘다.
        self.failures = self.threshold - 1


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
    backfill_only: bool = False,
    guard: OutageGuard | None = None,
) -> bool:
    settings = settings or get_settings()
    if not settings.ai_analysis_enabled:
        return False
    async with factory() as session:
        # AI 서버 장애 때 운영자가 멈추면 작업을 꺼내지 않는다. 대기열은 그대로 남는다.
        if await analysis_stopped(session):
            return False
        settings = await effective_analysis_settings(session, settings)
        claim = await AnalysisRepository(session).claim(
            worker_id,
            lease_seconds,
            backfill_only=backfill_only,
            reanalysis_share=settings.analysis_reanalysis_share,
        )
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
        if guard is not None:
            guard.record(False)
    except (AnalysisStreamError, TimeoutError, SQLAlchemyError) as exc:
        retryable = not isinstance(exc, AnalysisStreamError) or exc.retryable
        if isinstance(exc, AnalysisStreamError):
            kind = exc.kind
        else:
            kind = "timeout" if isinstance(exc, TimeoutError) else "storage"
        # AI가 앞선 요청을 아직 처리 중이면 곧바로 다시 보내도 409만 반복되어 시도 횟수를
        # 소진한다. 앞선 요청이 끝날 시간을 두고 같은 작업 ID로 다시 보낸다.
        min_delay = settings.ai_timeout_seconds * 2 if kind == "conflict" else 0
        async with factory() as session:
            await AnalysisRepository(session).fail(
                claim["id"], worker_id, retryable, kind, min_delay
            )
            await session.commit()
        logger.warning("분석 작업 %s 처리 실패 (원인=%s, 재시도=%s)", claim["id"], kind, retryable)
        if guard is not None:
            guard.record(retryable and kind in OUTAGE_KINDS)
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
    factory,
    worker_id: str,
    *,
    poll_interval: float = 5,
    settings: Settings | None = None,
    backfill_only: bool = False,
):
    settings = settings or get_settings()
    if not settings.ai_analysis_enabled:
        logger.info("분석 연결 비활성화: AI 설정과 계약 확정 후 활성화한다.")
    guard = OutageGuard(settings)
    while True:
        try:
            from review_data.core.service.analysis_refresh import AnalysisRefreshService

            async with factory() as session:
                await AnalysisRefreshService(session, settings).run_once()
                await session.commit()
        except Exception:
            logger.warning("재분석 대상 예약 실패: 다음 주기에 다시 확인한다.")
        # 장애로 멈춘 동안에는 작업을 꺼내지 않아 대기 작업의 시도 횟수가 그대로 남는다.
        if wait := guard.wait_seconds():
            await asyncio.sleep(min(wait, poll_interval))
            continue
        try:
            processed = await run_once(
                factory, worker_id, settings=settings, backfill_only=backfill_only, guard=guard
            )
        except Exception:  # noqa: BLE001
            logger.warning("분석 워커 처리 실패: 다음 주기에 다시 확인한다.")
            processed = False
        if not processed:
            await asyncio.sleep(poll_interval)
