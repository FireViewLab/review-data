"""인증된 운영 대시보드. 명시한 필드만 공개한다."""

import asyncio
import hashlib
import hmac
import secrets
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text

from review_data.api.v1 import SessionDep
from review_data.core.db.analysis_control import control_status, effective_analysis_settings
from review_data.core.db.models import AnalysisRefreshControl
from review_data.core.service.analysis_refresh import AnalysisRefreshService, campaign_status
from review_data.core.service.operations import operational_status
from review_data.core.service.scheduling import _SCHEDULER_LOCK_KEY
from review_data.core.settings import get_settings

router = APIRouter(prefix="/status", include_in_schema=False)
COOKIE = "review_status_session"
SESSION_SECONDS = 8 * 3600


def session_value(token: str, expires: int) -> str:
    signature = hmac.new(token.encode(), f"status:{expires}".encode(), hashlib.sha256).hexdigest()
    return f"{expires}.{signature}"


def valid_session(request: Request, token: str) -> bool:
    value = request.cookies.get(COOKIE, "")
    try:
        expires = int(value.split(".")[0])
        return (
            time.time() < expires <= time.time() + SESSION_SECONDS + 60
            and secrets.compare_digest(value, session_value(token, expires))
        )
    except (ValueError, IndexError):
        return False


class Login(BaseModel):
    token: str


class RefreshTarget(BaseModel):
    model_version: str = Field(
        min_length=1, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/+@-]*$"
    )
    policy_version: str = Field(
        min_length=1, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/+@-]*$"
    )


def validate_control_request(request: Request):
    if not get_settings().internal_token:
        raise HTTPException(503, "운영 제어에는 Data 내부 토큰 설정이 필요합니다.")
    origin = request.headers.get("origin")
    if origin and origin != str(request.base_url).rstrip("/"):
        raise HTTPException(403, "같은 서버의 대시보드에서 실행해 주세요.")


@router.post("/api/analysis-refresh/start")
async def start_refresh(body: RefreshTarget, request: Request, session: SessionDep):
    validate_control_request(request)
    settings = get_settings()
    if not settings.ai_analysis_enabled:
        raise HTTPException(409, "AI 분석 연결을 먼저 활성화해 주세요.")
    await session.execute(select(func.pg_advisory_xact_lock(_SCHEDULER_LOCK_KEY)))
    row = await session.get(AnalysisRefreshControl, 1)
    if row is None:
        row = AnalysisRefreshControl(id=1)
        session.add(row)
    row.model_version, row.policy_version = body.model_version, body.policy_version
    row.enabled, row.updated_at = True, datetime.now(UTC)
    await session.flush()
    result = await AnalysisRefreshService(session, settings).run_once(reactivate=True)
    await session.commit()
    request.app.state.status_cache = None
    return {"control": await control_status(session, settings), **result}


@router.post("/api/analysis-refresh/pause")
async def pause_refresh(request: Request, session: SessionDep):
    validate_control_request(request)
    await session.execute(select(func.pg_advisory_xact_lock(_SCHEDULER_LOCK_KEY)))
    settings = await effective_analysis_settings(session, get_settings())
    row = await session.get(AnalysisRefreshControl, 1)
    if row is None:
        if not settings.ai_model_version or not settings.ai_policy_version:
            raise HTTPException(409, "먼저 목표 버전으로 재분석을 시작해 주세요.")
        row = AnalysisRefreshControl(
            id=1, model_version=settings.ai_model_version, policy_version=settings.ai_policy_version
        )
        session.add(row)
    row.enabled, row.updated_at = False, datetime.now(UTC)
    await session.commit()
    request.app.state.status_cache = None
    return {"control": await control_status(session, settings)}


@router.post("/api/analysis-refresh/retry-failed")
async def retry_refresh(request: Request, session: SessionDep):
    validate_control_request(request)
    settings = await effective_analysis_settings(session, get_settings())
    if not settings.analysis_refresh_enabled or not settings.ai_analysis_enabled:
        raise HTTPException(409, "목표 버전으로 시작·재개한 뒤 실패 건을 재시도해 주세요.")
    result = await AnalysisRefreshService(session, settings).run_once(retry_failed=True)
    await session.commit()
    request.app.state.status_cache = None
    return result


