"""
TTL 기반 하이브리드 조회 API.

specs/2026-09-03-api-contract.md 의 계약을 구현한다.
"""

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from review_data.api import docs
from review_data.core.db.analysis_repository import AnalysisRepository, public_job
from review_data.core.db.models import AnalysisJob, CollectionJob, ProductRow, ReviewRow
from review_data.core.db.repository import InvalidCursorError, ReviewRepository
from review_data.core.service.catalog import catalog_page
from review_data.core.service.collection import CollectionResult, CollectionService
from review_data.core.settings import get_settings

router = APIRouter(prefix="/api/v1")


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """앱 수명에 묶인 엔진에서 요청당 세션을 하나 연다(app.py 의 lifespan 참고)."""
    async with request.app.state.session_factory() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]


@router.get("/catalog", tags=[docs.TAG_PRODUCTS], summary="저장 상품·분석 요약 목록")
async def get_catalog(
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    cursor: str | None = None,
) -> dict:
    try:
        items, next_cursor = await catalog_page(session, get_settings(), limit, cursor)
    except ValueError as exc:
        raise _api_error(400, "INVALID_CURSOR", str(exc)) from exc
    return {
        "items": [
            {"product": _serialize_product(product), "analysis": analysis}
            for product, analysis in items
        ],
        "next_cursor": next_cursor,
    }


def _api_error(status_code: int, code: str, message: str, detail: object = None) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"error": {"code": code, "message": message, "detail": detail}},
    )


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _serialize_product(row: ProductRow) -> dict:
    return {
        "platform": row.platform,
        "product_id": row.product_id,
        "name": row.name,
        "url": row.url,
        "brand": row.brand,
        "manufacturer": row.manufacturer,
        "seller": row.seller,
        "price": row.price,
        "thumbnail_url": row.thumbnail_url,
        "category": row.category,
        "review_count": row.review_count,
        "rating": float(row.rating) if row.rating is not None else None,
        "last_collected_at": _iso(row.last_collected_at),
    }


def _serialize_review(row: ReviewRow) -> dict:
    return {
        "review_id": row.review_id,
        "content": row.content,
        "rating": float(row.rating) if row.rating is not None else None,
        "author": row.author,
        "written_at": _iso(row.written_at),
        "option": row.option,
        "images": list(row.images or []),
        "helpful_count": row.helpful_count,
    }


def _serialize_job(job: CollectionJob) -> dict:
    return {
        "id": job.id,
        "platform": job.platform,
        "product_id": job.product_id,
        "status": job.status,
        "product_status": job.product_status,
        "review_status": job.review_status,
        "last_error": job.last_error,
    }


def _build_body(result: CollectionResult) -> dict:
    body: dict = {"status": result.status}
    if result.product is not None:
        body["product"] = _serialize_product(result.product)
    if result.status != "queued":
        body["reviews"] = {
            "items": [_serialize_review(r) for r in result.reviews],
            "next_cursor": result.reviews_next_cursor,
        }
    if result.job is not None:
        body["job"] = _serialize_job(result.job)
    return body


@router.get(
    "/{platform}/products/{product_id}",
    tags=[docs.TAG_PRODUCTS],
    summary="상품·리뷰 조회",
    description="저장된 상품과 리뷰 한 페이지를 돌려준다. 오래됐거나 없으면 수집 job 을 만든다. "
    "응답 `status` 로 fresh / stale / queued 를 구분한다. "
    "analysis.sampled와 source_review_count로 대표 표본 여부와 저장 원본 건수를 확인한다.",
    response_model=None,
    responses=docs.PRODUCT_RESPONSES,
)
async def get_product(
    platform: Annotated[
        str, Path(description="수집기 이름 (GET /platforms 참고)", examples=["kurly"])
    ],
    product_id: Annotated[str, Path(description="쇼핑몰의 상품 ID", examples=["1000146248"])],
    session: SessionDep,
    cursor: Annotated[
        str | None, Query(description="이전 응답의 reviews.next_cursor. 첫 페이지는 비운다")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=100, description="리뷰 페이지 크기")] = 20,
) -> JSONResponse:
    await AnalysisRepository(session).lock_product(platform, product_id, read=True)
    service = CollectionService(session)
    try:
        result = await service.get_or_queue(
            platform, product_id, review_limit=limit, review_cursor=cursor
        )
    except InvalidCursorError as exc:
        raise _api_error(400, "INVALID_CURSOR", "cursor 값이 올바르지 않습니다.", str(exc)) from exc
    status_code = 202 if result.status == "queued" else 200
    body = _build_body(result)
    body["analysis"] = await AnalysisRepository(session).status(
        platform, product_id, get_settings(), [r.review_id for r in result.reviews]
    )
    await session.commit()
    return JSONResponse(status_code=status_code, content=body)


@router.get(
    "/jobs/{job_id}",
    tags=[docs.TAG_JOBS],
    summary="수집 job 상태 조회",
    description="상품 조회 응답의 `job.id` 로 진행 상황을 확인한다. "
    "`succeeded`·`partial` 이 되면 상품 조회를 다시 호출한다.",
    response_model=None,
    responses=docs.JOB_RESPONSES,
)
async def get_job(
    job_id: Annotated[int, Path(description="상품 조회 응답의 job.id")], session: SessionDep
) -> dict:
    job = await session.get(CollectionJob, job_id)

    if job is None:
        raise _api_error(404, "NOT_FOUND", "job을 찾을 수 없습니다.")
    return _serialize_job(job)


@router.get(
    "/{platform}/products/{product_id}/analysis",
    tags=[docs.TAG_PRODUCTS],
    summary="저장된 분석 상태·결과 조회",
    description="review_count는 분석 건수, source_review_count는 저장 원본 건수다. "
    "sampled=true는 별점 분포 대표 표본이며 results에는 현재 페이지의 실제 분석 결과만 포함한다.",
    response_model=None,
)
async def get_analysis(
    platform: str,
    product_id: str,
    session: SessionDep,
    cursor: str | None = None,
    limit: int = Query(20, ge=1, le=100),
) -> dict:
    if await AnalysisRepository(session).lock_product(platform, product_id, read=True) is None:
        raise _api_error(404, "NOT_FOUND", "상품을 찾을 수 없습니다.")
    try:
        reviews, next_cursor = await ReviewRepository(session).list_page(
            platform, product_id, limit=limit, cursor=cursor
        )
    except InvalidCursorError as exc:
        raise _api_error(400, "INVALID_CURSOR", "cursor 값이 올바르지 않습니다.") from exc
    body = await AnalysisRepository(session).status(
        platform, product_id, get_settings(), [r.review_id for r in reviews]
    )
    body["next_cursor"] = next_cursor
    return body


@router.get(
    "/analysis-jobs/{job_id}",
    tags=[docs.TAG_JOBS],
    summary="분석 작업 상태 조회",
    response_model=None,
)
async def get_analysis_job(job_id: int, session: SessionDep) -> dict:
    job = await session.get(AnalysisJob, job_id)
    if job is None:
        raise _api_error(404, "NOT_FOUND", "분석 작업을 찾을 수 없습니다.")
    return public_job(job)
