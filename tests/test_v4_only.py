"""v4 단독 분석: v4 계약 워커 저장, 실패 구분, 설정 검증, 기존 v4 결과 인정."""

import httpx
import pytest
from sqlalchemy import select

from review_data.core import v4_client
from review_data.core.db.analysis_repository import (
    AnalysisRepository,
    payload_signature,
    target_signature,
)
from review_data.core.db.models import AnalysisJob, AnalysisRefreshControl, ReviewAnalysisRow
from review_data.core.service import v4_adopt
from review_data.core.settings import Settings
from review_data.worker.analysis_worker import run_once
from tests.test_analysis_pipeline import Client, config, seed, v2_results
from tests.test_v4_results import _v4

MODEL, POLICY = "ptext-koelectra-v4.0", "text-first-v1"


def _v4_settings(**extra):
    return config(
        ai_contract_version="v4",
        ai_model_version=MODEL,
        ai_policy_version=POLICY,
        v4_analyze_url="http://v4.invalid/v4/analyze",
        v4_internal_token="secret",
        **extra,
    )


async def test_v4_contract_stores_v4_results_without_v3(session_factory, monkeypatch):
    settings = _v4_settings()
    await seed(session_factory, settings)
    calls = []

    async def fake_analyze(url, token, platform, product_id, reviews, **kwargs):
        calls.append((url, token))
        return _v4(reviews, level="DANGER")

    monkeypatch.setattr(v4_client, "analyze", fake_analyze)
    assert await run_once(session_factory, "w", settings=settings)
    # v4 계약에서는 v4를 한 번만 부른다(덧붙여 채우는 호출이 없다).
    assert calls == [("http://v4.invalid/v4/analyze", "secret")]
    async with session_factory() as s:
        job = await s.scalar(select(AnalysisJob))
        status = await AnalysisRepository(s).status("kurly", "p", settings)
    assert job.status == "done" and job.model_version == MODEL and job.policy_version == POLICY
    assert job.v4_completed_at is not None and job.result["contract_version"] == "v4"
    assert status["is_current"]
    row = status["results"][0]
    assert row["level_v4"] == "DANGER" and row["policy_index_v4"] == 61.2
    assert row["policy_index_v3"] is None and row["evidence_level_v3"] is None


@pytest.mark.parametrize(
    ("error", "queued", "label"),
    [
        (v4_client.V4Error("x", kind="transport", retryable=True), True, "AI 서버 연결 실패"),
        (v4_client.V4Error("x", kind="http", retryable=True), True, "AI 서버 HTTP 오류"),
        (v4_client.V4Error("x"), False, "AI 응답 검증 실패"),
    ],
)
async def test_v4_errors_keep_failure_kinds(session_factory, monkeypatch, error, queued, label):
    settings = _v4_settings()
    await seed(session_factory, settings)

    async def broken(*args, **kwargs):
        raise error

    monkeypatch.setattr(v4_client, "analyze", broken)
    assert await run_once(session_factory, "w", settings=settings)
    async with session_factory() as s:
        job = await s.scalar(select(AnalysisJob))
    assert job.status == ("queued" if queued else "failed") and job.last_error == label


@pytest.mark.parametrize(
    ("code", "retryable"), [(503, True), (401, True), (429, True), (422, False)]
)
async def test_v4_http_status_retryability(code, retryable):
    transport = httpx.MockTransport(lambda request: httpx.Response(code))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(v4_client.V4Error) as caught:
            await v4_client.analyze(
                "http://v4.invalid",
                "t",
                "kurly",
                "p",
                [{"review_id": "a", "content": "좋아요"}],
                client=client,
            )
    assert caught.value.kind == "http" and caught.value.retryable is retryable


def test_v4_contract_needs_v4_url_not_stream_url():
    base = {"_env_file": None, "ai_analysis_enabled": True, "ai_contract_version": "v4"}
    ok = Settings(**base, v4_analyze_url="http://v4.invalid/v4/analyze", v4_internal_token="t")
    assert ok.ai_stream_url is None
    with pytest.raises(ValueError):
        Settings(**base, ai_stream_url="http://test.invalid/analyze")