@router.get("", response_class=HTMLResponse)
async def page():
    return HTMLResponse(
        (Path(__file__).parent / "status.html").read_text(),
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        },
    )


@router.post("/login")
async def login(body: Login, request: Request):
    token = get_settings().internal_token
    if token and not secrets.compare_digest(body.token.encode(), token.encode()):
        raise HTTPException(401, "Data 서버 내부 토큰을 확인해 주세요.")
    response = JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})
    if token:
        response.set_cookie(
            COOKIE,
            session_value(token, int(time.time()) + SESSION_SECONDS),
            max_age=SESSION_SECONDS,
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
            path="/status",
        )
    return response


@router.post("/logout")
async def logout():
    response = JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})
    response.delete_cookie(COOKIE, path="/status")
    return response


async def rows(session, sql, params=None):
    return [dict(row) for row in (await session.execute(text(sql), params or {})).mappings()]


@router.get("/api/overview")
async def overview(request: Request, session: SessionDep):
    # 브라우저 수와 무관하게 10초에 한 번 집계한다.
    if not hasattr(request.app.state, "status_cache_lock"):
        request.app.state.status_cache_lock = asyncio.Lock()
    async with request.app.state.status_cache_lock:
        cached = getattr(request.app.state, "status_cache", None)
        if cached and time.monotonic() - cached[0] < 10:
            return cached[1]
        result = await operational_status(session)
        result["resources"] = request.app.state.resource_metrics
        result["totals"] = (
            await rows(
                session,
                """
            SELECT (SELECT count(*) FROM products) AS products,
                   (SELECT count(*) FROM reviews) AS reviews,
                   (SELECT count(*) FROM review_analyses) AS analysis_results,
                   (SELECT count(*) FROM products p WHERE NOT EXISTS
                      (SELECT 1 FROM reviews r WHERE r.platform=p.platform
                       AND r.product_id=p.product_id)) AS empty_products
        """,
            )
        )[0]
        result["rates_15m"] = (
            await rows(
                session,
                """
            SELECT
                (SELECT count(*) FROM collection_jobs WHERE completed_at>=now()-interval '15 min'
                  AND review_status='succeeded') AS collected_products,
                (SELECT count(*) FROM reviews WHERE first_collected_at>=now()-interval '15 min')
                  AS new_reviews,
                (SELECT count(*) FROM reviews WHERE last_collected_at>=now()-interval '15 min')
                  AS refreshed_reviews,
                (SELECT coalesce(sum(input_review_count),0) FROM analysis_jobs
                  WHERE locked_at>=now()-interval '15 min' AND attempt_count>0)
                  AS ai_claimed_reviews,
                (SELECT coalesce(sum(input_review_count),0) FROM analysis_jobs
                  WHERE completed_at>=now()-interval '15 min' AND status='done')
                  AS ai_completed_reviews
        """,
            )
        )[0]
        result["workers"] = await rows(
            session,
            """
            SELECT w.worker_id,w.role,w.last_seen_at,w.stopped_at,
                (w.stopped_at IS NULL AND w.last_seen_at>now()-interval '35 seconds') AS alive,
                (SELECT json_agg(json_build_object('id',j.id,'platform',j.platform,
                         'product_id',j.product_id,'lease_expires_at',j.lease_expires_at))
                 FROM collection_jobs j WHERE j.locked_by=w.worker_id AND j.status='running')
                  AS collection_jobs,
                (SELECT json_agg(json_build_object('id',j.id,'platform',j.platform,
                         'product_id',j.product_id,'input_review_count',j.input_review_count,
                         'lease_expires_at',j.lease_expires_at))
                 FROM analysis_jobs j WHERE j.locked_by=w.worker_id AND j.status='running')
                  AS analysis_jobs
            FROM worker_heartbeats w WHERE w.stopped_at IS NULL
                AND w.last_seen_at>now()-interval '5 minutes'
            ORDER BY w.role,w.last_seen_at DESC
        """,
        )
        result["platforms"] = await rows(
            session,
            """
            SELECT p.platform,count(*) AS products,
              (SELECT count(*) FROM reviews r WHERE r.platform=p.platform) AS reviews
            FROM products p GROUP BY p.platform ORDER BY p.platform
        """,
        )
        result["timeline"] = await rows(
            session,
            """
            WITH buckets AS (
              SELECT generate_series(date_bin(interval '5 min',now()-interval '55 min',
                         timestamp '2000-01-01'),date_bin(interval '5 min',now(),
                         timestamp '2000-01-01'),interval '5 min') AS bucket
            ), collected AS (
              SELECT date_bin(interval '5 min',first_collected_at,timestamp '2000-01-01') AS bucket,
                     count(*) AS count FROM reviews
              WHERE first_collected_at>=now()-interval '1 hour'
              GROUP BY 1
            ), analyzed AS (
              SELECT date_bin(interval '5 min',completed_at,timestamp '2000-01-01') AS bucket,
                sum(input_review_count) AS count FROM analysis_jobs WHERE status='done'
                AND completed_at>=now()-interval '1 hour' GROUP BY 1
            ) SELECT b.bucket,coalesce(c.count,0) AS collected,coalesce(a.count,0) AS analyzed
              FROM buckets b LEFT JOIN collected c USING(bucket)
              LEFT JOIN analyzed a USING(bucket) ORDER BY b.bucket
        """,
        )
        settings = await effective_analysis_settings(session, get_settings())
        result["analysis_control"] = await control_status(session, settings)
        result["analysis_campaigns"] = await campaign_status(
            session, enabled=settings.analysis_refresh_enabled
        )
        result["settings"] = {
            "analysis_refresh_enabled": settings.analysis_refresh_enabled,
            "target_model_version": settings.ai_model_version,
            "target_policy_version": settings.ai_policy_version,
            "ai_enabled": settings.ai_analysis_enabled,
            "discovery_enabled": settings.discovery_enabled,
            "discovery_interval_seconds": settings.discovery_interval_seconds,
        }
        request.app.state.status_cache = time.monotonic(), result
        return result


