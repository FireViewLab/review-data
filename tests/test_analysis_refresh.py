"""실 DB에서 재분석 재개·큐 상한·결과 유지·실패 복구를 검증한다."""

import asyncio

from sqlalchemy import func, select

from review_data.core.analysis_stream import AnalysisStreamError, AnalysisStreamResult
from review_data.core.db.analysis_repository import AnalysisRepository
from review_data.core.db.models import AnalysisJob, ProductRow, ReviewRow
from review_data.core.service.analysis_refresh import (
    AnalysisRefreshService,
    campaign_status,
    collection_paused,
)
from review_data.core.service.catalog import catalog_page
from review_data.core.service.scheduling import SchedulingService
from review_data.core.settings import Settings
from review_data.worker.analysis_worker import run_once


def config(version="v2", **kwargs):
    return Settings(
        _env_file=None,
        ai_analysis_enabled=True,
        ai_stream_url="https://example.invalid/stream",
        ai_model_version=version,
        ai_policy_version="p1",
        analysis_refresh_enabled=True,
        analysis_refresh_max_pending=2,
        analysis_refresh_batch_size=1,
        **kwargs,
    )


class Client:
    def __init__(self, version="v2", fail=False):
        self.version, self.fail, self.ids = version, fail, []

    async def analyze(self, **kwargs):
        self.ids.append(kwargs["job_id"])
        if self.fail:
            raise AnalysisStreamError("private-token", retryable=False)
        return AnalysisStreamResult(
            [
                {
                    "review_id": r["review_id"],
                    "rti": 80 if self.version == "v2" else 50,
                    "level": "safe" if self.version == "v2" else "warn",
                    "text_score": 80,
                    "behavior_score": None,
                    "network_score": None,
                    "reasons": [],
                }
                for r in kwargs["reviews"]
            ],
            self.version,
            "p1",
        )


async def seed(factory, count=3, version="v1", done=True):
    settings = config(version)
    async with factory() as s:
        for i in range(count):
            s.add(ProductRow(platform="kurly", product_id=str(i), name="상품", url="https://x"))
        s.add(ProductRow(platform="kurly", product_id="empty", name="빈 상품", url="https://x"))
        await s.flush()
        for i in range(count):
            s.add(
                ReviewRow(
                    platform="kurly", product_id=str(i), review_id="r", content="리뷰", rating=5
                )
            )
        await s.flush()
        for i in range(count):
            await AnalysisRepository(s).enqueue("kurly", str(i), settings)
        await s.commit()
    if done:
        for _ in range(count):
            assert await run_once(factory, "initial", settings=settings, client=Client(version))


async def tick(factory, settings, **kwargs):
    async with factory() as s:
        result = await AnalysisRefreshService(s, settings).run_once(**kwargs)
        await s.commit()
        return result


async def test_bounded_resume_keeps_target_snapshot_and_completes(session_factory):
    factory = session_factory
    await seed(factory)
    settings = config()
    first = await tick(factory, settings)
    # New service instances/processes continue the same persisted campaign.
    second = await tick(factory, settings)
    third = await tick(factory, settings)
    assert first["campaign_id"] == second["campaign_id"] == third["campaign_id"]
    assert [first["reserved"], second["reserved"], third["reserved"]] == [1, 1, 0]
    async with factory() as s:
        report = (await campaign_status(s))[0]
        assert report["total"] == 3 and report["queued"] == 2 and report["waiting"] == 1
        assert await collection_paused(s, settings)
        assert (await SchedulingService(s, settings).run_once()).created == 0
    for _ in range(3):
        assert await run_once(factory, "new", settings=settings, client=Client())
        await tick(factory, settings)
    async with factory() as s:
        report = (await campaign_status(s))[0]
        assert report["done"] == 3 and report["total"] == 3 and report["failed"] == 0
        assert report["status"] == "completed"
        assert not await collection_paused(s, settings)
    assert (await tick(factory, settings))["reserved"] == 0


async def test_concurrent_reservation_respects_queue_capacity(session_factory):
    await seed(session_factory, count=5)
    settings = config()
    results = await asyncio.gather(*(tick(session_factory, settings) for _ in range(5)))
    assert len({r["campaign_id"] for r in results}) == 1
    async with session_factory() as s:
        active = await s.scalar(
            select(func.count())
            .select_from(AnalysisJob)
            .where(AnalysisJob.status.in_(["queued", "running"]))
        )
        assert active == 2
        status = (await campaign_status(s))[0]
        assert status["total"] == 5 and status["queued"] == 2 and status["waiting"] == 3


