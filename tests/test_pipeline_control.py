"""운영 제어(수집·AI 분석 요청 중지)와 실패 분석 복구를 실제 PostgreSQL에서 검증한다."""

import importlib
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from review_data.api import status
from review_data.core.db.analysis_repository import (
    FAILURE_REASONS,
    failure_summary,
    latest_recovery,
    recover_failures,
)
from review_data.core.db.models import AnalysisJob, AnalysisRecovery, CollectionJob, ProductRow
from review_data.core.db.pipeline_control import pipeline_state, set_paused
from review_data.core.service.scheduling import SchedulingService
from review_data.core.settings import Settings, get_settings
from review_data.worker import analysis_worker, collection_worker

module = importlib.import_module("review_data.api.app")
NOW = datetime.now(UTC)


def config(**values):
    return Settings(
        _env_file=None,
        ai_analysis_enabled=True,
        ai_stream_url="http://test.invalid/analyze",
        ai_model_version="m1",
        ai_policy_version="p1",
        **values,
    )


@pytest.fixture
def dashboard_settings(monkeypatch):
    settings = Settings(
        _env_file=None,
        database_url=get_settings().database_url,
        internal_token="data-only-secret",
        ai_model_version="m1",
        ai_policy_version="p1",
    )
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    monkeypatch.setattr(status, "get_settings", lambda: settings)
    return settings


async def test_state_defaults_and_toggles(session):
    assert (await pipeline_state(session))["analysis_paused"] is False
    state = await set_paused(session, "analysis", True)
    assert state["analysis_paused"] is True and state["collection_paused"] is False
    assert state["analysis_updated_at"] is not None
    state = await set_paused(session, "analysis", False)
    assert state["analysis_paused"] is False
    with pytest.raises(ValueError):
        await set_paused(session, "unknown", True)


async def test_paused_workers_and_scheduler_send_nothing(session_factory):
    async with session_factory() as s:
        s.add(ProductRow(platform="kurly", product_id="p", name="상품", url="https://x"))
        await s.flush()
        s.add(AnalysisJob(platform="kurly", product_id="p", status="queued", input_hash="h"))
        s.add(CollectionJob(platform="kurly", product_id="p", idempotency_key="k"))
        await set_paused(s, "analysis", True)
        await set_paused(s, "collection", True)
        await s.commit()

    class Client:
        async def analyze(self, **kwargs):
            raise AssertionError("중지 중에는 AI 서버에 요청하지 않는다")

    assert not await analysis_worker.run_once(
        session_factory, "w", settings=config(), client=Client()
    )
    assert not await collection_worker.run_once(session_factory, "w", settings=config())
    async with session_factory() as s:
        result = await SchedulingService(s, config()).run_once()
        assert result.created == 0
        # 대기 중인 작업은 지우지 않는다.
        assert (await s.scalar(select(AnalysisJob))).status == "queued"
        assert (await s.scalar(select(CollectionJob))).status == "pending"


async def _failed(s, product_id, error, minutes_ago=30, model="m1"):
    job = AnalysisJob(
        platform="kurly",
        product_id=product_id,
        status="failed",
        input_payload={"model_version": model, "policy_version": "p1", "reviews": []},
        input_hash=f"h-{product_id}",
        input_review_count=3,
        attempt_count=3,
        last_error=error,
        completed_at=NOW - timedelta(minutes=minutes_ago),
    )
    s.add(job)
    await s.flush()
    return job.id