@router.get("/api/jobs/{kind}")
async def jobs(
    kind: Literal["collection", "analysis"],
    session: SessionDep,
    status: str | None = None,
    platform: str | None = None,
    q: Annotated[str, Query(max_length=100)] = "",
    before: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
):
    table = "collection_jobs" if kind == "collection" else "analysis_jobs"
    extra = (
        "j.product_status,j.review_status"
        if kind == "collection"
        else ("j.input_review_count,j.model_version,j.policy_version,j.result")
    )
    items = await rows(
        session,
        f"""
        SELECT j.id,j.platform,j.product_id,p.name,j.status,j.attempt_count,j.max_attempts,
            j.locked_at,j.lease_expires_at,j.created_at,j.completed_at,
            j.last_error IS NOT NULL AS has_error,{extra}
        FROM {table} j LEFT JOIN products p USING(platform,product_id)
        WHERE (CAST(:status AS text) IS NULL OR j.status=:status)
          AND (CAST(:platform AS text) IS NULL OR j.platform=:platform)
          AND (CAST(:before AS bigint) IS NULL OR j.id<:before)
          AND (:q='' OR j.product_id ILIKE :pattern OR p.name ILIKE :pattern)
        ORDER BY j.id DESC LIMIT :limit
    """,
        {
            "status": status,
            "platform": platform,
            "before": before,
            "q": q,
            "pattern": f"%{q}%",
            "limit": limit + 1,
        },
    )
    return {
        "items": items[:limit],
        "next_cursor": items[limit - 1]["id"] if len(items) > limit else None,
    }


