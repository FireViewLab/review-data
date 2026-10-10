"""운영 인증 범위와 실제 요청·결과 매칭, heartbeat·자원 계측을 검증한다."""

import asyncio
import importlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from review_data.api import status
from review_data.core.db.models import (
    AnalysisJob,
    CollectionJob,
    ProductRow,
    ReviewAnalysisRow,
    ReviewRow,
    WorkerHeartbeat,
)
from review_data.core.service.runtime import ResourceSampler, runtime_heartbeat
from review_data.core.settings import Settings, get_settings

module = importlib.import_module("review_data.api.app")


@pytest.fixture
def dashboard_settings(monkeypatch):
    settings = Settings(
        _env_file=None, database_url=get_settings().database_url, internal_token="data-only-secret"
    )
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    monkeypatch.setattr(status, "get_settings", lambda: settings)
    return settings


def test_login_cookie_is_scoped_and_logout_revokes_browser_session(engine, dashboard_settings):
    with TestClient(module.app) as client:
        page = client.get("/status")
        assert page.status_code == 200
        assert "운영 개요" in page.text
        assert "data-only-secret" not in page.text
        assert client.get("/status/api/overview").status_code == 401
        assert client.post("/status/login", json={"token": "wrong"}).status_code == 401
        login = client.post("/status/login", json={"token": "data-only-secret"})
        assert login.status_code == 200
        assert "HttpOnly" in login.headers["set-cookie"]
        assert "Path=/status" in login.headers["set-cookie"]
        assert "SameSite=strict" in login.headers["set-cookie"]
        assert "data-only-secret" not in login.headers["set-cookie"]
        summary = client.get("/status/api/overview")
        assert summary.status_code == 200, summary.text
        assert summary.headers["cache-control"] == "no-store"
        assert client.get("/api/v1/catalog").status_code == 401
        assert client.get("/status/api/jobs/analysis?limit=101").status_code == 422
        assert client.get("/status/api/jobs/analysis/99999").status_code == 404
        client.post("/status/logout")
        assert client.get("/status/api/overview").status_code == 401


def test_expired_and_tampered_sessions_fail(engine, dashboard_settings):
    with TestClient(module.app) as client:
        for value in [
            status.session_value("data-only-secret", 1),
            "bad",
            status.session_value("other", int(datetime.now(UTC).timestamp()) + 60),
        ]:
            client.cookies.set(status.COOKIE, value, path="/status")
            assert client.get("/status/api/overview").status_code == 401
        assert (
            client.get(
                "/status/api/overview", headers={"X-Internal-Token": "data-only-secret"}
            ).status_code
            == 200
        )


def test_backfill_controls_authenticated_persisted_and_resumable(_clean_db, dashboard_settings):
    dashboard_settings.ai_stream_url = "https://example.invalid/stream"
    dashboard_settings.ai_model_version = "old-model"
    dashboard_settings.ai_policy_version = "old-policy"
    target = {"model_version": "new-model", "policy_version": "new-policy"}
    path = "/status/api/analysis-refresh/"
    with TestClient(module.app) as client:
        for action in ["start", "pause", "retry-failed"]:
            assert client.post(path + action, json=target).status_code == 401
        client.post("/status/login", json={"token": "data-only-secret"})
        assert client.post(path + "start", json=target).status_code == 409
        dashboard_settings.ai_analysis_enabled = True
        for bad in ["", " ", "<script>", "v" * 201]:
            assert (
                client.post(path + "start", json={**target, "model_version": bad}).status_code
                == 422
            )
        assert (
            client.post(
                path + "start", json=target, headers={"Origin": "https://other.example"}
            ).status_code
            == 403
        )
        first = client.post(path + "start", json=target, headers={"Origin": "http://testserver"})
        assert first.status_code == 200, first.text
        assert first.json()["control"]["model_version"] == "new-model"
        campaign_id = first.json()["campaign_id"]
        overview = client.get("/status/api/overview").json()
        assert overview["settings"]["target_model_version"] == "new-model"
        assert overview["settings"]["analysis_refresh_enabled"]
        assert overview["analysis_control"]["source"] == "dashboard"
        assert client.post(path + "pause").json()["control"]["enabled"] is False
        assert (
            client.get("/status/api/overview").json()["settings"]["analysis_refresh_enabled"]
            is False
        )
        assert client.post(path + "retry-failed").status_code == 409
        assert client.post(path + "start", json=target).json()["campaign_id"] == campaign_id
        assert client.post(path + "retry-failed").status_code == 200
    with TestClient(module.app) as restarted:
        restarted.post("/status/login", json={"token": "data-only-secret"})
        current = restarted.get("/status/api/overview").json()
        assert current["analysis_control"]["model_version"] == "new-model"
        assert dashboard_settings.ai_model_version == "old-model"
        assert not dashboard_settings.analysis_refresh_enabled


