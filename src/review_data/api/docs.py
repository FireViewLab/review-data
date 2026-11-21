"""
API 문서(Swagger)용 스키마와 설명.

v1 핸들러는 JSONResponse 를 직접 돌려주므로 여기 모델이 응답을 검증하지는 않는다.
Spring·AI 팀원이 /docs 만 보고 응답 형태를 알 수 있게 하는 것이 목적이다.
실제 직렬화는 v1.py 의 _serialize_* 와 필드를 맞춰야 한다.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field

# ── 태그 ──────────────────────────────────
# /docs 에서 보이는 순서가 곧 이 목록의 순서다. Spring 이 실제로 쓰는 것을 위에 둔다.
TAG_PRODUCTS = "상품·리뷰"
TAG_JOBS = "수집 job"
TAG_SYSTEM = "시스템"
TAG_STREAM = "리뷰 스트림 (AI 연동)"
TAG_DIRECT = "직접 수집 (개발용)"

OPENAPI_TAGS = [
    {
        "name": TAG_PRODUCTS,
        "description": "Spring 이 호출하는 주 API. 저장된 데이터를 돌려주고, 오래됐거나 없으면 "
        "수집 job 을 만든다.",
    },
    {"name": TAG_JOBS, "description": "수집 job 진행 상황 조회. 진행 표시가 필요할 때만 쓴다."},
    {"name": TAG_SYSTEM, "description": "상태 확인과 등록된 수집기 목록."},
    {
        "name": TAG_STREAM,
        "description": "리뷰를 수집하면서 SSE 로 흘려보낸다. DB 에 저장하지 않는다. "
        "AI 연동 방식이 확정되면 바뀔 수 있다.",
    },
    {
        "name": TAG_DIRECT,
        "description": "DB 를 거치지 않고 쇼핑몰에서 바로 긁어온다. 수집기 동작 확인용이며 "
        "서비스 계약에 포함되지 않는다. 응답이 오래 걸릴 수 있다.",
    },
]

API_DESCRIPTION = """
쇼핑몰 상품·리뷰를 수집·저장하고 Spring 에 제공하는 Data 서버.

## 인증
`INTERNAL_TOKEN` 이 설정된 서버에서는 모든 요청에 `X-Internal-Token` 헤더가 필요하다.
오른쪽 위 **Authorize** 에 토큰을 넣으면 이 페이지의 요청에 자동으로 붙는다.
`/health` 와 문서 페이지는 토큰 없이 열린다.

## 조회 흐름
크롤링은 수 초~수 분이 걸려 한 요청 안에서 끝내지 않는다.

1. `GET /api/v1/{platform}/products/{product_id}` 호출
2. 응답 `status` 로 분기
   - `fresh` (200): 최신 데이터. 끝.
   - `stale` (200): 이전 데이터를 먼저 주고 뒤에서 다시 수집한다.
   - `queued` (202): 데이터가 없다. 수집이 끝난 뒤 같은 요청을 다시 보낸다.
3. 진행 상황이 필요하면 `GET /api/v1/jobs/{job_id}`

같은 상품에 대한 수집 job 은 하나만 생기므로 반복 호출해도 안전하다.

## 분석 결과 계약 (v0.5 / v2)
분석 결과는 `contract_version`으로 구분한다. 재분석 중에는 상품마다 두 계약의 결과가 섞여 온다.

- `v0.5`(기존 모델): `rti`·`level`(소문자 safe/warn/danger)·`reasons`를 쓴다.
  v2 필드는 null 또는 빈 배열이다.
- `v2`(v3.1 모델): `policy_index_v3`·`evidence_level_v3`(대문자 SAFE/WARN/DANGER)·
  `analysis_summary_ko`·`rules_reasons_ko`를 쓴다. `rti`·`level`은 null, `reasons`는 빈 배열이다.
- 정책 지수는 RTI나 허위 리뷰 확률이 아니다. 70 이상 SAFE, 40 이상 WARN, 40 미만 DANGER.
- 점수 null은 계산 불가이며 0점이 아니다.
  `text_score`·`behavior_score`·`network_score`는 두 계약에서 의미가 같다.
- 한 문단 설명(`analysis_summary_ko`)이 null이면 `rules_reasons_ko` 문장 목록을 대신 보여 준다.

