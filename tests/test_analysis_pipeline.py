"""실제 PostgreSQL에서 분석 작업의 원자성·복구·결과 게시를 검증한다."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from review_data.core.analysis_stream import AnalysisStreamError, AnalysisStreamResult
from review_data.core.db.analysis_repository import AnalysisRepository
from review_data.core.db.models import AnalysisJob, ProductRow, ReviewAnalysisRow, ReviewRow
from review_data.core.settings import Settings
from review_data.worker.analysis_worker import run_once


def config(**values):
    return Settings(
        _env_file=None,
        ai_analysis_enabled=True,
        ai_stream_url="http://test.invalid/analyze",
        **values,
    )


async def seed(factory, settings=None, count=2):
    async with factory() as s:
        s.add(ProductRow(platform="kurly", product_id="p", name="상품", url="https://x"))
        await s.flush()
        s.add_all(
            [
                ReviewRow(platform="kurly", product_id="p", review_id=f"r{i}", content=f"리뷰 {i}")
                for i in range(count)
            ]
        )
        await s.flush()
        job = await AnalysisRepository(s).enqueue("kurly", "p", settings or config())
        await s.commit()
        return job


def results(reviews):
    return AnalysisStreamResult(
        [
            {
                "review_id": r["review_id"],
                "rti": 0,
                "level": "danger",
                "text_score": None,
                "behavior_score": None,
                "network_score": None,
                "reasons": [],
            }
            for r in reviews
        ],
        "v1",
        "p1",
    )


class Client:
    def __init__(self, callback=None):
        self.callback = callback
        self.calls = 0

    async def analyze(self, **kwargs):
        self.calls += 1
        if self.callback:
            return await self.callback(kwargs)
        return results(kwargs["reviews"])


async def test_success_is_atomic_and_unchanged_input_reused(session_factory):
    job = await seed(session_factory)
    client = Client()
    assert await run_once(session_factory, "w", settings=config(), client=client)
    async with session_factory() as s:
        repo = AnalysisRepository(s)
        assert await repo.enqueue("kurly", "p", config()) == job
        status = await repo.status("kurly", "p", config())
        assert status["status"] == "done" and len(status["results"]) == 2
        assert status["results"][0]["rti"] == 0
        assert status["results"][0]["text_score"] is None
    assert not await run_once(session_factory, "w", settings=config(), client=client)
    assert client.calls == 1


async def test_versions_change_and_result_visibility(session_factory):
    old = await seed(session_factory)
    await run_once(session_factory, "w", settings=config(), client=Client())
    newer = config(ai_model_version="v2")
    async with session_factory() as s:
        assert len((await AnalysisRepository(s).status("kurly", "p", newer))["results"]) == 2
        new = await AnalysisRepository(s).enqueue("kurly", "p", newer)
        await s.commit()
        assert new != old
        assert (await s.get(AnalysisJob, old)).status == "done"
        assert (await s.get(AnalysisJob, new)).input_payload["model_version"] == "v2"


async def test_concurrent_enqueue_and_claim(session_factory):
    job = await seed(session_factory)

    async def enqueue():
        async with session_factory() as s:
            value = await AnalysisRepository(s).enqueue("kurly", "p", config())
            await s.commit()
            return value

    assert await asyncio.gather(enqueue(), enqueue()) == [job, job]

    async def claim(owner):
        async with session_factory() as s:
            value = await AnalysisRepository(s).claim(owner, 60)
            await s.commit()
            return value

    claimed = await asyncio.gather(claim("a"), claim("b"))
    assert sum(item is not None for item in claimed) == 1


async def test_changed_input_during_request_discards_old_results(session_factory):
    old = await seed(session_factory)

    async def changed(kwargs):
        async with session_factory() as s:
            await AnalysisRepository(s).lock_product("kurly", "p")
            await s.execute(update(ReviewRow).values(content="수정됨"))
            await AnalysisRepository(s).enqueue("kurly", "p", config())
            await s.commit()
        return results(kwargs["reviews"])

    await run_once(session_factory, "w", settings=config(), client=Client(changed))
    async with session_factory() as s:
        assert (await s.get(AnalysisJob, old)).status == "stale"
        assert list(await s.scalars(select(ReviewAnalysisRow))) == []
        assert (await AnalysisRepository(s).status("kurly", "p", config()))["status"] == "queued"


@pytest.mark.parametrize("retryable,status", [(True, "queued"), (False, "failed")])
async def test_failure_records_only_generic_error(session_factory, retryable, status):
    job = await seed(session_factory)

    async def fail(kwargs):
        raise AnalysisStreamError("private-token-and-review-content", retryable=retryable)

    await run_once(session_factory, "w", settings=config(), client=Client(fail))
    async with session_factory() as s:
        row = await s.get(AnalysisJob, job)
        assert row.status == status and "private" not in row.last_error
        assert row.attempt_count == 1
        assert not await AnalysisRepository(s).claim("next", 60)
        assert list(await s.scalars(select(ReviewAnalysisRow))) == []


async def test_database_constraint_failure_rolls_back_results_and_done(session_factory):
    job = await seed(session_factory)

    async def invalid(kwargs):
        response = results(kwargs["reviews"])
        response.results[-1]["rti"] = 101
        return response

    await run_once(session_factory, "w", settings=config(), client=Client(invalid))
    async with session_factory() as s:
        assert (await s.get(AnalysisJob, job)).status == "queued"
        assert list(await s.scalars(select(ReviewAnalysisRow))) == []


async def test_expired_lease_recovery_and_old_owner_rejection(session_factory):
    job = await seed(session_factory)
    async with session_factory() as s:
        old = await AnalysisRepository(s).claim("old", 60)
        await s.execute(
            update(AnalysisJob).values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await s.commit()
    async with session_factory() as s:
        claim = await AnalysisRepository(s).claim("new", 60)
        assert claim["id"] == old["id"] == job
        assert not await AnalysisRepository(s).renew(job, "old", 60)
        assert not await AnalysisRepository(s).complete(
            old, "old", results(old["input_payload"]["reviews"]), config()
        )
        await s.commit()
    async with session_factory() as s:
        await s.execute(
            update(AnalysisJob).values(
                attempt_count=3,
                max_attempts=3,
                lease_expires_at=datetime.now(UTC) - timedelta(seconds=1),
            )
        )
        await s.commit()
    async with session_factory() as s:
        assert await AnalysisRepository(s).claim("last", 60) is None
        assert (await s.get(AnalysisJob, job)).status == "failed"


async def test_disabled_skips_ai_and_oversized_input_uses_sample(session_factory):
    client = Client()
    assert not await run_once(
        session_factory, "w", settings=Settings(_env_file=None), client=client
    )
    job = await seed(session_factory, config(ai_max_reviews=1))
    assert await run_once(session_factory, "w", settings=config(ai_max_reviews=1), client=client)
    assert client.calls == 1
    async with session_factory() as s:
        row = await s.get(AnalysisJob, job)
        assert row.status == "done" and row.input_review_count == 1
        assert row.input_payload["sampling"]["source_review_count"] == 2
        assert row.input_payload["sampling"]["sampled"] is True


async def test_heartbeat_keeps_lease_alive_and_cancellation_releases_tasks(session_factory):
    job = await seed(session_factory)
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def slow(kwargs):
        started.set()
        try:
            await asyncio.sleep(10)
        finally:
            stopped.set()

    task = asyncio.create_task(
        run_once(session_factory, "w", settings=config(), lease_seconds=0.3, client=Client(slow))
    )
    await started.wait()
    await asyncio.sleep(0.5)
    async with session_factory() as s:
        row = await s.get(AnalysisJob, job)
        assert row.lease_expires_at > datetime.now(UTC)
        assert await AnalysisRepository(s).claim("other", 1) is None
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set()


async def test_heartbeat_owner_loss_cancels_request(session_factory):
    job = await seed(session_factory)
    stopped = asyncio.Event()

    async def lost(kwargs):
        async with session_factory() as s:
            await s.execute(update(AnalysisJob).values(status="stale"))
            await s.commit()
        try:
            await asyncio.sleep(10)
        finally:
            stopped.set()

    assert await run_once(
        session_factory, "w", settings=config(), lease_seconds=0.15, client=Client(lost)
    )
    assert stopped.is_set()
    async with session_factory() as s:
        assert (await s.get(AnalysisJob, job)).status == "stale"
        assert list(await s.scalars(select(ReviewAnalysisRow))) == []


async def test_analysis_read_api_pages_only_current_done_results(session_factory):
    from fastapi.testclient import TestClient

    from review_data.api.app import app

    job = await seed(session_factory)
    await run_once(session_factory, "w", settings=config(), client=Client())
    with TestClient(app) as client:
        path = "/api/v1/kurly/products/p/analysis"
        first = client.get(path, params={"limit": 1})
        assert first.status_code == 200
        body = first.json()
        assert body["status"] == "done" and body["review_count"] == 2
        assert len(body["results"]) == 1 and body["next_cursor"]
        second = client.get(path, params={"limit": 1, "cursor": body["next_cursor"]}).json()
        assert second["results"][0]["review_id"] != body["results"][0]["review_id"]
        assert client.get(path, params={"cursor": "invalid"}).status_code == 400
        assert client.get("/api/v1/kurly/products/missing/analysis").status_code == 404
        status = client.get(f"/api/v1/analysis-jobs/{job}").json()
        assert status["status"] == "done" and "input_payload" not in status
        assert client.get("/api/v1/analysis-jobs/9999999").status_code == 404
    async with session_factory() as s:
        await s.execute(update(ReviewRow).values(content="변경"))
        await s.execute(update(ProductRow).values(analysis_input_hash=None))
        await s.commit()
    with TestClient(app) as client:
        body = client.get(path).json()
        assert body["status"] == "done" and len(body["results"]) == 2
        assert body["is_current"] is False


async def test_collection_persistence_enqueues_in_same_transaction(session_factory):
    from review_data.core.db.models import CollectionJob
    from review_data.core.models import Review
    from review_data.worker.collection_worker import _Claim, _Collected, _persist

    async with session_factory() as s:
        s.add(ProductRow(platform="kurly", product_id="p", name="상품", url="https://x"))
        await s.flush()
        cjob = CollectionJob(
            platform="kurly",
            product_id="p",
            idempotency_key="test",
            status="running",
            locked_by="w",
            lease_expires_at=datetime.now(UTC) + timedelta(seconds=60),
        )
        s.add(cjob)
        await s.flush()
        cjob_id = cjob.id
        await s.commit()
    outcome = await _persist(
        session_factory,
        _Claim(cjob_id, "kurly", "p", "w"),
        _Collected(
            reviews=[Review(platform="kurly", product_id="p", review_id="r", content="좋음")]
        ),
        settings=config(),
    )
    assert outcome[1] == "succeeded"
    async with session_factory() as s:
        job = await s.scalar(select(AnalysisJob))
        assert job.status == "queued" and job.trigger_collection_job_id == cjob_id
        assert job.input_payload["reviews"][0]["content"] == "좋음"


def test_settings_reject_missing_or_credential_embedded_endpoint():
    with pytest.raises(ValueError):
        Settings(_env_file=None, ai_analysis_enabled=True)
    with pytest.raises(ValueError):
        Settings(_env_file=None, ai_stream_url="https://user:secret@example.com")
    assert "private-token" not in repr(config(ai_internal_token="private-token"))


async def test_mock_http_stream_to_persisted_database(session_factory, monkeypatch):
    import json

    import httpx

    from review_data.core.analysis_stream import AnalysisStreamClient

    job = await seed(session_factory)
    real_client = httpx.AsyncClient

    def event(name, data):
        return f"event: {name}\ndata: {json.dumps(data)}\n\n"

    def handler(request):
        body = json.loads(request.content)
        assert request.headers["x-request-id"] == str(job)
        assert request.headers["idempotency-key"] == str(job)
        assert "x-analysis-job-id" not in request.headers
        assert request.headers["x-internal-token"] == "private-token"
        assert all("author" not in r for r in body["reviews"])
        content = event(
            "meta",
            {
                "request_id": str(job),
                "ai_job_id": "ai-1",
                "model_version": "v1",
                "policy_version": "p1",
                "platform": "kurly",
                "product_id": "p",
                "review_count": 2,
                "contract_version": "v0.5",
            },
        )
        for r in body["reviews"]:
            result = results([r]).results[0]
            result["text_score"] = -1
            result["request_id"] = str(job)
            content += event("result", result)
        content += event("done", {"request_id": str(job), "ai_job_id": "ai-1", "result_count": 2})
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=content)

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    client = AnalysisStreamClient("https://test.invalid/analyze", token="private-token")
    await run_once(session_factory, "w", settings=config(), client=client)
    async with session_factory() as s:
        status = await AnalysisRepository(s).status("kurly", "p", config())
        assert status["status"] == "done" and len(status["results"]) == 2
        assert status["results"][0]["text_score"] is None
        assert status["job"]["ai_job_id"] == "ai-1"


async def test_lost_collection_owner_cannot_write_or_enqueue(session_factory):
    from review_data.core.db.models import CollectionJob
    from review_data.core.models import Product, Review
    from review_data.worker.collection_worker import _Claim, _Collected, _persist

    async with session_factory() as s:
        s.add(ProductRow(platform="kurly", product_id="p", name="원래상품", url="https://x"))
        await s.flush()
        row = CollectionJob(
            platform="kurly",
            product_id="p",
            idempotency_key="lost",
            status="running",
            locked_by="new",
            lease_expires_at=datetime.now(UTC) + timedelta(seconds=60),
        )
        s.add(row)
        await s.flush()
        job = row.id
        await s.commit()
    await _persist(
        session_factory,
        _Claim(job, "kurly", "p", "old"),
        _Collected(
            product=Product(platform="kurly", product_id="p", name="오래된상품", url="https://x"),
            reviews=[Review(platform="kurly", product_id="p", review_id="r", content="리뷰")],
        ),
        settings=config(),
    )
    async with session_factory() as s:
        assert (await s.get(ProductRow, ("kurly", "p"))).name == "원래상품"
        assert list(await s.scalars(select(ReviewRow))) == []
        assert list(await s.scalars(select(AnalysisJob))) == []


async def test_review_write_rolled_back_when_analysis_enqueue_fails(session_factory, monkeypatch):
    from sqlalchemy.exc import SQLAlchemyError

    from review_data.core.db.models import CollectionJob
    from review_data.core.models import Review
    from review_data.worker.collection_worker import _Claim, _Collected, _persist

    async with session_factory() as s:
        s.add(ProductRow(platform="kurly", product_id="p", name="상품", url="https://x"))
        await s.flush()
        row = CollectionJob(
            platform="kurly",
            product_id="p",
            idempotency_key="rollback",
            status="running",
            locked_by="w",
            lease_expires_at=datetime.now(UTC) + timedelta(seconds=60),
        )
        s.add(row)
        await s.flush()
        job = row.id
        await s.commit()

    async def fail(*args, **kwargs):
        raise SQLAlchemyError("test")

    monkeypatch.setattr(AnalysisRepository, "enqueue", fail)
    outcome = await _persist(
        session_factory,
        _Claim(job, "kurly", "p", "w"),
        _Collected(
            reviews=[Review(platform="kurly", product_id="p", review_id="r", content="리뷰")]
        ),
        settings=config(),
    )
    assert outcome[1] == "failed"
    async with session_factory() as s:
        assert list(await s.scalars(select(ReviewRow))) == []
        assert (await s.get(ProductRow, ("kurly", "p"))).reviews_last_collected_at is None


async def test_status_does_not_load_all_review_content(session_factory, monkeypatch):
    await seed(session_factory)
    await run_once(session_factory, "w", settings=config(), client=Client())

    async def forbidden(*args):
        raise AssertionError("조회는 전체 리뷰를 다시 읽지 않는다.")

    monkeypatch.setattr(AnalysisRepository, "snapshot", forbidden)
    async with session_factory() as s:
        body = await AnalysisRepository(s).status("kurly", "p", config(), ["r0"])
        assert body["status"] == "done" and len(body["results"]) == 1


async def test_snapshot_migration_roundtrip_keeps_old_rows(engine):
    from pathlib import Path
    from uuid import uuid4

    from alembic.config import Config
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from alembic.script import ScriptDirectory
    from sqlalchemy import inspect, text
    from sqlalchemy.ext.asyncio import create_async_engine

    from .conftest import DATABASE_URL

    eng = create_async_engine(DATABASE_URL)
    schema = "snapshot_" + uuid4().hex
    try:
        async with eng.connect() as conn:
            transaction = await conn.begin()
            try:
                await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
                await conn.execute(text(f'SET LOCAL search_path TO "{schema}"'))

                def migrate(sync):
                    cfg = Config()
                    cfg.set_main_option(
                        "script_location", str(Path(__file__).resolve().parents[1] / "alembic")
                    )
                    scripts = ScriptDirectory.from_config(cfg)
                    revisions = list(reversed(list(scripts.iterate_revisions("head", "base"))))
                    with Operations.context(MigrationContext.configure(sync)):
                        for revision in revisions[:-1]:
                            revision.module.upgrade()
                        sync.execute(
                            text(
                                "INSERT INTO products(platform,product_id,name,url) "
                                "VALUES('kurly','p','상품','https://x')"
                            )
                        )
                        sync.execute(
                            text(
                                "INSERT INTO reviews(platform,product_id,review_id,content) "
                                "VALUES('kurly','p','r','리뷰')"
                            )
                        )
                        sync.execute(
                            text(
                                "INSERT INTO analysis_jobs(id,platform,product_id,status) "
                                "VALUES(1,'kurly','p','done')"
                            )
                        )
                        sync.execute(
                            text(
                                "INSERT INTO review_analyses(analysis_job_id,platform,"
                                "product_id,review_id,rti) VALUES(1,'kurly','p','r',0)"
                            )
                        )
                        revisions[-1].module.upgrade()
                        assert sync.scalar(text("SELECT available_at FROM analysis_jobs"))
                        assert "analysis_input_hash" in {
                            c["name"] for c in inspect(sync).get_columns("products")
                        }
                        revisions[-1].module.downgrade()
                        assert sync.scalar(text("SELECT status FROM analysis_jobs")) == "done"
                        assert sync.scalar(text("SELECT rti FROM review_analyses")) == 0
                        assert sync.scalar(text("SELECT content FROM reviews")) == "리뷰"

                await conn.run_sync(migrate)
            finally:
                await transaction.rollback()
    finally:
        await eng.dispose()


def test_blank_optional_analysis_settings_are_unset():
    settings = config(ai_internal_token="", ai_model_version="", ai_policy_version="")
    assert settings.ai_internal_token is None
    assert settings.ai_model_version is None and settings.ai_policy_version is None


async def test_shared_read_prevents_mixed_review_analysis_pages(session_factory):
    from review_data.core.db.repository import ReviewRepository
    from review_data.core.models import Review

    await seed(session_factory)
    await run_once(session_factory, "w", settings=config(), client=Client())
    started = asyncio.Event()

    async def write():
        async with session_factory() as s:
            started.set()
            await ReviewRepository(s).upsert_many(
                "kurly",
                "p",
                [Review(platform="kurly", product_id="p", review_id="r0", content="변경된 리뷰")],
            )
            await AnalysisRepository(s).enqueue("kurly", "p", config())
            await s.commit()

    async with session_factory() as s:
        await AnalysisRepository(s).lock_product("kurly", "p", read=True)
        task = asyncio.create_task(write())
        await started.wait()
        await asyncio.sleep(0.1)
        assert not task.done()
        assert (await AnalysisRepository(s).status("kurly", "p", config()))["status"] == "done"
        assert (await s.get(ReviewRow, ("kurly", "p", "r0"))).content == "리뷰 0"
        await s.commit()
    await task
    async with session_factory() as s:
        state = await AnalysisRepository(s).status("kurly", "p", config())
        assert state["status"] == "done" and state["refresh_job"]["status"] == "queued"
        assert state["is_current"] is False


async def test_large_product_sends_sample_and_reports_coverage(session_factory):
    from fastapi.testclient import TestClient

    from review_data.api.app import app

    await seed(session_factory, count=600)
    async with session_factory() as s:
        await s.execute(update(ReviewRow).where(ReviewRow.review_id == "r0").values(rating=1))
        await s.execute(update(ReviewRow).where(ReviewRow.review_id == "r599").values(rating=5))
        job = await AnalysisRepository(s).enqueue("kurly", "p", config())
        await s.commit()
    client = Client()
    await run_once(session_factory, "w", settings=config(), client=client)
    assert client.calls == 1
    async with session_factory() as s:
        row = await s.get(AnalysisJob, job)
        assert row.input_review_count == len(row.input_payload["reviews"]) == 500
        assert {"r0", "r599"}.issubset({r["review_id"] for r in row.input_payload["reviews"]})
        body = await AnalysisRepository(s).status("kurly", "p", config())
        assert body["status"] == "done" and body["sampled"] is True
        assert body["review_count"] == 500 and body["source_review_count"] == 600
        assert body["sampling"]["source_rating_distribution"] == {"1": 1, "5": 1, "unrated": 598}
        assert body["sampling"]["sample_rating_distribution"] == {"1": 1, "5": 1, "unrated": 498}
        selected = {r["review_id"] for r in row.input_payload["reviews"]}
        unselected = sorted({f"r{i}" for i in range(600)} - selected)
        page = await AnalysisRepository(s).status("kurly", "p", config(), unselected)
        assert page["results"] == [] and page["sampled"] is True
        assert len(list(await s.scalars(select(ReviewRow)))) == 600
    with TestClient(app) as http:
        body = http.get("/api/v1/kurly/products/p/analysis", params={"limit": 100}).json()
        assert body["sampled"] is True and body["source_review_count"] == 600
        assert body["review_count"] == 500 and len(body["results"]) <= 100
        job_body = http.get(f"/api/v1/analysis-jobs/{job}").json()
        assert job_body["sampled"] is True and job_body["input_review_count"] == 500
        assert job_body["source_review_count"] == 600


async def test_unselected_review_change_invalidates_sample(session_factory):
    old = await seed(session_factory, count=600)
    await run_once(session_factory, "w", settings=config(), client=Client())
    async with session_factory() as s:
        row = await s.get(AnalysisJob, old)
        selected = {r["review_id"] for r in row.input_payload["reviews"]}
        unselected = next(f"r{i}" for i in range(600) if f"r{i}" not in selected)
        old_hash = row.input_hash
        await s.execute(
            update(ReviewRow)
            .where(ReviewRow.review_id == unselected)
            .values(content="비선택 리뷰 변경")
        )
        new = await AnalysisRepository(s).enqueue("kurly", "p", config())
        await s.commit()
        assert new != old and (await s.get(AnalysisJob, old)).status == "done"
        assert (await s.get(AnalysisJob, new)).input_hash != old_hash
        assert {
            r["review_id"] for r in (await s.get(AnalysisJob, new)).input_payload["reviews"]
        } == selected
        assert len((await AnalysisRepository(s).status("kurly", "p", config()))["results"]) == 500


async def test_sample_policy_and_limit_changes_create_new_jobs(session_factory):
    first = await seed(session_factory, count=600)
    async with session_factory() as s:
        row = await s.get(AnalysisJob, first)
        row.input_payload = {
            **row.input_payload,
            "sampling": {**row.input_payload["sampling"], "policy_version": "previous-policy"},
        }
        await s.commit()
    async with session_factory() as s:
        second = await AnalysisRepository(s).enqueue("kurly", "p", config())
        await s.commit()
        assert second != first
    async with session_factory() as s:
        assert await AnalysisRepository(s).enqueue("kurly", "p", config()) == second
        third = await AnalysisRepository(s).enqueue("kurly", "p", config(ai_max_reviews=200))
        await s.commit()
        assert third != second
        assert (await s.get(AnalysisJob, third)).input_review_count == 200


async def test_legacy_full_result_reused_and_oversized_failure_replaced(session_factory):
    old = await seed(session_factory)
    await run_once(session_factory, "w", settings=config(), client=Client())
    async with session_factory() as s:
        row = await s.get(AnalysisJob, old)
        row.input_payload = {k: v for k, v in row.input_payload.items() if k != "sampling"}
        await s.commit()
    async with session_factory() as s:
        assert await AnalysisRepository(s).enqueue("kurly", "p", config()) == old
        body = await AnalysisRepository(s).status("kurly", "p", config())
        assert body["status"] == "done" and body["sampled"] is False
        assert body["source_review_count"] == 2
        s.add_all(
            [
                ReviewRow(platform="kurly", product_id="p", review_id=f"extra{i}", content="리뷰")
                for i in range(600)
            ]
        )
        await s.flush()
        oversized = await AnalysisRepository(s).enqueue("kurly", "p", config())
        await s.commit()
    async with session_factory() as s:
        row = await s.get(AnalysisJob, oversized)
        row.status = "failed"
        row.input_review_count = 602
        row.input_payload = {k: v for k, v in row.input_payload.items() if k != "sampling"}
        row.input_payload = {**row.input_payload, "reviews": []}
        await s.commit()
    async with session_factory() as s:
        replacement = await AnalysisRepository(s).enqueue("kurly", "p", config())
        await s.commit()
        assert replacement != oversized
        assert (await s.get(AnalysisJob, replacement)).status == "queued"
        assert (await s.get(AnalysisJob, replacement)).input_review_count == 500
