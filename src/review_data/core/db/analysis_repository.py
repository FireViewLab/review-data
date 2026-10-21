"""입력 스냅샷·작업 소유권·분석 결과의 게시를 관리한다."""

import hashlib
import json
import random
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from review_data.core.analysis_sampling import SAMPLING_VERSION, select_reviews
from review_data.core.db.analysis_control import effective_analysis_settings
from review_data.core.db.models import (
    AnalysisCampaign,
    AnalysisCampaignItem,
    AnalysisJob,
    AnalysisRecovery,
    ProductRow,
    ReviewAnalysisRow,
)
from review_data.core.db.review_identity import representative_reviews
from review_data.core.settings import Settings


def input_hash(reviews: list[dict]) -> str:
    payload = json.dumps(reviews, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def published_condition(job=AnalysisJob):
    # 예전 배포가 stale로 표시한 완료 이력도 복구한다. 미완료 작업은 선택하지 않는다.
    return or_(
        job.status == "done",
        and_(job.status == "stale", job.completed_at.is_not(None), job.result.is_not(None)),
    )


def published_order(settings: Settings):
    matches = []
    if settings.ai_model_version:
        matches.append(AnalysisJob.model_version == settings.ai_model_version)
    if settings.ai_policy_version:
        matches.append(AnalysisJob.policy_version == settings.ai_policy_version)
    return (*([and_(*matches).desc()] if matches else []), AnalysisJob.id.desc())


# last_error에는 원본 오류 대신 아래 고정 문구만 남긴다. 운영 화면은 문구로 원인을 구분한다.
FAILURE_REASONS = {
    "timeout": "AI 응답 시간 초과",
    "conflict": "AI가 같은 작업을 처리 중",
    "http": "AI 서버 HTTP 오류",
    "transport": "AI 서버 연결 실패",
    "ai_error": "AI 서버 오류 응답",
    "storage": "분석 결과 저장 실패",
    "validation": "AI 응답 검증 실패",
    "exhausted": "분석 최대 시도 횟수 초과",
}
FAILURE_KINDS = {reason: kind for kind, reason in FAILURE_REASONS.items()}

# AI 서버 쪽 장애로 볼 수 있는 실패. 응답 검증·저장 실패는 다시 보내도 같은 결과라 뺀다.
RECOVERABLE_KINDS = ("http", "transport", "timeout", "conflict", "ai_error", "exhausted")

# 상품별 최신 작업이 기간 안의 실패이고 지금 목표 버전으로 요청했던 것만 다시 보낸다. 그 뒤에
# 리뷰가 바뀌었으면 수집이 이미 새 작업을 만들었으므로 최신 작업이 아니게 된다.
_RECOVERY_CANDIDATES = """
WITH latest AS (
    SELECT DISTINCT ON (platform, product_id) id FROM analysis_jobs
    ORDER BY platform, product_id, id DESC
)
SELECT j.id, j.platform, j.product_id, j.last_error,
    (j.last_error = ANY(:labels)
        AND j.input_payload->>'model_version' IS NOT DISTINCT FROM :model
        AND j.input_payload->>'policy_version' IS NOT DISTINCT FROM :policy) AS recoverable
FROM analysis_jobs j JOIN latest USING (id)
WHERE j.status = 'failed' AND j.completed_at > :since
"""


def _recovery_params(since: datetime, settings: Settings) -> dict:
    return {
        "since": since,
        "labels": [FAILURE_REASONS[kind] for kind in RECOVERABLE_KINDS],
        "model": settings.ai_model_version,
        "policy": settings.ai_policy_version,
    }


async def failure_summary(session: AsyncSession, since: datetime, settings: Settings) -> dict:
    """기간 안에 끝난 실패 분석 작업(상품별 최신)을 원인별로 센다."""
    rows = (
        await session.execute(text(_RECOVERY_CANDIDATES), _recovery_params(since, settings))
    ).all()
    by_kind: dict[str, int] = {}
    for row in rows:
        kind = FAILURE_KINDS.get(row.last_error, "legacy_unknown")
        by_kind[kind] = by_kind.get(kind, 0) + 1
    return {
        "total": len(rows),
        "recoverable": sum(1 for row in rows if row.recoverable),
        "by_reason": [
            {"kind": kind, "count": count}
            for kind, count in sorted(by_kind.items(), key=lambda item: -item[1])
        ],
    }


async def recover_failures(
    session: AsyncSession, since: datetime, settings: Settings, hours: int | None = None
) -> int:
    """AI 쪽 원인으로 실패한 작업을 같은 입력·새 작업 ID로 다시 예약한다.

    hours 를 주면 다시 예약한 작업 목록을 기록해 화면에서 진행률을 볼 수 있게 한다.
    """
    result = await session.execute(
        text(f"""
        WITH candidates AS ({_RECOVERY_CANDIDATES})
        INSERT INTO analysis_jobs (platform, product_id, status, input_payload, input_hash,
            input_review_count, trigger_collection_job_id, available_at, attempt_count,
            max_attempts)
        SELECT j.platform, j.product_id, 'queued', j.input_payload, j.input_hash,
            j.input_review_count, j.trigger_collection_job_id, now(), 0, j.max_attempts
        FROM analysis_jobs j JOIN candidates c ON c.id = j.id
        WHERE c.recoverable AND NOT EXISTS (
            SELECT 1 FROM analysis_jobs a WHERE a.platform = j.platform
                AND a.product_id = j.product_id AND a.status IN ('queued', 'running'))
        RETURNING id
        """),
        _recovery_params(since, settings),
    )
    job_ids = sorted(row.id for row in result.all())
    if job_ids and hours is not None:
        session.add(AnalysisRecovery(hours=hours, job_ids=job_ids))
        await session.flush()
    return len(job_ids)


async def latest_recovery(session: AsyncSession, now: datetime) -> dict | None:
    """최근 7일 안의 마지막 복구에서 다시 예약한 작업들의 처리 상태."""
    recovery = await session.scalar(
        select(AnalysisRecovery)
        .where(AnalysisRecovery.created_at > now - timedelta(days=7))
        .order_by(AnalysisRecovery.id.desc())
        .limit(1)
    )
    if recovery is None:
        return None
    rows = await session.execute(
        text("""
        SELECT status, count(*) AS count,
            count(*) FILTER (WHERE status='done' AND completed_at > :recent) AS recent
        FROM analysis_jobs WHERE id = ANY(:ids) GROUP BY status
    """),
        {"ids": recovery.job_ids, "recent": now - timedelta(minutes=15)},
    )
    counts = {"done": 0, "running": 0, "queued": 0, "failed": 0, "superseded": 0}
    recent_done = 0
    for status, count, recent in rows:
        # stale 은 리뷰가 바뀌어 새 작업으로 넘어간 것이다.
        key = "superseded" if status == "stale" else status
        counts[key] = counts.get(key, 0) + count
        recent_done += recent
    remaining = counts["running"] + counts["queued"]
    if not remaining:
        estimate = 0
    elif recent_done:
        # 최근 15분 처리 속도로 남은 시간을 추정한다.
        estimate = round(remaining * 900 / recent_done)
    else:
        estimate = None  # 처리 기록이 없으면 알 수 없다.
    return {
        "id": recovery.id,
        "created_at": recovery.created_at.isoformat(),
        "hours": recovery.hours,
        "total": sum(counts.values()),
        **counts,
        "recent_done_15m": recent_done,
        "estimated_remaining_seconds": estimate,
    }


def has_prior_result(job=AnalysisJob):
    """같은 상품에 이미 게시된 분석 결과가 있는지. 없으면 첫 분석이다."""
    prior = aliased(AnalysisJob)
    return (
        select(prior.id)
        .where(
            prior.platform == job.platform,
            prior.product_id == job.product_id,
            prior.status.in_(("done", "stale")),
            prior.result.is_not(None),
        )
        .exists()
    )


class AnalysisRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def lock_product(self, platform: str, product_id: str, *, read: bool = False):
        return await self.session.scalar(
            select(ProductRow)
            .where(ProductRow.platform == platform, ProductRow.product_id == product_id)
            .with_for_update(read=read)
        )

    async def snapshot(self, platform: str, product_id: str) -> list[dict]:
        review, stmt = representative_reviews(platform, product_id)
        rows = await self.session.scalars(stmt.order_by(review.review_id))
        return [
            {
                "review_id": r.review_id,
                "content": r.content,
                "rating": float(r.rating) if r.rating is not None else None,
                "written_at": r.written_at.isoformat() if r.written_at is not None else None,
            }
            for r in rows
        ]

    @staticmethod
    def _same(job: AnalysisJob, digest: str, settings: Settings) -> bool:
        payload = job.input_payload or {}
        sampling = sampling_info(job)
        needs_sample = sampling["source_review_count"] > settings.ai_max_reviews
        selection_matches = sampling["sampled"] == needs_sample and (
            not needs_sample
            or (
                sampling.get("policy_version") == SAMPLING_VERSION
                and sampling.get("max_reviews") == settings.ai_max_reviews
            )
        )
        return (
            job.input_hash == digest
            and payload.get("model_version") == settings.ai_model_version
            and payload.get("policy_version") == settings.ai_policy_version
            and selection_matches
        )

    async def enqueue(
        self,
        platform: str,
        product_id: str,
        settings: Settings,
        trigger: int | None = None,
        force: bool = False,
    ):
        settings = await effective_analysis_settings(self.session, settings)
        product = await self.lock_product(platform, product_id)
        if product is None:
            return None
        latest = await self.session.scalar(
            select(AnalysisJob)
            .where(AnalysisJob.platform == platform, AnalysisJob.product_id == product_id)
            .order_by(AnalysisJob.id.desc())
            .limit(1)
        )
        if latest is None and not settings.ai_analysis_enabled:
            return None
        reviews = await self.snapshot(platform, product_id)
        digest = input_hash(reviews)
        product.analysis_input_hash = digest
        if (
            latest
            and latest.status != "stale"
            and not force
            and self._same(latest, digest, settings)
        ):
            return latest.id
        await self.session.execute(
            update(AnalysisJob)
            .where(
                AnalysisJob.platform == platform,
                AnalysisJob.product_id == product_id,
                AnalysisJob.status.in_(["queued", "running"]),
            )
            .values(status="stale", updated_at=datetime.now(UTC))
        )
        if not reviews or not settings.ai_analysis_enabled:
            return None
        selected, sampling = select_reviews(reviews, settings.ai_max_reviews)
        payload = {
            "reviews": selected,
            "sampling": sampling,
            "model_version": settings.ai_model_version,
            "policy_version": settings.ai_policy_version,
        }
        job = AnalysisJob(
            platform=platform,
            product_id=product_id,
            input_hash=digest,
            input_payload=payload,
            input_review_count=len(selected),
            trigger_collection_job_id=trigger,
            status="queued",
        )
        self.session.add(job)
        await self.session.flush()
        return job.id

    async def published(self, platform: str, product_id: str, settings: Settings):
        return await self.session.scalar(
            select(AnalysisJob)
            .where(
                AnalysisJob.platform == platform,
                AnalysisJob.product_id == product_id,
                published_condition(),
            )
            .order_by(*published_order(settings))
            .limit(1)
        )

    async def claim(
        self,
        worker_id: str,
        lease_seconds: float,
        *,
        backfill_only=False,
        reanalysis_share: float = 0.0,
    ):
        now = datetime.now(UTC)
        await self.session.execute(
            update(AnalysisJob)
            .where(
                AnalysisJob.status == "running",
                AnalysisJob.lease_expires_at <= now,
                AnalysisJob.attempt_count >= AnalysisJob.max_attempts,
            )
            .values(status="failed", last_error=FAILURE_REASONS["exhausted"], completed_at=now)
        )
        in_campaign = (
            select(AnalysisCampaignItem.analysis_job_id)
            .join(AnalysisCampaign, AnalysisCampaign.id == AnalysisCampaignItem.campaign_id)
            .where(
                AnalysisCampaign.status == "running",
                AnalysisCampaignItem.status == "active",
                AnalysisCampaignItem.analysis_job_id == AnalysisJob.id,
            )
            .exists()
        )
        campaign_filter = in_campaign if backfill_only else True
        job = await self.session.scalar(
            select(AnalysisJob)
            .where(
                campaign_filter,
                AnalysisJob.status.in_(["queued", "running"]),
                AnalysisJob.attempt_count < AnalysisJob.max_attempts,
                AnalysisJob.available_at <= now,
                (AnalysisJob.lease_expires_at.is_(None)) | (AnalysisJob.lease_expires_at <= now),
            )
            # 캠페인이 진행 중인 동안 수집이 멈추므로 캠페인 작업을 먼저 끝낸다. 그다음은 결과가
            # 아직 없는 상품의 첫 분석이다. 재분석 대상은 새 결과 전까지 기존 결과를 보여 준다.
            # 첫 분석이 끊이지 않아도 재분석이 굶지 않도록 일정 비율은 재분석을 먼저 꺼낸다.
            .order_by(
                in_campaign.desc(),
                has_prior_result().desc()
                if random.random() < reanalysis_share
                else has_prior_result().asc(),
                AnalysisJob.id,
            )
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if job is None:
            return None
        job.status = "running"
        job.locked_by = worker_id
        job.locked_at = now
        job.lease_expires_at = now + timedelta(seconds=lease_seconds)
        job.attempt_count += 1
        job.last_error = None
        await self.session.flush()
        return {
            "id": job.id,
            "platform": job.platform,
            "product_id": job.product_id,
            "input_hash": job.input_hash,
            "input_payload": job.input_payload,
        }

    async def renew(self, job_id: int, worker_id: str, lease_seconds: float) -> bool:
        now = datetime.now(UTC)
        result = await self.session.execute(
            update(AnalysisJob)
            .where(
                AnalysisJob.id == job_id,
                AnalysisJob.status == "running",
                AnalysisJob.locked_by == worker_id,
                AnalysisJob.lease_expires_at > now,
            )
            .values(lease_expires_at=now + timedelta(seconds=lease_seconds), updated_at=now)
        )
        return result.rowcount == 1

    async def _owned(self, job_id: int, worker_id: str):
        return await self.session.scalar(
            select(AnalysisJob)
            .where(
                AnalysisJob.id == job_id,
                AnalysisJob.status == "running",
                AnalysisJob.locked_by == worker_id,
                AnalysisJob.lease_expires_at > datetime.now(UTC),
            )
            .with_for_update()
        )

    async def complete(self, claim: dict, worker_id: str, response, settings: Settings):
        settings = await effective_analysis_settings(self.session, settings)
        # 수집 저장과 같은 순서로 잠근다.
        await self.lock_product(claim["platform"], claim["product_id"])
        job = await self._owned(claim["id"], worker_id)
        if job is None:
            return False
        current = await self.snapshot(job.platform, job.product_id)
        if not self._same(job, input_hash(current), settings):
            job.status = "stale"
            await self.session.flush()
            await self.enqueue(job.platform, job.product_id, settings)
            return False
        if (settings.ai_model_version and response.model_version != settings.ai_model_version) or (
            settings.ai_policy_version and response.policy_version != settings.ai_policy_version
        ):
            raise ValueError("목표 분석 버전과 응답 버전이 다릅니다.")
        expected = {r["review_id"] for r in (job.input_payload or {}).get("reviews", [])}
        results = response.results
        actual = [r["review_id"] for r in results]
        if len(actual) != len(expected) or set(actual) != expected:
            raise ValueError("분석 결과의 리뷰 식별자가 입력과 다릅니다.")
        for result in results:
            self.session.add(
                ReviewAnalysisRow(
                    analysis_job_id=job.id,
                    platform=job.platform,
                    product_id=job.product_id,
                    input_hash=job.input_hash,
                    model_version=response.model_version,
                    **result,
                )
            )
        job.model_version = response.model_version
        job.policy_version = response.policy_version
        job.result = {"review_count": len(results), "ai_job_id": response.ai_job_id}
        job.status = "done"
        job.completed_at = datetime.now(UTC)
        job.lease_expires_at = None
        await self.session.flush()
        return True

    async def fail(
        self,
        job_id: int,
        worker_id: str,
        retryable: bool,
        kind: str = "validation",
        min_delay: float = 0,
    ):
        job = await self._owned(job_id, worker_id)
        if job is None:
            return False
        now = datetime.now(UTC)
        retry = retryable and job.attempt_count < job.max_attempts
        job.status = "queued" if retry else "failed"
        job.last_error = FAILURE_REASONS.get(kind, FAILURE_REASONS["validation"])
        job.lease_expires_at = None
        job.locked_by = None
        delay = max(min(30 * 2 ** (job.attempt_count - 1), 300), min_delay)
        job.available_at = now + timedelta(seconds=delay)
        job.completed_at = None if retry else now
        return True

    async def status(
        self,
        platform: str,
        product_id: str,
        settings: Settings,
        review_ids: list[str] | None = None,
    ) -> dict:
        settings = await effective_analysis_settings(self.session, settings)
        product = await self.lock_product(platform, product_id, read=True)
        job = await self.session.scalar(
            select(AnalysisJob)
            .where(AnalysisJob.platform == platform, AnalysisJob.product_id == product_id)
            .order_by(AnalysisJob.id.desc())
            .limit(1)
        )
        if job is None:
            return {
                "status": "not_analyzed" if settings.ai_analysis_enabled else "disabled",
                "job": None,
                "review_count": 0,
                "results": [],
            }
        latest = job
        published = await self.published(platform, product_id, settings)
        digest = product.analysis_input_hash if product else None
        if published is not None:
            job = published
            status = "done"
        else:
            status = job.status if digest and self._same(job, digest, settings) else "stale"
        is_current = bool(digest and self._same(job, digest, settings))
        results = []
        if status == "done":
            stmt = select(ReviewAnalysisRow).where(ReviewAnalysisRow.analysis_job_id == job.id)
            if review_ids is not None:
                stmt = stmt.where(ReviewAnalysisRow.review_id.in_(review_ids))
            rows = await self.session.scalars(stmt.order_by(ReviewAnalysisRow.review_id))
            for r in rows:
                results.append(
                    {
                        "review_id": r.review_id,
                        "rti": _number(r.rti),
                        "level": r.level,
                        "text_score": _number(r.text_score),
                        "behavior_score": _number(r.behavior_score),
                        "network_score": _number(r.network_score),
                        "reasons": r.reasons,
                    }
                )
        sampling = sampling_info(job)
        return {
            "status": status,
            "is_current": is_current,
            "target_model_version": settings.ai_model_version,
            "target_policy_version": settings.ai_policy_version,
            "refresh_job": public_job(latest) if latest.id != job.id else None,
            "sampled": sampling["sampled"],
            "source_review_count": sampling["source_review_count"],
            "sampling": sampling,
            "job": public_job(job),
            "input_hash": job.input_hash,
            "model_version": job.model_version,
            "policy_version": job.policy_version,
            "review_count": job.input_review_count or 0,
            "results": results,
        }


def _number(value):
    return float(value) if value is not None else None


def public_job(job: AnalysisJob) -> dict:
    sampling = sampling_info(job)
    return {
        "sampled": sampling["sampled"],
        "source_review_count": sampling["source_review_count"],
        "id": job.id,
        "platform": job.platform,
        "product_id": job.product_id,
        "status": job.status,
        "attempt_count": job.attempt_count,
        "ai_job_id": (job.result or {}).get("ai_job_id"),
        "input_review_count": job.input_review_count,
        "last_error": job.last_error,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
    }


def sampling_info(job: AnalysisJob) -> dict:
    stored = (job.input_payload or {}).get("sampling")
    if stored is not None:
        return stored
    count = job.input_review_count or 0
    return {
        "sampled": False,
        "method": "all",
        "source_review_count": count,
        "analyzed_review_count": count,
    }