async def test_matching_model_policy_input_skipped_and_missing_target_guarded(session_factory):
    await seed(session_factory, version="v2")
    settings = config()
    result = await tick(session_factory, settings)
    assert result["status"] == "completed"
    async with session_factory() as s:
        assert (await campaign_status(s))[0]["total"] == 0
    unknown = settings.model_copy(update={"ai_model_version": None})
    assert (await tick(session_factory, unknown))["reason"] == "target_version_required"


async def test_failure_retains_old_results_retry_has_new_id_and_success_switches(session_factory):
    await seed(session_factory, count=1)
    settings = config()
    await tick(session_factory, settings)
    async with session_factory() as s:
        visible = await AnalysisRepository(s).status("kurly", "0", settings)
        assert visible["status"] == "done" and visible["is_current"] is False
        assert visible["model_version"] == "v1" and visible["results"][0]["rti"] == 50
        assert visible["refresh_job"]["status"] == "queued"
        catalog, _ = await catalog_page(s, settings, 10)
        assert catalog[0][1]["avg_rti"] == 50 and catalog[0][1]["is_current"] is False
    failed = Client(fail=True)
    await run_once(session_factory, "w", settings=settings, client=failed)
    await tick(session_factory, settings)
    async with session_factory() as s:
        report = (await campaign_status(s))[0]
        assert report["status"] == "completed_with_errors" and report["failed"] == 1
        assert (await AnalysisRepository(s).status("kurly", "0", settings))["results"][0][
            "rti"
        ] == 50
    await tick(session_factory, settings, retry_failed=True)
    succeeded = Client()
    await run_once(session_factory, "w", settings=settings, client=succeeded)
    assert failed.ids[0] != succeeded.ids[0]
    await tick(session_factory, settings)
    async with session_factory() as s:
        visible = await AnalysisRepository(s).status("kurly", "0", settings)
        assert visible["model_version"] == "v2" and visible["is_current"] is True
        assert visible["results"][0]["rti"] == 80 and visible["refresh_job"] is None
        assert (await campaign_status(s))[0]["done"] == 1
        assert (await s.get(AnalysisJob, failed.ids[0])).status == "failed"


async def test_old_queued_versions_are_replaced_without_post(session_factory):
    await seed(session_factory, count=1, done=False)
    settings = config()
    client = Client()
    assert await run_once(session_factory, "w", settings=settings, client=client)
    assert client.ids == []
    async with session_factory() as s:
        jobs = list(await s.scalars(select(AnalysisJob).order_by(AnalysisJob.id)))
        assert jobs[0].status == "stale" and jobs[1].status == "queued"
        assert jobs[1].input_payload["model_version"] == "v2"
    assert await run_once(session_factory, "w", settings=settings, client=client)
    await tick(session_factory, settings)
    async with session_factory() as s:
        assert (await campaign_status(s))[0]["total"] == 0


async def test_new_version_supersedes_campaign_without_erasing_published_result(session_factory):
    await seed(session_factory, count=3)
    first = await tick(session_factory, config())
    second = await tick(session_factory, config("v3"))
    assert first["campaign_id"] != second["campaign_id"]
    async with session_factory() as s:
        reports = await campaign_status(s)
        assert reports[1]["status"] == "superseded"
        visible = await AnalysisRepository(s).status("kurly", "0", config("v3"))
        assert visible["model_version"] == "v1" and visible["results"][0]["rti"] == 50


async def test_same_version_rollback_selects_matching_published_result(session_factory):
    await seed(session_factory, count=1)
    settings = config()
    await tick(session_factory, settings)
    await run_once(session_factory, "w", settings=settings, client=Client())
    await tick(session_factory, settings)
    old = config("v1")
    await tick(session_factory, old)
    async with session_factory() as s:
        visible = await AnalysisRepository(s).status("kurly", "0", old)
        assert visible["model_version"] == "v1" and visible["results"][0]["rti"] == 50
        assert visible["is_current"] is True
        assert (await campaign_status(s))[0]["total"] == 0
        catalog, _ = await catalog_page(s, old, 10)
        assert catalog[0][1]["avg_rti"] == 50


async def test_retry_reconciles_recent_failure_before_reset(session_factory):
    await seed(session_factory, count=1)
    settings = config()
    await tick(session_factory, settings)
    client = Client(fail=True)
    await run_once(session_factory, "w", settings=settings, client=client)
    # No scheduler reconciliation happened between failure and the operator command.
    await tick(session_factory, settings, retry_failed=True)
    async with session_factory() as s:
        state = (await campaign_status(s))[0]
        assert state["failed"] == 0 and state["queued"] == 1
        newest = await s.scalar(select(AnalysisJob).order_by(AnalysisJob.id.desc()).limit(1))
        assert newest.id != client.ids[0]
