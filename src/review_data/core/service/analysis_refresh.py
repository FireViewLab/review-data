"""예약 대상을 DB에 보존하며 모델 변경을 제한된 큐로 처리한다."""

import hashlib
import json
import math
from datetime import UTC, datetime, timedelta

from sqlalchemy import exists, func, select, text, update

from review_data.core.analysis_sampling import SAMPLING_VERSION
from review_data.core.db.analysis_control import effective_analysis_settings
from review_data.core.db.analysis_repository import AnalysisRepository
from review_data.core.db.models import AnalysisCampaign, AnalysisCampaignItem, AnalysisJob
from review_data.core.service.scheduling import _SCHEDULER_LOCK_KEY
from review_data.core.settings import Settings


async def collection_paused(session, settings):
    if not settings.analysis_refresh_pause_collection:
        return False
    return bool(await session.scalar(select(exists().where(AnalysisCampaign.status == "running"))))


class AnalysisRefreshService:
    def __init__(self, session, settings: Settings):
        self.session = session
        self.settings = settings

    async def run_once(self, *, retry_failed=False, reactivate=False):
        s = await effective_analysis_settings(self.session, self.settings)
        if not s.analysis_refresh_enabled or not s.ai_analysis_enabled:
            return {"enabled": False, "reserved": 0}
        if not s.ai_model_version or not s.ai_policy_version:
            return {"enabled": True, "reserved": 0, "reason": "target_version_required"}
        await self.session.execute(select(func.pg_advisory_xact_lock(_SCHEDULER_LOCK_KEY)))
        key = hashlib.sha256(
            json.dumps(
                [s.ai_model_version, s.ai_policy_version, SAMPLING_VERSION, s.ai_max_reviews]
            ).encode()
        ).hexdigest()
        campaign = await self.session.scalar(
            select(AnalysisCampaign).where(AnalysisCampaign.target_key == key).with_for_update()
        )
        if campaign is None:
            await self.session.execute(
                update(AnalysisCampaign)
                .where(AnalysisCampaign.status == "running")
                .values(status="superseded", completed_at=datetime.now(UTC))
            )
            campaign = AnalysisCampaign(
                target_key=key,
                model_version=s.ai_model_version,
                policy_version=s.ai_policy_version,
                sampling_version=SAMPLING_VERSION,
                max_reviews=s.ai_max_reviews,
            )
            self.session.add(campaign)
            await self.session.flush()
            await self.session.execute(
                text("""
                INSERT INTO analysis_campaign_items(campaign_id,platform,product_id)
                SELECT :campaign,p.platform,p.product_id FROM products p
                WHERE EXISTS(SELECT 1 FROM reviews r WHERE r.platform=p.platform
                    AND r.product_id=p.product_id)
                AND NOT EXISTS(SELECT 1 FROM analysis_jobs j WHERE j.platform=p.platform
                    AND j.product_id=p.product_id AND j.status='done'
                    AND j.model_version=:model AND j.policy_version=:policy
                    AND j.input_hash=p.analysis_input_hash
                    AND j.input_payload->>'model_version'=:model
                    AND j.input_payload->>'policy_version'=:policy
                    AND j.input_payload->'sampling'->>'policy_version'=:sampling
                    AND (j.input_payload->'sampling'->>'max_reviews')::int=:max_reviews)
            """),
                {
                    "campaign": campaign.id,
                    "model": s.ai_model_version,
                    "policy": s.ai_policy_version,
                    "sampling": SAMPLING_VERSION,
                    "max_reviews": s.ai_max_reviews,
                },
            )
        if reactivate and campaign.status == "superseded":
            await self.session.execute(
                update(AnalysisCampaign)
                .where(AnalysisCampaign.status == "running", AnalysisCampaign.id != campaign.id)
                .values(status="superseded", completed_at=datetime.now(UTC))
            )
            campaign.status = "running"
            campaign.completed_at = None
        transition = {"outdated_queued": 0, "retargeted_products": 0}
        if campaign.status == "running" or reactivate:
            transition = await self._retarget_queued(campaign, s)
            if transition["retargeted_products"]:
                campaign.status = "running"
                campaign.completed_at = None
        retry_keys = set()
        if retry_failed:
            await self._reconcile(campaign)
            retry_keys = set(
                (
                    await self.session.execute(
                        select(
                            AnalysisCampaignItem.platform, AnalysisCampaignItem.product_id
                        ).where(
                            AnalysisCampaignItem.campaign_id == campaign.id,
                            AnalysisCampaignItem.status == "failed",
                        )
                    )
                ).all()
            )
            await self.session.execute(
                update(AnalysisCampaignItem)
                .where(
                    AnalysisCampaignItem.campaign_id == campaign.id,
                    AnalysisCampaignItem.status == "failed",
                )
                .values(status="pending", analysis_job_id=None)
            )
            campaign.status = "running"
            campaign.completed_at = None
        if campaign.status != "running":
            return {"enabled": True, "campaign_id": campaign.id, "reserved": 0, **transition}
        await self._reconcile(campaign)
        await self._attach_active(campaign)
        active = await self.session.scalar(
            select(func.count())
            .select_from(AnalysisJob)
            .where(AnalysisJob.status.in_(["queued", "running"]))
        )
        room = min(s.analysis_refresh_batch_size, max(0, s.analysis_refresh_max_pending - active))
        items = (
            list(
                await self.session.scalars(
                    select(AnalysisCampaignItem)
                    .where(
                        AnalysisCampaignItem.campaign_id == campaign.id,
                        AnalysisCampaignItem.status == "pending",
                    )
                    .order_by(AnalysisCampaignItem.platform, AnalysisCampaignItem.product_id)
                    .limit(room)
                    .with_for_update()
                )
            )
            if room
            else []
        )
        reserved = 0
        repo = AnalysisRepository(self.session)
        for item in items:
            # Retry terminal failures with a new ID; transient retries keep their old ID.
            latest = await self.session.scalar(
                select(AnalysisJob)
                .where(
                    AnalysisJob.platform == item.platform, AnalysisJob.product_id == item.product_id
                )
                .order_by(AnalysisJob.id.desc())
                .limit(1)
            )
            job_id = await repo.enqueue(
                item.platform,
                item.product_id,
                s,
                force=bool(latest and latest.status == "failed")
                or (item.platform, item.product_id) in retry_keys,
            )
            item.analysis_job_id = job_id
            item.status = "active" if job_id else "skipped"
            reserved += job_id is not None
        await self.session.flush()
        await self._reconcile(campaign)
        remaining = await self.session.scalar(
            select(func.count())
            .select_from(AnalysisCampaignItem)
            .where(
                AnalysisCampaignItem.campaign_id == campaign.id,
                AnalysisCampaignItem.status.in_(["pending", "active"]),
            )
        )
        if not remaining:
            failed = await self.session.scalar(
                select(func.count())
                .select_from(AnalysisCampaignItem)
                .where(
                    AnalysisCampaignItem.campaign_id == campaign.id,
                    AnalysisCampaignItem.status == "failed",
                )
            )
            campaign.status = "completed_with_errors" if failed else "completed"
            campaign.completed_at = datetime.now(UTC)
        return {
            "enabled": True,
            "campaign_id": campaign.id,
            "reserved": reserved,
            "status": campaign.status,
            **transition,
        }

    async def _retarget_queued(self, campaign, settings):
        # Free slots occupied by an obsolete target without rewriting its immutable request.
        # Persist replacement candidates before reserving within the normal queue capacity.
        row = (
            (
                await self.session.execute(
                    text("""
            WITH retired AS (
                UPDATE analysis_jobs SET status='stale',updated_at=now(),
                    last_error='목표 버전 변경으로 대기열 전환'
                WHERE status='queued' AND (
                    input_payload->>'model_version' IS DISTINCT FROM :model OR
                    input_payload->>'policy_version' IS DISTINCT FROM :policy)
                RETURNING platform,product_id
            ), targets AS (
                INSERT INTO analysis_campaign_items(campaign_id,platform,product_id,status)
                SELECT DISTINCT CAST(:campaign AS bigint),p.platform,p.product_id,'pending'
                FROM retired r JOIN products p USING(platform,product_id)
                WHERE EXISTS(SELECT 1 FROM reviews v WHERE v.platform=p.platform
                    AND v.product_id=p.product_id)
                AND NOT EXISTS(SELECT 1 FROM analysis_jobs j WHERE j.platform=p.platform
                    AND j.product_id=p.product_id AND j.status='done'
                    AND j.model_version=:model AND j.policy_version=:policy
                    AND j.input_hash=p.analysis_input_hash
                    AND j.input_payload->>'model_version'=:model
                    AND j.input_payload->>'policy_version'=:policy
                    AND j.input_payload->'sampling'->>'policy_version'=:sampling
                    AND (j.input_payload->'sampling'->>'max_reviews')::int=:max_reviews)
                ON CONFLICT(campaign_id,platform,product_id) DO UPDATE
                    SET status='pending',analysis_job_id=NULL
                RETURNING product_id
            ) SELECT (SELECT count(*) FROM retired) AS outdated_queued,
                     (SELECT count(*) FROM targets) AS retargeted_products
        """),
                    {
                        "campaign": campaign.id,
                        "model": settings.ai_model_version,
                        "policy": settings.ai_policy_version,
                        "sampling": SAMPLING_VERSION,
                        "max_reviews": settings.ai_max_reviews,
                    },
                )
            )
            .mappings()
            .one()
        )
        return dict(row)

    async def _attach_active(self, campaign):
        # Keep a claimed request alive until completion; also reuse existing new-target queues.
        await self.session.execute(
            text("""
            UPDATE analysis_campaign_items i SET status='active',analysis_job_id=j.id
            FROM analysis_jobs j WHERE i.campaign_id=:campaign AND i.status='pending'
                AND j.platform=i.platform AND j.product_id=i.product_id
                AND (j.status='running' OR (j.status='queued'
                    AND j.input_payload->>'model_version'=:model
                    AND j.input_payload->>'policy_version'=:policy))
        """),
            {
                "campaign": campaign.id,
                "model": campaign.model_version,
                "policy": campaign.policy_version,
            },
        )

    async def _reconcile(self, campaign):
        # A mismatched response must never count as success for the campaign.
        await self.session.execute(
            text("""
            UPDATE analysis_campaign_items i SET status=CASE
                WHEN j.status IN ('done','failed','stale') AND (
                    j.input_payload->>'model_version' IS DISTINCT FROM :model OR
                    j.input_payload->>'policy_version' IS DISTINCT FROM :policy) THEN 'pending'
                WHEN j.status='done' AND j.model_version=:model AND j.policy_version=:policy
                    THEN 'done'
                WHEN j.status IN ('failed','done') THEN 'failed'
                WHEN j.status='stale' THEN 'pending'
                ELSE 'active' END,
                analysis_job_id=CASE WHEN j.status='stale' OR (
                    j.status IN ('done','failed') AND (
                    j.input_payload->>'model_version' IS DISTINCT FROM :model OR
                    j.input_payload->>'policy_version' IS DISTINCT FROM :policy))
                    THEN NULL ELSE j.id END
            FROM analysis_jobs j WHERE i.campaign_id=:campaign AND i.status='active'
                AND j.id=i.analysis_job_id
        """),
            {
                "campaign": campaign.id,
                "model": campaign.model_version,
                "policy": campaign.policy_version,
            },
        )