## 오류 형식
모든 오류는 `{"error": {"code", "message", "detail"}}` 형식이다. 분기는 `code` 로 한다.
"""


# ── 응답 스키마 ──────────────────────────────
class ProductOut(BaseModel):
    platform: str = Field(examples=["kurly"])
    product_id: str = Field(examples=["1000146248"])
    name: str
    url: str
    brand: str | None = None
    manufacturer: str | None = None
    seller: str | None = None
    price: int | None = Field(None, description="원 단위")
    thumbnail_url: str | None = None
    description_text: str | None = Field(None, description="상품 소개 텍스트 (최대 20,000자)")
    description_images: list[str] = Field(
        default_factory=list, description="소개 이미지 주소 (최대 100개)"
    )
    category: str | None = None
    review_count: int | None = None
    rating: float | None = Field(None, description="5점 만점 환산")
    last_collected_at: str = Field(description="마지막 상품 수집 시각 (ISO 8601)")


class ReviewOut(BaseModel):
    review_id: str
    content: str
    rating: float | None = Field(None, description="5점 만점 환산")
    author: str | None = None
    written_at: str | None = Field(None, description="ISO 8601")
    option: str | None = Field(None, description="구매 옵션 (색상/사이즈 등)")
    images: list[str] = []
    helpful_count: int | None = None


class ReviewPage(BaseModel):
    items: list[ReviewOut]
    next_cursor: str | None = Field(
        None, description="다음 페이지 요청에 그대로 넘긴다. 마지막 페이지면 null"
    )


class JobOut(BaseModel):
    id: int
    platform: str
    product_id: str
    status: Literal["pending", "running", "succeeded", "partial", "failed"]
    product_status: Literal["pending", "succeeded", "failed", "skipped"]
    review_status: Literal["pending", "succeeded", "failed", "skipped"]
    last_error: str | None = None


class ProductResponse(BaseModel):
    status: Literal["fresh", "stale", "queued"]
    product: ProductOut | None = Field(None, description="queued 일 때는 없다")
    reviews: ReviewPage | None = Field(None, description="queued 일 때는 없다")
    job: JobOut | None = Field(None, description="stale·queued 일 때만 있다")


class ErrorBody(BaseModel):
    code: str = Field(examples=["NOT_FOUND"])
    message: str
    detail: Any = None


class ErrorResponse(BaseModel):
    error: ErrorBody


class HealthOut(BaseModel):
    status: Literal["ok"]


class PlatformsOut(BaseModel):
    available: list[str] = Field(examples=[["ably", "elevenst", "kurly"]])
    failed: list[str] = Field(description="불러오다 실패한 수집기 패키지")


class ReviewAnalysisOut(BaseModel):
    review_id: str
    rti: float | None = Field(None, description="v0.5 RTI 0~100. v2 결과와 계산 불가는 null")
    level: Literal["safe", "warn", "danger"] | None = Field(None, description="v0.5 등급")
    text_score: float | None = Field(None, description="0~100, 계산 불가는 null")
    behavior_score: float | None = Field(None, description="0~100, 계산 불가는 null")
    network_score: float | None = Field(None, description="0~100, 계산 불가는 null")
    reasons: list[str] = Field(default_factory=list, description="v0.5 사유. v2 결과는 빈 배열")
    policy_index_v3: float | None = Field(
        None, description="v2 정책 지수 0~100. RTI·확률이 아니다. 계산 불가는 null"
    )
    evidence_level_v3: Literal["SAFE", "WARN", "DANGER"] | None = Field(
        None, description="v2 등급. 70 이상 SAFE, 40 이상 WARN, 40 미만 DANGER"
    )
    analysis_summary_ko: str | None = Field(None, description="v2 한 문단 설명. 없을 수 있다")
    rules_reasons_ko: list[str] = Field(
        default_factory=list, description="v2 상세 근거 문장. 한 문단 설명이 없을 때 보여 준다"
    )
    evidence_reasons_v3: list[str] = Field(default_factory=list, description="v2 근거 코드")
    reason_source: Literal["python", "groq", "rules"] | None = Field(
        None, description="v2 설명을 만든 방식. rules면 한 문단 설명이 없다"
    )
    text_probability_v4: float | None = Field(
        None, description="v4.0 텍스트 모델의 보정된 의심 확률 0~1. 아직 채우지 않았으면 null"
    )
    text_score_v4: float | None = Field(
        None, description="v4.0 텍스트 점수 0~100(=round((1-확률)*100)), 높을수록 신뢰"
    )
    policy_index_v4: float | None = Field(
        None,
        description="v4.0 정책 지수. 등급 구간(SAFE 70~100, WARN 40~69, DANGER 0~39) 안의 값",
    )
    level_v4: Literal["SAFE", "WARN", "DANGER"] | None = Field(
        None, description="v4.0 등급(규칙 A: 텍스트 중심, 사진 유무를 쓰지 않음)"
    )
    model_version_v4: str | None = Field(None, description="v4 모델 버전")
    policy_version_v4: str | None = Field(None, description="v4 등급 규칙 버전")


class AnalysisJobOut(BaseModel):
    id: int
    platform: str
    product_id: str
    status: Literal["queued", "running", "done", "failed", "stale"]
    attempt_count: int
    ai_job_id: str | None = None
    input_review_count: int | None = None
    last_error: str | None = Field(None, description="고정 실패 문구")
    completed_at: str | None = Field(None, description="ISO 8601")
    sampled: bool
    source_review_count: int


class AnalysisResponse(BaseModel):
    status: Literal["not_analyzed", "disabled", "queued", "running", "done", "failed", "stale"]
    is_current: bool = Field(description="결과가 지금 리뷰·목표 버전과 같은 입력인지")
    contract_version: Literal["v0.5", "v2"] | None = Field(
        None, description="결과를 받은 계약. 결과가 없으면 null"
    )
    model_version: str | None = None
    policy_version: str | None = None
    target_model_version: str | None = None
    target_policy_version: str | None = None
    review_count: int = Field(description="분석한 리뷰 수")
    source_review_count: int = Field(description="저장 원본 리뷰 수")
    sampled: bool = Field(description="500건을 넘어 별점 분포 대표 표본을 분석했는지")
    job: AnalysisJobOut | None = None
    refresh_job: AnalysisJobOut | None = Field(None, description="재분석 중인 새 작업")
    results: list[ReviewAnalysisOut] = Field(description="이번 페이지 리뷰의 분석 결과")
    next_cursor: str | None = None


class CatalogAnalysisOut(BaseModel):
    status: str
    is_current: bool
    contract_version: Literal["v0.5", "v2"] | None = None
    avg_rti: float | None = Field(None, description="v0.5 평균 RTI. v2 상품은 null")
    avg_policy_index_v3: float | None = Field(None, description="v2 평균 정책 지수")
    scored_review_count: int
    review_count: int
    source_review_count: int
    sampled: bool
    model_version: str | None = None
    policy_version: str | None = None
    job_id: int | None = None


class CatalogItem(BaseModel):
    product: ProductOut
    analysis: CatalogAnalysisOut


class CatalogPage(BaseModel):
    items: list[CatalogItem]
    next_cursor: str | None = None


def error(description: str) -> dict:
    return {"model": ErrorResponse, "description": description}


# 인증을 켠 서버에서는 모든 보호 경로가 401 을 줄 수 있다.
AUTH_ERROR = {401: error("X-Internal-Token 누락 또는 불일치")}
VALIDATION_ERROR = {422: error("요청 값 검증 실패 (code: VALIDATION_ERROR)")}

_JOB_EXAMPLE = {
    "id": 42,
    "platform": "kurly",
    "product_id": "1000146248",
    "status": "pending",
    "product_status": "pending",
    "review_status": "pending",
    "last_error": None,
}
_PRODUCT_EXAMPLE = {
    "platform": "kurly",
    "product_id": "1000146248",
    "name": "상품명",
    "url": "https://www.kurly.com/goods/1000146248",
    "brand": "브랜드",
    "manufacturer": None,
    "seller": None,
    "price": 12900,
    "thumbnail_url": "https://img.example.com/thumb.jpg",
    "description_text": "상품의 특징과 사용 방법을 소개합니다.",
    "description_images": ["https://img.example.com/description.jpg"],
    "category": "간식",
    "review_count": 128,
    "rating": 4.8,
    "last_collected_at": "2026-09-27T01:20:30+00:00",
}
_REVIEWS_EXAMPLE = {
    "items": [
        {
            "review_id": "135039521",
            "content": "리뷰 내용",
            "rating": 5.0,
            "author": "작성자",
            "written_at": "2026-09-26T10:00:00+00:00",
            "option": None,
            "images": [],
            "helpful_count": 3,
        }
    ],
    "next_cursor": "WyIyMDI2LTA5LTI2VDEwOjAwOjAwKzAwOjAwIiwgIjEzNTAzOTUyMSJd",
}

PRODUCT_RESPONSES: dict = {
    200: {
        "model": ProductResponse,
        "description": "fresh 또는 stale",
        "content": {
            "application/json": {
                "examples": {
                    "fresh": {
                        "summary": "최신 데이터",
                        "value": {
                            "status": "fresh",
                            "product": _PRODUCT_EXAMPLE,
                            "reviews": _REVIEWS_EXAMPLE,
                        },
                    },
                    "stale": {
                        "summary": "이전 데이터 + 재수집 중",
                        "value": {
                            "status": "stale",
                            "product": _PRODUCT_EXAMPLE,
                            "reviews": _REVIEWS_EXAMPLE,
                            "job": _JOB_EXAMPLE,
                        },
                    },
                }
            }
        },
    },
    202: {
        "model": ProductResponse,
        "description": "queued — 저장된 데이터가 없어 수집 job 만 만들었다",
        "content": {"application/json": {"example": {"status": "queued", "job": _JOB_EXAMPLE}}},
    },
    400: error("cursor 가 손상됨 (code: INVALID_CURSOR)"),
    **AUTH_ERROR,
    **VALIDATION_ERROR,
}

JOB_RESPONSES: dict = {
    200: {"model": JobOut, "description": "job 상태"},
    404: error("없는 job (code: NOT_FOUND)"),
    **AUTH_ERROR,
    **VALIDATION_ERROR,
}

_V05_RESULT = {
    "review_id": "135039521",
    "rti": 54.0,
    "level": "warn",
    "text_score": 54.0,
    "behavior_score": None,
    "network_score": None,
    "reasons": ["TEXT_SHORT_REVIEW"],
    "policy_index_v3": None,
    "evidence_level_v3": None,
    "analysis_summary_ko": None,
    "rules_reasons_ko": [],
    "evidence_reasons_v3": [],
    "reason_source": None,
    "text_probability_v4": None,
    "text_score_v4": None,
    "policy_index_v4": None,
    "level_v4": None,
    "model_version_v4": None,
    "policy_version_v4": None,
}
_V2_RESULT = {
    **_V05_RESULT,
    "rti": None,
    "level": None,
    "reasons": [],
    "policy_index_v3": 15.8,
    "evidence_level_v3": "DANGER",
    "analysis_summary_ko": "첨부 사진이 없고 별점 5점에 본문이 짧아 주의 근거로 반영됐습니다.",
    "rules_reasons_ko": ["의미 있는 본문이 10자 미만이라 길이 감점이 크게 반영됐습니다."],
    "evidence_reasons_v3": ["TEXT_SHORT"],
    "reason_source": "python",
    "text_probability_v4": 0.031,
    "text_score_v4": 97.0,
    "policy_index_v4": 96.7,
    "level_v4": "SAFE",
    "model_version_v4": "ptext-koelectra-v4.0",
    "policy_version_v4": "text-first-v1",
}


def _analysis_example(result: dict, contract: str, model: str, policy: str) -> dict:
    return {
        "status": "done",
        "is_current": True,
        "contract_version": contract,
        "model_version": model,
        "policy_version": policy,
        "target_model_version": model,
        "target_policy_version": policy,
        "review_count": 1,
        "source_review_count": 1,
        "sampled": False,
        "job": None,
        "refresh_job": None,
        "results": [result],
        "next_cursor": None,
    }


ANALYSIS_RESPONSES: dict = {
    200: {
        "model": AnalysisResponse,
        "description": "분석 상태와 이번 페이지 리뷰의 결과",
        "content": {
            "application/json": {
                "examples": {
                    "v2": {
                        "summary": "v3.1 모델 (contract_version=v2)",
                        "value": _analysis_example(
                            _V2_RESULT,
                            "v2",
                            "ptext-koelectra-v3.1-b-seed20261115",
                            "evidence-v3-balanced-interior-v1",
                        ),
                    },
                    "v0.5": {
                        "summary": "기존 모델 (contract_version=v0.5)",
                        "value": _analysis_example(
                            _V05_RESULT, "v0.5", "ptext-koelectra-v1-2epoch-20260929", "rti-v0.1"
                        ),
                    },
                }
            }
        },
    },
    400: error("cursor 가 손상됨 (code: INVALID_CURSOR)"),
    404: error("없는 상품 (code: NOT_FOUND)"),
    **AUTH_ERROR,
    **VALIDATION_ERROR,
}

CATALOG_RESPONSES: dict = {
    200: {"model": CatalogPage, "description": "상품과 분석 요약"},
    400: error("cursor 가 손상됨 (code: INVALID_CURSOR)"),
    **AUTH_ERROR,
    **VALIDATION_ERROR,
}

ANALYSIS_JOB_RESPONSES: dict = {
    200: {"model": AnalysisJobOut, "description": "분석 작업 상태"},
    404: error("없는 분석 작업 (code: NOT_FOUND)"),
    **AUTH_ERROR,
    **VALIDATION_ERROR,
}
