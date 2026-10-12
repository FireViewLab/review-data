"""인증된 운영 대시보드. 명시한 필드만 공개한다."""

import asyncio
import hashlib
import hmac
import logging
import secrets
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text

from review_data.api.v1 import SessionDep
from review_data.core.db.analysis_control import control_status, effective_analysis_settings
from review_data.core.db.models import AnalysisRefreshControl
from review_data.core.service.analysis_refresh import (
    AnalysisRefreshService,
    campaign_status,
    collection_paused,
)
from review_data.core.service.operations import (
    collection_metrics,
    discovery_status,
    operational_status,
    queue_status,
)
from review_data.core.service.scheduling import _SCHEDULER_LOCK_KEY
from review_data.core.settings import get_settings

router = APIRouter(prefix="/status", include_in_schema=False)
logger = logging.getLogger(__name__)
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
    token: str | None = Field(default=None, max_length=512)
    email: str | None = Field(default=None, max_length=320)
    password: str | None = Field(default=None, max_length=512)


# Spring 로그인 대행이 비밀번호 대입 통로가 되지 않도록 실패 횟수를 제한한다.
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_FAILURES_PER_EMAIL = 5
LOGIN_FAILURES_TOTAL = 30
_login_failures: dict[str, list[float]] = {}


def _recent_failures(key: str, now: float) -> list[float]:
    recent = [t for t in _login_failures.get(key, []) if now - t < LOGIN_WINDOW_SECONDS]
    if recent:
        _login_failures[key] = recent
    else:
        _login_failures.pop(key, None)
    return recent


async def spring_admin_login(url: str, email: str, password: str) -> None:
    """Spring 로그인으로 ADMIN 계정을 확인한다. 응답의 토큰은 보관하지 않는다."""
    now = time.monotonic()
    email_key = hashlib.sha256(email.strip().lower().encode()).hexdigest()
    if (
        len(_recent_failures(email_key, now)) >= LOGIN_FAILURES_PER_EMAIL
        or len(_recent_failures("*", now)) >= LOGIN_FAILURES_TOTAL
    ):
        raise HTTPException(429, "로그인 시도가 많습니다. 잠시 후 다시 시도해 주세요.")
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            response = await client.post(url, json={"email": email, "password": password})
    except httpx.HTTPError as exc:
        raise HTTPException(
            503, "Spring 인증 서버에 연결하지 못했습니다. 내부 토큰으로 로그인해 주세요."
        ) from exc
    if response.status_code >= 500 or response.status_code == 429:
        raise HTTPException(
            503, "Spring 인증 서버가 응답하지 않습니다. 내부 토큰으로 로그인해 주세요."
        )
    role = None
    if response.status_code == 200:
        try:
            role = response.json()["data"]["role"]
        except (ValueError, KeyError, TypeError):
            role = None
    if role != "ADMIN":
        # 자격 증명 오류와 권한 부족을 구분해 알려주지 않는다.
        for key in (email_key, "*"):
            _login_failures.setdefault(key, []).append(now)
        raise HTTPException(401, "이메일·비밀번호 또는 관리자 권한을 확인해 주세요.")
    _login_failures.pop(email_key, None)


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
    request.app.state.status_cache = request.app.state.status_live_cache = None
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
    request.app.state.status_cache = request.app.state.status_live_cache = None
    return {"control": await control_status(session, settings)}


@router.post("/api/analysis-refresh/retry-failed")
async def retry_refresh(request: Request, session: SessionDep):
    validate_control_request(request)
    settings = await effective_analysis_settings(session, get_settings())
    if not settings.analysis_refresh_enabled or not settings.ai_analysis_enabled:
        raise HTTPException(409, "목표 버전으로 시작·재개한 뒤 실패 건을 재시도해 주세요.")
    result = await AnalysisRefreshService(session, settings).run_once(retry_failed=True)
    await session.commit()
    request.app.state.status_cache = request.app.state.status_live_cache = None
    return result