async def test_detail_uses_immutable_input_and_preserves_unavailable_results(
    session, session_factory, dashboard_settings
):
    now = datetime.now(UTC)
    session.add(ProductRow(platform="kurly", product_id="p", name="<상품>", url="https://x"))
    await session.flush()
    session.add(
        ReviewRow(
            platform="kurly",
            product_id="p",
            review_id="r",
            content="현재 DB에서 바뀐 본문",
            rating=5,
        )
    )
    job = AnalysisJob(
        platform="kurly",
        product_id="p",
        status="done",
        input_review_count=2,
        input_payload={
            "reviews": [
                {"review_id": "r", "content": "<script>요청 원본</script>", "rating": 4},
                {"review_id": "r2", "content": "다음 리뷰"},
            ],
            "sampling": {"source_review_count": 800, "analyzed_review_count": 2},
            "secret": "hidden-input-extra",
        },
        result={"review_count": 1},
        last_error="hidden-error-token",
    )
    session.add(job)
    collection = CollectionJob(platform="kurly", product_id="p", idempotency_key="p")
    session.add(collection)
    session.add(WorkerHeartbeat(worker_id="idle", role="collection", last_seen_at=now))
    session.add(
        WorkerHeartbeat(worker_id="old", role="analysis", last_seen_at=now - timedelta(minutes=1))
    )
    session.add_all(
        [
            WorkerHeartbeat(worker_id="stopped", role="analysis", last_seen_at=now, stopped_at=now),
            WorkerHeartbeat(
                worker_id="expired", role="scheduler", last_seen_at=now - timedelta(minutes=6)
            ),
        ]
    )
    await session.flush()
    session.add(
        ReviewAnalysisRow(
            analysis_job_id=job.id,
            platform="kurly",
            product_id="p",
            review_id="r",
            rti=70,
            level="safe",
            text_score=70,
            behavior_score=None,
            network_score=None,
            reasons=["사유"],
        )
    )
    await session.commit()
    # TestClient owns a different event loop; use its lifespan factory for HTTP queries.
    with TestClient(module.app) as client:
        headers = {"X-Internal-Token": "data-only-secret"}
        summary = client.get("/status/api/overview", headers=headers)
        assert summary.status_code == 200, summary.text
        workers = {w["worker_id"]: w for w in summary.json()["workers"]}
        assert workers["idle"]["alive"] is True
        assert workers["old"]["alive"] is False
        assert set(workers) == {"idle", "old"}
        response = client.get(f"/status/api/jobs/analysis/{job.id}?limit=1", headers=headers)
        assert response.status_code == 200, response.text
        detail = response.json()
        assert detail["reviews"][0]["request"]["content"] == "<script>요청 원본</script>"
        assert detail["reviews"][0]["result"]["behavior_score"] is None
        assert detail["next_offset"] == 1 and detail["total"] == 2
        assert detail["request_headers"]["Idempotency-Key"] == str(job.id)
        assert "hidden-error-token" not in response.text
        assert "hidden-input-extra" not in response.text
        second = client.get(f"/status/api/jobs/analysis/{job.id}?offset=1", headers=headers).json()
        assert second["reviews"][0]["result"] is None
        for endpoint in [
            "/status/api/jobs/collection",
            "/status/api/jobs/analysis",
            "/status/api/jobs/analysis?status=done&q=商品",
            "/status/api/products",
            "/status/api/products/kurly/p/reviews",
            f"/status/api/jobs/collection/{collection.id}",
        ]:
            result = client.get(endpoint, headers=headers)
            assert result.status_code == 200, result.text
            assert "hidden-error-token" not in result.text
        product = client.get("/status/api/products", headers=headers).json()["items"][0]
        assert product["stored_reviews"] == 1


