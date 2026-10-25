"""운영 화면의 처리량 시계열과 플랫폼별 수집 상태. 본문·작성자·원본 오류는 내보내지 않는다."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from review_data.core.collection_failure import failure_kind
from review_data.core.db.repository import CollectionJobRepository
from review_data.core.settings import Settings

# 화면 기간별 버킷 크기. 버킷 수를 70개 안팎으로 맞춰 그래프가 촘촘하지도 비지도 않게 한다.
RANGES = {
    "1h": (timedelta(hours=1), timedelta(minutes=5)),
    "6h": (timedelta(hours=6), timedelta(minutes=15)),
    "24h": (timedelta(hours=24), timedelta(hours=1)),
    "7d": (timedelta(days=7), timedelta(hours=6)),
}
_ORIGIN = datetime(2000, 1, 1, tzinfo=UTC)
# 끝난 구간의 신규 리뷰 수. (버킷 크기, 시작 시각) → 건수. 프로세스 수명 동안만 둔다.
_NEW_REVIEWS: dict[tuple[timedelta, datetime], int] = {}


def _bucket(at: datetime, step: timedelta) -> datetime:
    return _ORIGIN + ((at - _ORIGIN) // step) * step


async def timeseries(session, range_key: str, now: datetime | None = None) -> dict:
    span, step = RANGES[range_key]
    now = now or datetime.now(UTC)
    start = _bucket(now - span, step) + step
    buckets: dict[datetime, dict] = {}
    at = start
    while at <= now:
        buckets[at] = {
            "bucket": at.isoformat(),
            "new_reviews": 0,
            "analyzed_reviews": 0,
            "collection_succeeded": 0,
            "collection_failed": 0,
            "analysis_done": 0,
            "analysis_failed": 0,
            "per_platform": {},
        }
        at += step
    params = {"start": start, "step": step, "origin": _ORIGIN}
    # 신규 리뷰는 최초 수집 시각 인덱스로 읽지만, 대량 유입이 있던 주에는 7일 범위가 수십 초
    # 걸린다. 끝난 구간의 값은 바뀌지 않으므로 한 번 센 구간은 기억해 두고 나머지만 센다.
    current = _bucket(now, step)
    known = {at: _NEW_REVIEWS[(step, at)] for at in buckets if (step, at) in _NEW_REVIEWS}
    missing = [at for at in buckets if at not in known]
    if missing:
        rows = await session.execute(
            text("""
            SELECT date_bin(:step, first_collected_at, :origin) AS b, count(*) FROM reviews
            WHERE first_collected_at >= :from GROUP BY 1
        """),
            {**params, "from": min(missing)},
        )
        counted = dict(rows.all())
        for at in missing:
            known[at] = counted.get(at, 0)
            if at < current:
                _NEW_REVIEWS[(step, at)] = known[at]
    for at, count in known.items():
        buckets[at]["new_reviews"] = count
    for bucket, status, jobs, reviews in await session.execute(
        text("""
        SELECT date_bin(:step, completed_at, :origin) AS b, status, count(*),
            coalesce(sum(input_review_count), 0)
        FROM analysis_jobs WHERE completed_at >= :start AND status IN ('done','failed')
        GROUP BY 1, 2
    """),
        params,
    ):
        if bucket not in buckets:
            continue
        if status == "done":
            buckets[bucket]["analysis_done"] = jobs
            buckets[bucket]["analyzed_reviews"] = int(reviews)
        else:
            buckets[bucket]["analysis_failed"] = jobs
    platforms = set()
    for bucket, platform, succeeded, failed in await session.execute(
        text("""
        SELECT date_bin(:step, completed_at, :origin) AS b, platform,
            count(*) FILTER (WHERE status='succeeded'),
            count(*) FILTER (WHERE status IN ('partial','failed'))
        FROM collection_jobs WHERE completed_at >= :start GROUP BY 1, 2
    """),
        params,
    ):
        if bucket not in buckets:
            continue
        platforms.add(platform)
        row = buckets[bucket]
        row["collection_succeeded"] += succeeded
        row["collection_failed"] += failed
        row["per_platform"][platform] = {"succeeded": succeeded, "failed": failed}
    return {
        "range": range_key,
        "bucket_seconds": int(step.total_seconds()),
        "generated_at": now.isoformat(),
        "buckets": list(buckets.values()),
        "platforms": sorted(platforms),
    }


async def platform_status(session, settings: Settings, labels: dict[str, str]) -> dict:
    now = datetime.now(UTC)
    repo = CollectionJobRepository(session)
    paused, probing = await repo.blocked_platforms(settings.schedule_platform_block_seconds, now)
    excluded = set(settings.excluded_platforms())
    intervals = settings.platform_intervals()
    rows = {
        row.platform: row
        for row in (
            await session.execute(
                text("""
            SELECT platform,
                count(*) FILTER (WHERE status='pending') AS pending,
                count(*) FILTER (WHERE status='running') AS running,
                max(completed_at) FILTER (WHERE status='succeeded') AS last_success_at,
                max(completed_at) FILTER (WHERE status IN ('partial','failed')) AS last_failure_at,
                count(*) FILTER (WHERE status='succeeded' AND completed_at > :hour)
                    AS succeeded_1h,
                count(*) FILTER (WHERE status IN ('partial','failed') AND completed_at > :hour)
                    AS failed_1h
            FROM collection_jobs GROUP BY platform
        """),
                {"hour": now - timedelta(hours=1)},
            )
        ).all()
    }
    kinds: dict[str, dict[str, int]] = {}
    for platform, kind, error, count in await session.execute(
        text("""
        SELECT platform, failure_kind, left(last_error, 200), count(*) FROM collection_jobs
        WHERE status IN ('partial','failed') AND completed_at > :day GROUP BY 1, 2, 3
    """),
        {"day": now - timedelta(hours=24)},
    ):
        # 원본 오류는 내보내지 않고 분류 결과만 쓴다. 분류가 없던 이전 이력은 문구로 다시 분류한다.
        key = kind if kind and kind != "unknown" else failure_kind(error or "")
        kinds.setdefault(platform, {})
        kinds[platform][key] = kinds[platform].get(key, 0) + count
    names = sorted(set(rows) | set(labels) - {"_template"})
    items = []
    for platform in names:
        row = rows.get(platform)
        if row is None and platform not in labels:
            continue
        if platform in excluded:
            state = "excluded"
        elif platform in paused:
            state = "paused"
        elif platform in probing:
            state = "probing"
        else:
            state = "normal"
        items.append(
            {
                "platform": platform,
                "label": labels.get(platform, platform),
                "state": state,
                "interval_seconds": intervals.get(platform, 0),
                "pending": row.pending if row else 0,
                "running": row.running if row else 0,
                "last_success_at": row.last_success_at.isoformat()
                if row and row.last_success_at
                else None,
                "last_failure_at": row.last_failure_at.isoformat()
                if row and row.last_failure_at
                else None,
                "succeeded_1h": row.succeeded_1h if row else 0,
                "failed_1h": row.failed_1h if row else 0,
                "failure_kinds_24h": [
                    {"kind": kind, "count": count}
                    for kind, count in sorted(
                        kinds.get(platform, {}).items(), key=lambda item: -item[1]
                    )
                ],
            }
        )
    return {"generated_at": now.isoformat(), "items": items}
