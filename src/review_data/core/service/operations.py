"""운영 점검용 집계. 토큰·리뷰 본문·작성자·원본 오류 메시지는 출력하지 않는다."""

from datetime import UTC, datetime

from sqlalchemy import text


async def queue_status(session):
    """수집·분석 대기열 요약. 상태 색인만 훑어 빠르다."""
    queues = {}
    for name, table, pending in [
        ("collection", "collection_jobs", "pending"),
        ("analysis", "analysis_jobs", "queued"),
    ]:
        row = (
            (
                await session.execute(
                    text(f"""
            SELECT count(*) FILTER (WHERE status=:pending) AS waiting,
                   count(*) FILTER (WHERE status='running') AS running,
                   count(*) FILTER (WHERE status='running' AND lease_expires_at<=now())
                       AS expired_leases,
                   count(*) FILTER (WHERE status='running' AND lease_expires_at<=now()
                       AND attempt_count>=max_attempts) AS exhausted_leases,
                   min(created_at) FILTER (WHERE status=:pending) AS oldest_waiting_at
            FROM {table}
        """),
                    {"pending": pending},
                )
            )
            .mappings()
            .one()
        )
        queues[name] = dict(row)
    return queues


async def discovery_status(session):
    return [
        dict(row)
        for row in (
            await session.execute(
                text("""
        SELECT key,position,next_run_at,lease_expires_at,last_completed_at,
               last_saved,last_queued,last_failures FROM discovery_state ORDER BY key
    """)
            )
        ).mappings()
    ]


async def operational_status(session):
    result = {"checked_at": datetime.now(UTC).isoformat(), "queues": await queue_status(session)}
    result["latest_analysis"] = [
        dict(row)
        for row in (
            await session.execute(
                text("""
        SELECT status,count(*) AS count FROM (
            SELECT DISTINCT ON(platform,product_id) status FROM analysis_jobs
            ORDER BY platform,product_id,id DESC
        ) j GROUP BY status ORDER BY status
    """)
            )
        ).mappings()
    ]
    result["empty_products"] = [
        dict(row)
        for row in (
            await session.execute(
                text("""
        WITH latest AS (
            SELECT DISTINCT ON(platform,product_id) platform,product_id,status,review_status
            FROM collection_jobs ORDER BY platform,product_id,id DESC
        )
        SELECT p.platform,
            CASE WHEN j.status IN ('pending','running') THEN j.status
                 WHEN j.review_status='succeeded' AND p.reviews_last_collected_at IS NOT NULL
                     THEN 'collected_empty'
                 WHEN j.review_status='failed' THEN 'collection_failed'
                 ELSE 'not_collected' END AS reason,
            count(*) AS count,
            count(*) FILTER (WHERE p.review_count>0) AS advertised_positive
        FROM products p LEFT JOIN latest j USING(platform,product_id)
        WHERE NOT EXISTS (SELECT 1 FROM reviews r
            WHERE r.platform=p.platform AND r.product_id=p.product_id)
        GROUP BY p.platform,reason ORDER BY p.platform,reason
    """)
            )
        ).mappings()
    ]
    result["latest_collection_failures"] = [
        dict(row)
        for row in (
            await session.execute(
                text("""
        SELECT platform,status,product_status,review_status,count(*) AS count FROM (
            SELECT DISTINCT ON(platform,product_id)
                platform,product_id,status,product_status,review_status
            FROM collection_jobs ORDER BY platform,product_id,id DESC
        ) j WHERE status IN ('partial','failed')
        GROUP BY platform,status,product_status,review_status ORDER BY platform,status
    """)
            )
        ).mappings()
    ]
    result["collection_failure_causes"] = [
        dict(row)
        for row in (
            await session.execute(
                text("""
                SELECT platform,coalesce(failure_kind,'legacy_unknown') AS failure_kind,
                    count(*) AS count,
                    count(*) FILTER (WHERE retry_after>now()) AS cooling_down,
                    min(retry_after) FILTER (WHERE retry_after>now()) AS next_retry_at
                FROM (
                    SELECT DISTINCT ON(platform,product_id)
                        platform,product_id,status,failure_kind,retry_after
                    FROM collection_jobs ORDER BY platform,product_id,id DESC
                ) j WHERE status IN ('partial','failed')
                GROUP BY platform,failure_kind ORDER BY platform,failure_kind
            """)
            )
        ).mappings()
    ]
    result["discovery"] = await discovery_status(session)
    return result


async def collection_metrics(session):
    """리뷰 전체/최근 관측/플랫폼 수를 한 번의 그룹 집계로 계산한다."""
    return [
        dict(row)
        for row in (
            await session.execute(
                text("""
            WITH review_counts AS (
                SELECT platform,count(*) AS reviews,
                    count(*) FILTER (WHERE first_collected_at>=now()-interval '15 minutes')
                        AS new_reviews,
                    count(*) FILTER (WHERE last_collected_at>=now()-interval '15 minutes')
                        AS refreshed_reviews
                FROM reviews GROUP BY platform
            ), product_counts AS (
                SELECT platform,count(*) AS products FROM products GROUP BY platform
            )
            SELECT p.platform,p.products,coalesce(r.reviews,0) AS reviews,
                coalesce(r.new_reviews,0) AS new_reviews,
                coalesce(r.refreshed_reviews,0) AS refreshed_reviews
            FROM product_counts p LEFT JOIN review_counts r USING(platform)
            ORDER BY p.platform
        """)
            )
        ).mappings()
    ]