async def test_adopt_marks_filled_jobs_current_without_reanalysis(session_factory, monkeypatch):
    # v3.1 계약 시절: v3.1 결과 저장 뒤 v4를 덧붙여 채웠다.
    old = config(
        ai_contract_version="v2",
        ai_model_version="v1",
        ai_policy_version="p1",
        v4_analyze_url="http://v4.invalid/v4/analyze",
        v4_internal_token="secret",
    )
    await seed(session_factory, old)

    async def fake_analyze(url, token, platform, product_id, reviews, **kwargs):
        return _v4(reviews)

    monkeypatch.setattr(v4_client, "analyze", fake_analyze)

    async def respond(kwargs):
        return v2_results(kwargs["reviews"])

    assert await run_once(session_factory, "w", settings=old, client=Client(respond))
    async with session_factory() as s:
        s.add(AnalysisRefreshControl(id=1, model_version="v1", policy_version="p1", enabled=False))
        await s.commit()

    async with session_factory() as s:
        assert await v4_adopt.adopt(
            s, model_version=MODEL, policy_version=POLICY, dry_run=True
        ) == {"dry_run": True, "candidates": 1}
        await s.rollback()
    async with session_factory() as s:
        result = await v4_adopt.adopt(s, model_version=MODEL, policy_version=POLICY)
        await s.commit()
    assert result["adopted"] == 1 and result["dashboard_target_updated"]

    settings = _v4_settings()
    async with session_factory() as s:
        job = await s.scalar(select(AnalysisJob))
        control = await s.get(AnalysisRefreshControl, 1)
        rows = (await s.scalars(select(ReviewAnalysisRow))).all()
        # 같은 입력이면 새 작업을 만들지 않는다(재분석 없음).
        assert await AnalysisRepository(s).enqueue("kurly", "p", settings) == job.id
        status = await AnalysisRepository(s).status("kurly", "p", settings)
    assert job.model_version == MODEL and job.input_payload["model_version"] == MODEL
    # 입력 서명도 바뀐 입력 기준으로 고쳐 재분석 대상 판정이 JSON을 열지 않는다.
    assert job.input_signature == payload_signature(job.input_payload)
    assert job.input_signature == target_signature(settings)
    assert job.result["adopted_from"] == {
        "model_version": "v1",
        "policy_version": "p1",
        "contract_version": "v2",
    }
    assert job.result["contract_version"] == "v4"
    assert (control.model_version, control.policy_version) == (MODEL, POLICY)
    assert all(r.policy_index_v3 == 80.4 for r in rows)  # 기존 칸 값은 지우지 않는다
    assert status["is_current"]
    # 두 번 돌려도 바뀌는 것이 없다.
    async with session_factory() as s:
        assert (await v4_adopt.adopt(s, model_version=MODEL, policy_version=POLICY))["adopted"] == 0


def test_public_responses_carry_v4_fields_only():
    from review_data.api.v1 import PUBLIC_RESULT_FIELDS, _public_analysis, _public_catalog_analysis

    row = {k: 1 for k in PUBLIC_RESULT_FIELDS} | {
        "rti": 50,
        "policy_index_v3": 80.4,
        "level": "warn",
    }
    body = _public_analysis({"status": "done", "results": [row]})
    assert set(body["results"][0]) == set(PUBLIC_RESULT_FIELDS)
    summary = _public_catalog_analysis(
        {
            "status": "done",
            "avg_rti": 50,
            "avg_policy_index_v3": 80,
            "scored_review_count": 9,
            "v4_level_counts": {"SAFE": 2, "WARN": 1, "DANGER": 0},
        }
    )
    assert "avg_rti" not in summary and "avg_policy_index_v3" not in summary
    assert summary["scored_review_count"] == 3