async def test_recover_only_latest_ai_side_failures_with_new_ids(session):
    for pid in ("a", "b", "c", "d", "e", "f"):
        session.add(ProductRow(platform="kurly", product_id=pid, name="상품", url="https://x"))
    await session.flush()
    http = FAILURE_REASONS["http"]
    recoverable = await _failed(session, "a", http)
    await _failed(session, "b", FAILURE_REASONS["validation"])  # 다시 보내도 같은 결과
    await _failed(session, "c", http, minutes_ago=60 * 30)  # 기간 밖
    await _failed(session, "d", http, model="old")  # 이전 목표 버전
    await _failed(session, "e", http)
    session.add(AnalysisJob(platform="kurly", product_id="e", status="done"))  # 더 새 작업
    await _failed(session, "f", FAILURE_REASONS["timeout"])
    session.add(AnalysisJob(platform="kurly", product_id="f", status="queued"))
    await session.flush()

    since = NOW - timedelta(hours=24)
    summary = await failure_summary(session, since, config())
    # e·f 는 최신 작업이 done·queued 라 빠진다. d 는 이전 목표 버전이라 복구 대상이 아니다.
    assert summary["total"] == 3 and summary["recoverable"] == 1
    assert {r["kind"]: r["count"] for r in summary["by_reason"]} == {"http": 2, "validation": 1}

    assert await recover_failures(session, since, config()) == 1
    new = await session.scalar(
        select(AnalysisJob).where(AnalysisJob.product_id == "a", AnalysisJob.status == "queued")
    )
    assert new.id > recoverable and new.attempt_count == 0
    assert new.input_hash == "h-a" and new.input_review_count == 3
    # 다시 눌러도 같은 상품을 두 번 예약하지 않는다.
    assert await recover_failures(session, since, config()) == 0


def test_status_controls_live_state_and_direct_api(engine, dashboard_settings):
    with TestClient(module.app) as client:
        assert client.post("/status/api/pipeline/analysis/pause").status_code == 401
        client.post("/status/login", json={"token": "data-only-secret"})
        live = client.get("/status/api/live").json()
        assert live["pipeline_control"]["collection_paused"] is False
        paused = client.post("/status/api/pipeline/collection/pause")
        assert paused.status_code == 200 and paused.json()["collection_paused"] is True
        assert client.get("/status/api/live").json()["pipeline_control"]["collection_paused"]
        # 수집 중지 중에는 쇼핑몰에 직접 요청하는 API도 거절한다.
        headers = {"X-Internal-Token": "data-only-secret"}
        assert client.get("/kurly/search?keyword=x", headers=headers).status_code == 503
        resumed = client.post("/status/api/pipeline/collection/resume")
        assert resumed.json()["collection_paused"] is False
        assert client.post("/status/api/pipeline/other/pause").status_code == 422
        failures = client.get("/status/api/analysis/failures?hours=24")
        assert failures.status_code == 200 and failures.json()["total"] == 0
        recovered = client.post("/status/api/analysis/failures/recover", json={"hours": 24})
        assert recovered.json() == {"recovered": 0}
        assert client.get("/status/api/analysis/failures?hours=0").status_code == 422


async def test_recovery_is_recorded_and_progress_reported(session):
    for pid in ("a", "b", "c"):
        session.add(ProductRow(platform="kurly", product_id=pid, name="상품", url="https://x"))
    await session.flush()
    for pid in ("a", "b", "c"):
        await _failed(session, pid, FAILURE_REASONS["http"])
    since = NOW - timedelta(hours=24)
    assert await latest_recovery(session, datetime.now(UTC)) is None
    assert await recover_failures(session, since, config(), hours=24) == 3
    recovery = await session.scalar(select(AnalysisRecovery))
    assert recovery.hours == 24 and len(recovery.job_ids) == 3

    jobs = {j.product_id: j for j in (await session.scalars(
        select(AnalysisJob).where(AnalysisJob.id.in_(recovery.job_ids))
    ))}
    jobs["a"].status, jobs["a"].completed_at = "done", datetime.now(UTC)
    jobs["b"].status = "stale"
    await session.flush()
    progress = await latest_recovery(session, datetime.now(UTC))
    assert progress["total"] == 3
    assert (progress["done"], progress["superseded"], progress["queued"]) == (1, 1, 1)
    # 최근 15분에 1건 완료, 남은 1건 → 약 15분.
    assert progress["recent_done_15m"] == 1 and progress["estimated_remaining_seconds"] == 900
    # 복구 건수가 0이면 기록하지 않는다.
    assert await recover_failures(session, since, config(), hours=24) == 0
    assert len(list(await session.scalars(select(AnalysisRecovery)))) == 1
