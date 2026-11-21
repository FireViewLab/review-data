"""v4.0 결과: 응답 검증, v3.1 저장 뒤 채우기, 실패 격리, 백필."""

import httpx
import pytest

from review_data.core import v4_client
from review_data.core.db.analysis_repository import AnalysisRepository
from review_data.core.service import v4_backfill
from review_data.worker.analysis_worker import run_once
from tests.test_analysis_pipeline import Client, config, seed, v2_results


def _v4(reviews, level="WARN"):
    return v4_client.V4Response(
        "ptext-koelectra-v4.0",
        "text-first-v1",
        [
            v4_client.V4Result(r["review_id"], 0.42, 58, 61.2, level)
            for r in reviews
            if r.get("content")
        ],
    )


def _settings(**extra):
    return config(
        ai_contract_version="v2",
        v4_analyze_url="http://v4.invalid/v4/analyze",
        v4_internal_token="secret",
        **extra,
    )


def test_parse_rejects_wrong_ids_and_out_of_range_values():
    ok = {
        "model_version": "m",
        "policy_version": "p",
        "results": [
            {
                "review_id": "a",
                "text_probability": 0.1,
                "text_score": 90,
                "policy_index": 91.0,
                "level": "SAFE",
            }
        ],
    }
    assert v4_client.parse(ok, {"a"}).results[0].level == "SAFE"
    with pytest.raises(v4_client.V4Error):
        v4_client.parse(ok, {"a", "b"})
    bad = {**ok, "results": [{**ok["results"][0], "policy_index": 120}]}
    with pytest.raises(v4_client.V4Error):
        v4_client.parse(bad, {"a"})


async def test_analyze_sends_token_and_skips_blank_content():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["token"] = request.headers["X-Internal-Token"]
        body = request.read().decode()
        seen["body"] = body
        return httpx.Response(
            200,
            json={
                "model_version": "m",
                "policy_version": "p",
                "results": [
                    {
                        "review_id": "a",
                        "text_probability": 0.9,
                        "text_score": 10,
                        "policy_index": 20.0,
                        "level": "DANGER",
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        response = await v4_client.analyze(
            "http://v4.invalid/v4/analyze",
            "secret",
            "kurly",
            "p",
            [{"review_id": "a", "content": "본문"}, {"review_id": "b", "content": " "}],
            client=client,
        )
    assert seen["token"] == "secret" and '"b"' not in seen["body"]
    assert response.results[0].level == "DANGER"


async def test_v4_filled_after_v3_and_failure_does_not_fail_job(session_factory, monkeypatch):
    await seed(session_factory)

    async def respond(kwargs):
        return v2_results(kwargs["reviews"])

    async def fake_analyze(url, token, platform, product_id, reviews, **kwargs):
        return _v4(reviews)

    monkeypatch.setattr(v4_client, "analyze", fake_analyze)
    settings = _settings()
    assert await run_once(session_factory, "w", settings=settings, client=Client(respond))
    async with session_factory() as s:
        status = await AnalysisRepository(s).status("kurly", "p", settings)
    row = status["results"][0]
    assert status["status"] == "done"
    assert row["evidence_level_v3"] == "SAFE"  # v3.1 결과는 그대로
    assert row["level_v4"] == "WARN" and row["policy_index_v4"] == 61.2
    assert row["model_version_v4"] == "ptext-koelectra-v4.0"


async def test_v4_failure_leaves_v3_done_and_backfill_fills_later(session_factory, monkeypatch):
    await seed(session_factory)

    async def respond(kwargs):
        return v2_results(kwargs["reviews"])

    async def broken(*args, **kwargs):
        raise v4_client.V4Error("down")

    monkeypatch.setattr(v4_client, "analyze", broken)
    settings = _settings(ai_model_version="v1")
    assert await run_once(session_factory, "w", settings=settings, client=Client(respond))
    async with session_factory() as s:
        status = await AnalysisRepository(s).status("kurly", "p", settings)
    assert status["status"] == "done" and status["results"][0]["level_v4"] is None

    async def fake_analyze(url, token, platform, product_id, reviews, **kwargs):
        return _v4(reviews, level="SAFE")

    monkeypatch.setattr(v4_client, "analyze", fake_analyze)
    summary = await v4_backfill.run(session_factory, settings, concurrency=2)
    assert summary == {"jobs": 1, "reviews": 2, "failed_jobs": 0}
    async with session_factory() as s:
        status = await AnalysisRepository(s).status("kurly", "p", settings)
    assert {r["level_v4"] for r in status["results"]} == {"SAFE"}
    # 다 채워졌으면 다시 돌려도 할 일이 없다.
    assert (await v4_backfill.run(session_factory, settings))["jobs"] == 0


def test_v4_url_requires_token():
    with pytest.raises(ValueError):
        config(v4_analyze_url="http://v4.invalid/v4/analyze")
