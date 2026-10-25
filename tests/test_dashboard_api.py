"""운영 화면 처리량 시계열과 플랫폼별 수집 상태를 실제 PostgreSQL에서 검증한다."""

import importlib
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from review_data.api import status
from review_data.core.db.models import AnalysisJob, CollectionJob, ProductRow, ReviewRow
from review_data.core.service.dashboard import RANGES, platform_status, timeseries
from review_data.core.settings import Settings, get_settings

module = importlib.import_module("review_data.api.app")
NOW = datetime(2026, 10, 25, 12, 7, tzinfo=UTC)


def job(platform, pid, status, minutes_ago, kind=None, error=None):
    return CollectionJob(
        platform=platform,
        product_id=pid,
        idempotency_key=f"{platform}{pid}{minutes_ago}",
        status=status,
        failure_kind=kind,
        last_error=error,
        completed_at=NOW - timedelta(minutes=minutes_ago),
    )


async def test_timeseries_buckets_counts_by_range(session):
    session.add(ProductRow(platform="kurly", product_id="p", name="상품", url="https://x"))
    await session.flush()
    for i, minutes in enumerate((2, 3, 40)):
        session.add(
            ReviewRow(
                platform="kurly",
                product_id="p",
                review_id=f"r{i}",
                content="x",
                first_collected_at=NOW - timedelta(minutes=minutes),
            )
        )
    session.add_all(
        [
            AnalysisJob(
                platform="kurly",
                product_id="p",
                status="done",
                input_review_count=7,
                completed_at=NOW - timedelta(minutes=1),
            ),
            AnalysisJob(
                platform="kurly",
                product_id="p",
                status="failed",
                completed_at=NOW - timedelta(minutes=4),
            ),
            job("kurly", "p", "succeeded", 2),
            job("musinsa", "m", "partial", 3),
            job("musinsa", "m", "succeeded", 70),  # 1시간 밖
        ]
    )
    await session.flush()
    data = await timeseries(session, "1h", NOW)
    assert data["bucket_seconds"] == 300 and len(data["buckets"]) == 12
    last = data["buckets"][-1]  # 12:05~ 진행 중 버킷
    assert last["bucket"] == "2026-10-25T12:05:00+00:00"
    assert (last["new_reviews"], last["analysis_done"], last["analyzed_reviews"]) == (1, 1, 7)
    previous = data["buckets"][-2]
    assert previous["new_reviews"] == 1 and previous["analysis_failed"] == 1
    assert previous["per_platform"] == {"musinsa": {"succeeded": 0, "failed": 1}}
    assert last["per_platform"] == {"kurly": {"succeeded": 1, "failed": 0}}
    assert data["platforms"] == ["kurly", "musinsa"]
    assert sum(b["collection_succeeded"] for b in data["buckets"]) == 1
    for key, (span, step) in RANGES.items():
        assert span / step <= 72, key
    # 끝난 구간은 기억해 둔 값을 쓰고, 진행 중인 구간만 다시 센다.
    session.add(
        ReviewRow(
            platform="kurly",
            product_id="p",
            review_id="late",
            content="x",
            first_collected_at=NOW - timedelta(minutes=8),
        )
    )
    session.add(
        ReviewRow(
            platform="kurly",
            product_id="p",
            review_id="now",
            content="x",
            first_collected_at=NOW - timedelta(minutes=1),
        )
    )
    await session.flush()
    again = await timeseries(session, "1h", NOW)
    assert again["buckets"][-2]["new_reviews"] == 1 and again["buckets"][-1]["new_reviews"] == 2


async def test_platform_status_states_and_failure_kinds(session):
    now = datetime.now(UTC)
    session.add_all(
        [
            CollectionJob(
                platform="kurly",
                product_id="k",
                idempotency_key="1",
                status="succeeded",
                completed_at=now - timedelta(minutes=5),
            ),
            CollectionJob(
                platform="oliveyoung",
                product_id="o",
                idempotency_key="2",
                status="partial",
                failure_kind="blocked",
                completed_at=now - timedelta(minutes=5),
            ),
            CollectionJob(
                platform="musinsa",
                product_id="m",
                idempotency_key="3",
                status="failed",
                failure_kind="unknown",
                last_error="unexpected: HTTPStatusError: Client error '429 Too Many Requests'",
                completed_at=now - timedelta(hours=3),
            ),
            CollectionJob(
                platform="musinsa", product_id="n", idempotency_key="4", status="pending"
            ),
        ]
    )
    await session.flush()
    settings = Settings(_env_file=None, schedule_excluded_platforms="coupang")
    labels = {"kurly": "컬리", "oliveyoung": "올리브영", "musinsa": "무신사", "coupang": "쿠팡"}
    items = {i["platform"]: i for i in (await platform_status(session, settings, labels))["items"]}
    assert items["kurly"]["state"] == "normal" and items["kurly"]["succeeded_1h"] == 1
    assert (
        items["oliveyoung"]["state"] == "paused" and items["oliveyoung"]["interval_seconds"] == 80
    )
    # 분류 전 이력의 429 문구는 차단으로 다시 읽고, 원본 오류는 내보내지 않는다.
    assert items["musinsa"]["state"] == "probing" and items["musinsa"]["pending"] == 1
    assert items["musinsa"]["failure_kinds_24h"] == [{"kind": "blocked", "count": 1}]
    assert items["coupang"]["state"] == "excluded" and items["coupang"]["label"] == "쿠팡"
    assert "429" not in str(items)


@pytest.fixture
def dashboard_settings(monkeypatch):
    settings = Settings(
        _env_file=None, database_url=get_settings().database_url, internal_token="data-only-secret"
    )
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    monkeypatch.setattr(status, "get_settings", lambda: settings)
    return settings


def test_dashboard_endpoints_require_login_and_validate_range(engine, dashboard_settings):
    with TestClient(module.app) as client:
        assert client.get("/status/api/timeseries").status_code == 401
        client.post("/status/login", json={"token": "data-only-secret"})
        series = client.get("/status/api/timeseries?range=24h")
        assert series.status_code == 200 and series.json()["bucket_seconds"] == 3600
        assert client.get("/status/api/timeseries?range=2h").status_code == 422
        platforms = client.get("/status/api/platforms")
        assert platforms.status_code == 200 and isinstance(platforms.json()["items"], list)