@router.get("/api/jobs/{kind}/{job_id}")
async def job_detail(
    kind: Literal["collection", "analysis"],
    job_id: int,
    session: SessionDep,
    offset: Annotated[int, Query(ge=0, le=100000)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
):
    table = "collection_jobs" if kind == "collection" else "analysis_jobs"
    extra = (
        "j.product_status,j.review_status"
        if kind == "collection"
        else ("j.input_payload,j.input_review_count,j.model_version,j.policy_version,j.result")
    )
    data = await rows(
        session,
        f"""
        SELECT j.id,j.platform,j.product_id,p.name,j.status,j.attempt_count,j.max_attempts,
               j.created_at,j.locked_at,j.completed_at,j.lease_expires_at,
               j.last_error IS NOT NULL AS has_error,{extra}
        FROM {table} j LEFT JOIN products p USING(platform,product_id) WHERE j.id=:id
    """,
        {"id": job_id},
    )
    if not data:
        raise HTTPException(404, "작업을 찾을 수 없습니다.")
    job = data[0]
    if kind == "analysis":
        payload = job.pop("input_payload") or {}
        reviews = payload.get("reviews", [])
        requested = [
            {key: r.get(key) for key in ("review_id", "content", "rating", "written_at")}
            for r in reviews[offset : offset + limit]
        ]
        results = await rows(
            session,
            """
            SELECT review_id,rti,level,text_score,behavior_score,network_score,reasons,
                   model_version,created_at FROM review_analyses
            WHERE analysis_job_id=:id AND review_id=ANY(CAST(:ids AS text[]))
        """,
            {"id": job_id, "ids": [r["review_id"] for r in requested]},
        )
        result_by_id = {r["review_id"]: r for r in results}
        return {
            "job": job,
            "sampling": payload.get("sampling"),
            "total": len(reviews),
            "offset": offset,
            "next_offset": offset + limit if offset + limit < len(reviews) else None,
            "request_headers": {"X-Request-ID": str(job_id), "Idempotency-Key": str(job_id)},
            "reviews": [
                {"request": r, "result": result_by_id.get(r["review_id"])} for r in requested
            ],
        }
    return await stored_review_detail(session, job, offset, limit)


async def stored_review_detail(session, job, offset, limit):
    total = (
        await rows(
            session,
            """
        SELECT count(*) AS count FROM reviews WHERE platform=:platform AND product_id=:product_id
    """,
            job,
        )
    )[0]["count"]
    reviews = await rows(
        session,
        """
        SELECT review_id,content,rating,written_at,"option",first_collected_at,last_collected_at
        FROM reviews WHERE platform=:platform AND product_id=:product_id
        ORDER BY first_collected_at DESC,review_id LIMIT :limit OFFSET :offset
    """,
        {**job, "offset": offset, "limit": limit},
    )
    return {
        "job": job,
        "total": total,
        "offset": offset,
        "reviews": reviews,
        "next_offset": offset + limit if offset + limit < total else None,
        "note": "해당 상품의 현재 DB 원본 리뷰입니다. 이 작업이 수집한 건수와 다를 수 있습니다.",
    }


@router.get("/api/products")
async def products(
    session: SessionDep,
    q: Annotated[str, Query(max_length=100)] = "",
    platform: str | None = None,
    offset: Annotated[int, Query(ge=0, le=100000)] = 0,
):
    items = await rows(
        session,
        """
        SELECT p.platform,p.product_id,p.name,p.review_count AS advertised_review_count,
               p.reviews_last_collected_at,
          (SELECT count(*) FROM reviews r WHERE r.platform=p.platform
              AND r.product_id=p.product_id) AS stored_reviews,
          (SELECT json_build_object('id',j.id,'status',j.status,'input_review_count',
              j.input_review_count,'completed_at',j.completed_at) FROM analysis_jobs j
           WHERE j.platform=p.platform AND j.product_id=p.product_id ORDER BY id DESC LIMIT 1)
              AS analysis,
          (SELECT j.id FROM collection_jobs j WHERE j.platform=p.platform
              AND j.product_id=p.product_id ORDER BY id DESC LIMIT 1) AS collection_job_id
        FROM products p WHERE (CAST(:platform AS text) IS NULL OR p.platform=:platform)
          AND (:q='' OR p.name ILIKE :pattern OR p.product_id ILIKE :pattern)
        ORDER BY p.first_collected_at DESC,p.platform,p.product_id LIMIT 31 OFFSET :offset
    """,
        {"platform": platform, "q": q, "pattern": f"%{q}%", "offset": offset},
    )
    return {"items": items[:30], "next_offset": offset + 30 if len(items) > 30 else None}


@router.get("/api/products/{platform}/{product_id}/reviews")
async def product_reviews(
    platform: str,
    product_id: str,
    session: SessionDep,
    offset: Annotated[int, Query(ge=0, le=100000)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
):
    found = await rows(
        session,
        """
        SELECT platform,product_id,name FROM products
        WHERE platform=:platform AND product_id=:product_id
    """,
        {"platform": platform, "product_id": product_id},
    )
    if not found:
        raise HTTPException(404, "상품을 찾을 수 없습니다.")
    return await stored_review_detail(session, found[0], offset, limit)
