"""분석 대기열의 남은 리뷰 수와 처리 속도.

작업 하나의 리뷰 수가 1건에서 500건까지 달라 작업 수로는 속도와 남은 시간을 판단할 수 없다.
그래서 작업에 기록된 입력 리뷰 수(input_review_count)를 기준으로 센다.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import text

WINDOW_MINUTES = 60


def estimate(snapshot: dict, window_minutes: float = WINDOW_MINUTES) -> dict:
    """대기 리뷰 수와 최근 처리·유입 리뷰 수로 남은 시간을 추정한다.

    - idle: 남은 작업이 없다.
    - stalled: 남은 작업이 있는데 최근 완료가 없다.
    - growing: 유입이 처리보다 많거나 같아 줄지 않는다. 남은 시간은 정하지 않는다.
    - estimated: 순감소 속도로 나눈 남은 시간(초).
    """
    backlog = int(snapshot["backlog_reviews"])
    done = int(snapshot["done_reviews"])
    inflow = int(snapshot["new_reviews"])
    per_minute = done / window_minutes
    net_per_minute = (done - inflow) / window_minutes
    result = {
        "reviews_per_minute": round(per_minute, 1),
        "net_reviews_per_minute": round(net_per_minute, 1),
        "estimated_remaining_seconds": None,
    }
    if backlog <= 0 and int(snapshot["backlog_jobs"]) <= 0:
        return {**result, "state": "idle", "estimated_remaining_seconds": 0}
    if done <= 0:
        return {**result, "state": "stalled"}
    if net_per_minute <= 0:
        return {**result, "state": "growing"}
    seconds = round(backlog / net_per_minute * 60)
    return {**result, "state": "estimated", "estimated_remaining_seconds": seconds}


_SNAPSHOT = """
SELECT count(*) FILTER (WHERE status IN ('queued', 'running')) AS backlog_jobs,
       coalesce(sum(input_review_count) FILTER (WHERE status IN ('queued', 'running')), 0)
         AS backlog_reviews,
       count(*) FILTER (WHERE status = 'running') AS running_jobs,
       count(*) FILTER (WHERE status = 'done' AND completed_at >= :since) AS done_jobs,
       coalesce(sum(input_review_count) FILTER (
         WHERE status = 'done' AND completed_at >= :since), 0) AS done_reviews,
       -- 같은 기간에 생겼다가 새 입력으로 대체된(stale) 작업은 처리할 일이 아니라 뺀다.
       count(*) FILTER (WHERE created_at >= :since AND status <> 'stale') AS new_jobs,
       coalesce(sum(input_review_count) FILTER (
         WHERE created_at >= :since AND status <> 'stale'), 0) AS new_reviews,
       min(created_at) FILTER (WHERE status = 'queued') AS oldest_queued_at
FROM analysis_jobs
WHERE status IN ('queued', 'running') OR completed_at >= :since OR created_at >= :since
"""


async def backlog_status(session, now=None) -> dict:
    """대기열 집계와 남은 시간 추정. input_payload는 읽지 않는다."""
    now = now or datetime.now(UTC)
    since = now - timedelta(minutes=WINDOW_MINUTES)
    row = dict((await session.execute(text(_SNAPSHOT), {"since": since})).mappings().one())
    oldest = row.pop("oldest_queued_at")
    row = {key: int(value) for key, value in row.items()}
    return {
        **row,
        "window_minutes": WINDOW_MINUTES,
        "oldest_queued_at": oldest.isoformat() if oldest else None,
        "oldest_queued_seconds": round((now - oldest).total_seconds()) if oldest else None,
        **estimate(row),
    }
