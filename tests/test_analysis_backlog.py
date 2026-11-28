"""분석 대기열 남은 시간 추정: 리뷰 수 기준 처리량과 순감소율."""

from review_data.core.service.analysis_backlog import estimate


def snap(backlog_jobs=10, backlog_reviews=6000, done_reviews=1200, new_reviews=0):
    return {
        "backlog_jobs": backlog_jobs,
        "backlog_reviews": backlog_reviews,
        "done_reviews": done_reviews,
        "new_reviews": new_reviews,
    }


def test_estimates_by_net_review_rate():
    # 60분에 1,200건 처리, 유입 600건 → 순감소 10건/분, 남은 6,000건 → 600분
    result = estimate(snap(new_reviews=600))
    assert result["state"] == "estimated"
    assert result["reviews_per_minute"] == 20.0 and result["net_reviews_per_minute"] == 10.0
    assert result["estimated_remaining_seconds"] == 600 * 60


def test_growing_backlog_has_no_eta():
    result = estimate(snap(new_reviews=1500))
    assert result["state"] == "growing" and result["estimated_remaining_seconds"] is None


def test_stalled_when_nothing_finished():
    result = estimate(snap(done_reviews=0))
    assert result["state"] == "stalled" and result["estimated_remaining_seconds"] is None


def test_idle_when_queue_is_empty():
    result = estimate(snap(backlog_jobs=0, backlog_reviews=0, done_reviews=0))
    assert result["state"] == "idle" and result["estimated_remaining_seconds"] == 0


def test_jobs_without_review_counts_are_not_idle():
    # 옛 작업은 리뷰 수 기록이 없을 수 있다. 작업이 남아 있으면 비어 있다고 보지 않는다.
    assert estimate(snap(backlog_jobs=3, backlog_reviews=0, done_reviews=0))["state"] == "stalled"


async def test_backlog_status_counts_reviews_not_jobs(session_factory):
    from datetime import UTC, datetime, timedelta

    from review_data.core.db.models import AnalysisJob, ProductRow
    from review_data.core.service.analysis_backlog import backlog_status

    now = datetime.now(UTC)
    old = now - timedelta(hours=3)
    async with session_factory() as s:
        for i in range(5):
            s.add(ProductRow(platform="kurly", product_id=f"p{i}", name="상품", url="https://x"))
        await s.flush()
        s.add_all(
            [
                # 남은 일: 대기 500건 + 처리 중 100건
                AnalysisJob(
                    platform="kurly",
                    product_id="p0",
                    status="queued",
                    input_review_count=500,
                    created_at=old,
                ),
                AnalysisJob(
                    platform="kurly",
                    product_id="p1",
                    status="running",
                    input_review_count=100,
                    created_at=old,
                ),
                # 최근 60분 완료 1,200건(작업 2개), 그중 하나는 이번 시간에 생긴 유입
                AnalysisJob(
                    platform="kurly",
                    product_id="p2",
                    status="done",
                    input_review_count=1000,
                    created_at=old,
                    completed_at=now - timedelta(minutes=10),
                ),
                AnalysisJob(
                    platform="kurly",
                    product_id="p3",
                    status="done",
                    input_review_count=200,
                    created_at=now - timedelta(minutes=30),
                    completed_at=now - timedelta(minutes=5),
                ),
                # 대체된 작업과 오래전 완료는 세지 않는다.
                AnalysisJob(
                    platform="kurly",
                    product_id="p4",
                    status="stale",
                    input_review_count=300,
                    created_at=now - timedelta(minutes=20),
                ),
                AnalysisJob(
                    platform="kurly",
                    product_id="p4",
                    status="done",
                    input_review_count=900,
                    created_at=old,
                    completed_at=now - timedelta(hours=2),
                ),
            ]
        )
        await s.commit()
    async with session_factory() as s:
        status = await backlog_status(s, now=now)
    assert status["backlog_jobs"] == 2 and status["backlog_reviews"] == 600
    assert status["running_jobs"] == 1
    assert status["done_jobs"] == 2 and status["done_reviews"] == 1200
    assert status["new_jobs"] == 1 and status["new_reviews"] == 200
    assert status["oldest_queued_seconds"] == 3 * 3600
    assert status["state"] == "estimated" and status["reviews_per_minute"] == 20.0
    # 순감소 (1200-200)/60 분당 16.7건 → 600건에 36분
    assert status["estimated_remaining_seconds"] == round(600 / (1000 / 60) * 60)
