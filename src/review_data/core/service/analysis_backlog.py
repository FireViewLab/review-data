"""분석 대기열의 남은 리뷰 수와 처리 속도.

작업 하나의 리뷰 수가 1건에서 500건까지 달라 작업 수로는 속도와 남은 시간을 판단할 수 없다.
그래서 작업에 기록된 입력 리뷰 수(input_review_count)를 기준으로 센다.
"""

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