@router.get("", response_class=HTMLResponse)
async def page():
    html = (Path(__file__).parent / "status.html").read_text()
    if get_settings().spring_auth_login_url:
        html = html.replace('data-spring-login="0"', 'data-spring-login="1"', 1)
    return HTMLResponse(
        html,
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
    settings = get_settings()
    token = settings.internal_token
    if token and body.token is None:
        if not settings.spring_auth_login_url:
            raise HTTPException(503, "계정 로그인이 설정되지 않았습니다. 내부 토큰을 써 주세요.")
        if not body.email or not body.password:
            raise HTTPException(401, "이메일과 비밀번호를 입력해 주세요.")
        await spring_admin_login(settings.spring_auth_login_url, body.email, body.password)
    elif token and not secrets.compare_digest(body.token.encode(), token.encode()):
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


OVERVIEW_TTL_SECONDS = 60
LIVE_TTL_SECONDS = 2


async def build_live(app, session) -> dict:
    """대기열·워커·캠페인처럼 조회가 빠른 운영 상태. 리뷰 전체를 훑는 집계는 넣지 않는다."""
    settings = await effective_analysis_settings(session, get_settings())
    result = {
        "checked_at": datetime.now(UTC).isoformat(),
        "queues": await queue_status(session),
        "discovery": await discovery_status(session),
    }
    result["resources"] = app.state.resource_metrics
    result["rates_15m"] = (
        await rows(
            session,
            """
        SELECT
            (SELECT count(*) FROM collection_jobs WHERE completed_at>=now()-interval '15 min'
              AND review_status='succeeded') AS collected_products,
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
        "discovery_max_analysis_pending": settings.discovery_max_analysis_pending,
        "schedule_max_pending": settings.schedule_max_pending,
        "collection_paused": await collection_paused(session, settings),
    }
    return result


async def build_overview(app, session) -> dict:
    settings = await effective_analysis_settings(session, get_settings())
    result = await operational_status(session)
    metrics = await collection_metrics(session)
    result["platforms"] = [
        {key: row[key] for key in ("platform", "products", "reviews")} for row in metrics
    ]
    result["totals"] = (
        await rows(
            session,
            """
        SELECT (SELECT count(*) FROM products) AS products,
               (SELECT count(*) FROM review_analyses a JOIN (
                   SELECT DISTINCT ON (platform,product_id) id FROM analysis_jobs
                   WHERE model_version=:model AND policy_version=:policy
                     AND (status='done' OR (status='stale' AND completed_at IS NOT NULL
                          AND result IS NOT NULL))
                   ORDER BY platform,product_id,completed_at DESC NULLS LAST,id DESC
                ) latest ON latest.id=a.analysis_job_id) AS analysis_results,
               (SELECT count(*) FROM products p WHERE NOT EXISTS
                  (SELECT 1 FROM reviews r WHERE r.platform=p.platform
                   AND r.product_id=p.product_id)) AS empty_products
    """,
            {"model": settings.ai_model_version, "policy": settings.ai_policy_version},
        )
    )[0]
    result["totals"]["reviews"] = sum(row["reviews"] for row in metrics)
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
    # 빠른 영역은 같은 시점 값으로 맞춰 함께 싣는다. 화면은 /api/live 값으로 계속 덮어쓴다.
    live = await build_live(app, session)
    live["rates_15m"]["new_reviews"] = sum(row["new_reviews"] for row in metrics)
    live["rates_15m"]["refreshed_reviews"] = sum(row["refreshed_reviews"] for row in metrics)
    live.pop("checked_at")
    result.update(live)
    return result


async def _refresh_overview(app, replaced) -> None:
    """화면 응답과 분리해 집계를 갱신한다. 그사이 제어 요청이 캐시를 비웠으면 덮어쓰지 않는다."""
    started = time.monotonic()
    try:
        async with app.state.session_factory() as session:
            result = await build_overview(app, session)
    except Exception:  # noqa: BLE001 - 다음 요청에서 다시 시도한다
        logger.warning("운영 개요 갱신 실패: 직전 집계를 유지한다.")
        return
    if app.state.status_cache is replaced:
        now = time.monotonic()
        app.state.status_cache = now, result, now - started


@router.get("/api/live")
async def live(request: Request, session: SessionDep):
    # 여러 브라우저가 열려 있어도 2초에 한 번만 조회한다.
    cached = getattr(request.app.state, "status_live_cache", None)
    if cached and time.monotonic() - cached[0] < LIVE_TTL_SECONDS:
        return cached[1]
    result = await build_live(request.app, session)
    request.app.state.status_live_cache = time.monotonic(), result
    return result


@router.get("/api/overview")
async def overview(request: Request, session: SessionDep, refresh: bool = False):
    # 리뷰 전체를 훑는 합계는 수 초가 걸린다. 직전 결과를 바로 응답하고 갱신은 백그라운드에서
    # 한 번만 돈다. refresh=true는 주기를 기다리지 않고 갱신을 시작한다.
    app = request.app
    if not hasattr(app.state, "status_cache_lock"):
        app.state.status_cache_lock = asyncio.Lock()
    cached = getattr(app.state, "status_cache", None)
    if cached is None:
        async with app.state.status_cache_lock:
            cached = getattr(app.state, "status_cache", None)
            if cached is None:
                started = time.monotonic()
                result = await build_overview(app, session)
                now = time.monotonic()
                cached = app.state.status_cache = now, result, now - started
        return {**cached[1], "refreshing": False}
    task = getattr(app.state, "status_refresh_task", None)
    refreshing = task is not None and not task.done()
    # 집계가 느린 동안에는 갱신 간격도 늘려 DB를 계속 붙잡지 않는다.
    stale_after = max(OVERVIEW_TTL_SECONDS, 2 * cached[2])
    if not refreshing and (refresh or time.monotonic() - cached[0] >= stale_after):
        app.state.status_refresh_task = asyncio.create_task(_refresh_overview(app, cached))
        refreshing = True
    return {**cached[1], "refreshing": refreshing}


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
