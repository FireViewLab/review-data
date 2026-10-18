"""
수집 데이터 표준 스키마.

⚠️ 이 파일은 팀 전체의 계약(contract)입니다.
   모든 collector 가 이 스키마에 의존하므로,
   필드 추가/변경은 반드시 팀 논의 후 PR 로 진행하세요.

플랫폼마다 제공하는 정보가 다르므로, 필수 필드는 최소한으로 두고
나머지는 전부 Optional 입니다. 없으면 None 으로 두면 됩니다.
"""

from datetime import datetime

from pydantic import BaseModel, Field


class Product(BaseModel):
    """플랫폼 원본 기준 상품 정보 (통합 전 단계)."""

    # ── 필수 ──────────────────────────────
    platform: str = Field(description="플랫폼 식별자 (collector 폴더명과 동일)")
    product_id: str = Field(description="해당 플랫폼 내 원본 상품 ID")
    # 빈 문자열을 막는다. 없는 상품에도 200 을 주는 플랫폼이 있어서, 파싱 실패가
    # 이름 없는 상품으로 저장되고 수집 성공으로 기록되는 일이 실제로 있었다.
    name: str = Field(min_length=1)
    url: str = Field(min_length=1)

    # ── 동일 상품 매칭 기준 후보 ────────────
    # 여러 플랫폼의 같은 상품을 하나로 묶을 때 사용합니다.
    brand: str | None = None
    manufacturer: str | None = Field(default=None, description="제조사")
    seller: str | None = Field(default=None, description="판매자/유통사")

    # ── 부가 정보 ──────────────────────────
    price: int | None = None
    thumbnail_url: str | None = None
    description_text: str | None = None
    description_images: list[str] = Field(default_factory=list)
    category: str | None = None
    review_count: int | None = None
    rating: float | None = Field(default=None, description="평균 평점 (5점 만점 환산)")

    collected_at: datetime = Field(default_factory=datetime.now)


class Review(BaseModel):
    """플랫폼 리뷰 (Re:view 프로토콜 표준 형식 - 추가 의논 필요합니다)."""

    # ── 필수 ──────────────────────────────
    platform: str
    product_id: str = Field(description="이 리뷰가 달린 상품의 platform 내 ID")
    review_id: str = Field(min_length=1)
    content: str = Field(min_length=1)

    # ── 부가 정보 ──────────────────────────
    rating: float | None = Field(default=None, description="5점 만점 환산")
    author: str | None = None
    written_at: datetime | None = None
    option: str | None = Field(default=None, description="구매 옵션 (색상/사이즈 등)")
    images: list[str] = Field(default_factory=list)
    helpful_count: int | None = Field(default=None, description="도움돼요 수")

    collected_at: datetime = Field(default_factory=datetime.now)