async def test_worker_heartbeat_is_visible_while_idle_and_marks_stop(session_factory):
    async with runtime_heartbeat(session_factory, "test-worker", "collection"):
        await asyncio.sleep(0.1)
        async with session_factory() as session:
            row = await session.get(WorkerHeartbeat, "test-worker")
            assert row is not None and row.stopped_at is None
    async with session_factory() as session:
        assert (await session.get(WorkerHeartbeat, "test-worker")).stopped_at is not None


def test_cpu_ticks_and_memory_available(tmp_path):
    proc = tmp_path
    (proc / "stat").write_text("cpu  20 0 10 70 0 0 0 0 5 0\ncpu0 20 0 10 70\n")
    (proc / "meminfo").write_text("MemTotal: 1000 kB\nMemAvailable: 400 kB\n")
    (proc / "loadavg").write_text("0.5 0.4 0.3 1/2 3")
    sampler = ResourceSampler(Path(proc))
    assert sampler.sample()["cpu_percent"] is None
    (proc / "stat").write_text("cpu  40 0 20 140 0 0 0 0 10 0\ncpu0 40 0 20 140\n")
    data = sampler.sample()
    assert data["cpu_percent"] == 30
    assert data["memory"]["percent"] == 60
    assert data["cpu_count"] == 1
    assert data["load"] == [0.5, 0.4, 0.3]
    assert ResourceSampler(Path("/nonexistent")).sample()["memory"] is None
    assert "token" not in json.dumps(data)


async def test_result_count_uses_db_target_latest_success_per_product(session, dashboard_settings):
    from review_data.core.db.models import AnalysisRefreshControl

    now = datetime.now(UTC)
    session.add(
        AnalysisRefreshControl(id=1, model_version="new", policy_version="p2", enabled=False)
    )
    session.add(ProductRow(platform="kurly", product_id="p", name="상품", url="https://x"))
    await session.flush()
    session.add_all(
        [
            ReviewRow(platform="kurly", product_id="p", review_id=str(i), content="리뷰")
            for i in range(3)
        ]
    )
    await session.flush()
    for model, policy, state, count, minute in [
        ("old", "p2", "done", 3, 0),
        ("new", "p1", "done", 3, 1),
        ("new", "p2", "done", 3, 2),
        ("new", "p2", "stale", 2, 3),
        ("new", "p2", "failed", 3, 4),
    ]:
        job = AnalysisJob(
            platform="kurly",
            product_id="p",
            status=state,
            model_version=model,
            policy_version=policy,
            completed_at=now + timedelta(minutes=minute),
            result={"review_count": count},
        )
        session.add(job)
        await session.flush()
        session.add_all(
            [
                ReviewAnalysisRow(
                    analysis_job_id=job.id, platform="kurly", product_id="p", review_id=str(i)
                )
                for i in range(count)
            ]
        )
    await session.commit()
    with TestClient(module.app) as client:
        response = client.get(
            "/status/api/overview", headers={"X-Internal-Token": "data-only-secret"}
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["totals"]["analysis_results"] == 2
        assert body["settings"]["target_model_version"] == "new"
        assert body["settings"]["target_policy_version"] == "p2"


async def test_result_count_without_target_is_zero(session, dashboard_settings):
    session.add(ProductRow(platform="kurly", product_id="p", name="상품", url="https://x"))
    await session.flush()
    session.add(ReviewRow(platform="kurly", product_id="p", review_id="r", content="리뷰"))
    job = AnalysisJob(
        platform="kurly",
        product_id="p",
        status="done",
        model_version="old",
        policy_version="p1",
        completed_at=datetime.now(UTC),
    )
    session.add(job)
    await session.flush()
    session.add(
        ReviewAnalysisRow(analysis_job_id=job.id, platform="kurly", product_id="p", review_id="r")
    )
    await session.commit()
    with TestClient(module.app) as client:
        response = client.get(
            "/status/api/overview", headers={"X-Internal-Token": "data-only-secret"}
        )
        assert response.status_code == 200
        assert response.json()["totals"]["analysis_results"] == 0
