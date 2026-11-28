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
