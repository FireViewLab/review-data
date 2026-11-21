"""운영 화면용 v4.0 결과 진행 상황."""

from urllib.parse import urlsplit, urlunsplit

import httpx
from sqlalchemy import text

from review_data.core.settings import Settings


def health_url(analyze_url: str) -> str:
    parts = urlsplit(analyze_url)
    return urlunsplit((parts.scheme, parts.netloc, "/health", "", ""))


async def service_health(settings: Settings, client: httpx.AsyncClient | None = None) -> dict:
    """v4 서비스 /health 응답. 주소가 없으면 미설정으로 돌려준다."""
    if not settings.v4_analyze_url:
        return {"state": "disabled"}
    owned = client is None
    client = client or httpx.AsyncClient(timeout=3.0)
    try:
        response = await client.get(health_url(settings.v4_analyze_url))
        if response.status_code != 200:
            return {"state": "error", "status_code": response.status_code}
        body = response.json()
        return {
            "state": "ok",
            "model_version": body.get("model_version"),
            "policy_version": body.get("policy_version"),
        }
    except (httpx.HTTPError, ValueError):
        return {"state": "unreachable"}
    finally:
        if owned:
            await client.aclose()


async def v4_progress(session, settings: Settings, client: httpx.AsyncClient | None = None) -> dict:
    """목표 모델로 완료된 분석 작업 중 v4 결과를 채운 작업 수와 최근 처리량."""
    row = (
        (
            await session.execute(
                text(
                    """
            SELECT count(*) AS target_jobs,
                   count(v4_completed_at) AS filled_jobs,
                   count(*) FILTER (WHERE v4_completed_at >= now() - interval '15 min')
                     AS filled_15m,
                   max(v4_completed_at) AS last_filled_at
            FROM analysis_jobs
            WHERE status = 'done' AND model_version = :model
            """
                ),
                {"model": settings.ai_model_version},
            )
        )
        .mappings()
        .one()
    )
    progress = dict(row)
    if progress["last_filled_at"] is not None:
        progress["last_filled_at"] = progress["last_filled_at"].isoformat()
    progress["target_model_version"] = settings.ai_model_version
    progress["service"] = await service_health(settings, client)
    return progress
