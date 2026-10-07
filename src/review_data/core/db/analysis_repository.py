"""입력 스냅샷·작업 소유권·분석 결과의 게시를 관리한다."""

import hashlib
import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from review_data.core.db.models import AnalysisJob, ProductRow, ReviewAnalysisRow, ReviewRow
from review_data.core.settings import Settings


def input_hash(reviews: list[dict]) -> str:
    payload = json.dumps(reviews, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


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
        rows = await self.session.scalars(
            select(ReviewRow)
            .where(ReviewRow.platform == platform, ReviewRow.product_id == product_id)
            .order_by(ReviewRow.review_id)
        )
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
        return (
            job.input_hash == digest
            and payload.get("model_version") == settings.ai_model_version
            and payload.get("policy_version") == settings.ai_policy_version
        )

    async def enqueue(
        self,
        platform: str,
        product_id: str,
        settings: Settings,
        trigger: int | None = None,
        force: bool = False,
    ):
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
                AnalysisJob.status.in_(["queued", "running", "done"]),
            )
            .values(status="stale", updated_at=datetime.now(UTC))
        )
        if not reviews or not settings.ai_analysis_enabled:
            return None
        payload = {
            "reviews": reviews,
            "model_version": settings.ai_model_version,
            "policy_version": settings.ai_policy_version,
        }
        oversized = len(reviews) > settings.ai_max_reviews
        job = AnalysisJob(
            platform=platform,
            product_id=product_id,
            input_hash=digest,
            input_payload=payload if not oversized else {**payload, "reviews": []},
            input_review_count=len(reviews),
            trigger_collection_job_id=trigger,
            status="failed" if oversized else "queued",
            last_error="리뷰 수가 분석 상한을 초과했습니다. 분할 계약이 필요합니다."
            if oversized
            else None,
        )
        self.session.add(job)
        await self.session.flush()
        return job.id

    async def claim(self, worker_id: str, lease_seconds: float):
        now = datetime.now(UTC)
        await self.session.execute(
            update(AnalysisJob)
            .where(
                AnalysisJob.status == "running",
                AnalysisJob.lease_expires_at <= now,
                AnalysisJob.attempt_count >= AnalysisJob.max_attempts,
            )
            .values(status="failed", last_error="분석 최대 시도 횟수 초과", completed_at=now)
        )
        job = await self.session.scalar(
            select(AnalysisJob)
            .where(
                AnalysisJob.status.in_(["queued", "running"]),
                AnalysisJob.attempt_count < AnalysisJob.max_attempts,
                AnalysisJob.available_at <= now,
                (AnalysisJob.lease_expires_at.is_(None)) | (AnalysisJob.lease_expires_at <= now),
            )
            .order_by(AnalysisJob.id)
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
        job.result = {"review_count": len(results)}
        job.status = "done"
        job.completed_at = datetime.now(UTC)
        job.lease_expires_at = None
        await self.session.flush()
        return True

    async def fail(self, job_id: int, worker_id: str, retryable: bool):
        job = await self._owned(job_id, worker_id)
        if job is None:
            return False
        now = datetime.now(UTC)
        retry = retryable and job.attempt_count < job.max_attempts
        job.status = "queued" if retry else "failed"
        job.last_error = "분석 요청 또는 응답 검증 실패"
        job.lease_expires_at = None
        job.locked_by = None
        job.available_at = now + timedelta(seconds=min(30 * 2 ** (job.attempt_count - 1), 300))
        job.completed_at = None if retry else now
        return True

    async def status(
        self,
        platform: str,
        product_id: str,
        settings: Settings,
        review_ids: list[str] | None = None,
    ) -> dict:
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
        digest = product.analysis_input_hash if product else None
        status = job.status if digest and self._same(job, digest, settings) else "stale"
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
        return {
            "status": status,
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
    return {
        "id": job.id,
        "platform": job.platform,
        "product_id": job.product_id,
        "status": job.status,
        "attempt_count": job.attempt_count,
        "input_review_count": job.input_review_count,
        "last_error": job.last_error,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
    }