async def campaign_status(session, *, enabled=True):
    rows = [
        dict(row)
        for row in (
            await session.execute(
                text("""
        SELECT c.id,c.model_version,c.policy_version,c.sampling_version,c.max_reviews,c.status,
               c.created_at,c.completed_at,count(i.product_id) AS total,
               count(*) FILTER(WHERE i.status='pending') AS waiting,
               count(*) FILTER(WHERE i.status='active' AND j.status='queued') AS queued,
               count(*) FILTER(WHERE i.status='active' AND j.status='running') AS running,
               count(*) FILTER(WHERE i.status='done' OR (i.status='active' AND j.status='done'
                   AND j.model_version=c.model_version AND j.policy_version=c.policy_version
                   AND j.input_payload->>'model_version'=c.model_version
                   AND j.input_payload->>'policy_version'=c.policy_version)) AS done,
               count(*) FILTER(WHERE i.status='failed' OR
                   (i.status='active' AND j.status='failed'
                   AND j.input_payload->>'model_version'=c.model_version
                   AND j.input_payload->>'policy_version'=c.policy_version)) AS failed,
               count(*) FILTER(WHERE i.status='skipped') AS skipped,
               greatest(c.created_at,now()-interval '15 min') AS sample_started_at,
               count(*) FILTER(WHERE j.status='done'
                   AND j.model_version=c.model_version AND j.policy_version=c.policy_version
                   AND j.input_payload->>'model_version'=c.model_version
                   AND j.input_payload->>'policy_version'=c.policy_version
                   AND j.completed_at>=greatest(c.created_at,now()-interval '15 min'))
                   AS recent_done,
               max(j.completed_at) FILTER(WHERE j.status='done'
                   AND j.model_version=c.model_version AND j.policy_version=c.policy_version
                   AND j.input_payload->>'model_version'=c.model_version
                   AND j.input_payload->>'policy_version'=c.policy_version) AS last_done_at
        FROM (SELECT * FROM analysis_campaigns ORDER BY id DESC LIMIT 5) c
        LEFT JOIN analysis_campaign_items i ON i.campaign_id=c.id
        LEFT JOIN analysis_jobs j ON j.id=i.analysis_job_id
        GROUP BY c.id,c.model_version,c.policy_version,c.sampling_version,c.max_reviews,
                 c.status,c.created_at,c.completed_at ORDER BY c.id DESC
    """)
            )
        ).mappings()
    ]
    now = datetime.now(UTC)
    for row in rows:
        row.update(estimate_completion(row, now, enabled=enabled))
    return rows


def estimate_completion(row, now, *, enabled=True):
    result = {
        "rate_products_per_minute": None,
        "estimated_remaining_seconds": None,
        "estimated_completion_at": None,
        "estimate_state": "collecting",
    }
    remaining = row["total"] - row["done"] - row["failed"] - row["skipped"]
    if row["status"] in {"completed", "completed_with_errors"}:
        return {
            **result,
            "estimated_remaining_seconds": 0,
            "estimated_completion_at": row["completed_at"],
            "estimate_state": "finished",
        }
    if row["status"] != "running" or not enabled:
        return {**result, "estimate_state": "paused"}
    seconds = (now - row["sample_started_at"]).total_seconds()
    if row["recent_done"] < 5 or seconds < 60:
        return result
    if not row["last_done_at"] or (now - row["last_done_at"]).total_seconds() > 300:
        return {**result, "estimate_state": "stalled"}
    eta = math.ceil(max(0, remaining) * seconds / row["recent_done"])
    return {
        "rate_products_per_minute": round(row["recent_done"] * 60 / seconds, 2),
        "estimated_remaining_seconds": eta,
        "estimated_completion_at": now + timedelta(seconds=eta),
        "estimate_state": "estimated",
    }
