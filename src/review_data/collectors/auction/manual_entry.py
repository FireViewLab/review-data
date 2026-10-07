"""
옥션 수동 데이터 입력 도구.

옥션은 Cloudflare 봇 감지로 자동 수집이 막혀 있습니다(collector.py 상단 주석 참고).
그래서 브라우저로 직접 옥션에 들어가 보면서, 필요한 상품/리뷰 정보를 손으로
입력하면 표준 스키마(Product/Review)로 검증한 뒤 다른 collector 와 동일하게
DB(products/reviews 테이블)에 저장해주는 스크립트입니다.

    python -m review_data.collectors.auction.manual_entry products
    python -m review_data.collectors.auction.manual_entry reviews
"""

import asyncio
import sys

import questionary

from review_data.core.db.base import session_scope
from review_data.core.db.repository import ProductRepository, ReviewRepository
from review_data.core.models import Product, Review

PLATFORM = "auction"


def _ask(label: str, required: bool = False) -> str | None:
    while True:
        value = questionary.text(label).ask()
        if value is None:
            raise KeyboardInterrupt
        value = value.strip()
        if value:
            return value
        if not required:
            return None
        print("필수 입력값입니다.")


def _ask_int(label: str) -> int | None:
    value = _ask(label)
    if value is None:
        return None
    digits = "".join(ch for ch in value if ch.isdigit())
    return int(digits) if digits else None


def _ask_float(label: str) -> float | None:
    value = _ask(label)
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        print("숫자가 아닙니다. 건너뜁니다.")
        return None


def collect_products() -> list[Product]:
    products: list[Product] = []
    print("\n상품 정보를 입력하세요. product_id 를 비워두면 종료합니다.\n")
    while True:
        product_id = _ask("product_id (URL 의 itemno 값, 예: B277448994)")
        if not product_id:
            break
        name = _ask("상품명", required=True)
        url = _ask("상품 URL", required=True)
        products.append(
            Product(
                platform=PLATFORM,
                product_id=product_id,
                name=name,
                url=url,
                brand=_ask("브랜드 (선택)"),
                seller=_ask("판매자 (선택)"),
                price=_ask_int("가격 (선택, 숫자만)"),
                thumbnail_url=_ask("썸네일 URL (선택)"),
                category=_ask("카테고리 (선택)"),
                review_count=_ask_int("리뷰 개수 (선택, 숫자만)"),
                rating=_ask_float("평점 (선택, 예: 4.6)"),
            )
        )
        print(f"  -> {name} 추가됨 (누적 {len(products)}건)\n")
    return products


def collect_reviews() -> list[Review]:
    reviews: list[Review] = []
    print("\n리뷰를 입력하세요. review_id 를 비워두면 종료합니다.\n")
    product_id = _ask("리뷰가 달린 product_id", required=True)
    while True:
        review_id = _ask("review_id (아무 고유값이어도 됩니다)")
        if not review_id:
            break
        content = _ask("리뷰 내용", required=True)
        images_raw = _ask("이미지 URL (쉼표로 구분, 선택)")
        images = [u.strip() for u in images_raw.split(",")] if images_raw else []
        reviews.append(
            Review(
                platform=PLATFORM,
                product_id=product_id,
                review_id=review_id,
                content=content,
                rating=_ask_float("평점 (선택, 예: 5)"),
                author=_ask("작성자 (선택)"),
                option=_ask("구매 옵션 (선택)"),
                images=images,
                helpful_count=_ask_int("도움돼요 수 (선택)"),
            )
        )
        print(f"  -> 리뷰 추가됨 (누적 {len(reviews)}건)\n")
    return reviews


async def _save_products(products: list[Product]) -> None:
    async with session_scope() as session:
        repo = ProductRepository(session)
        for product in products:
            await repo.upsert(product)


async def _save_reviews(product_id: str, reviews: list[Review]) -> None:
    async with session_scope() as session:
        product_repo = ProductRepository(session)
        existing = await product_repo.get(PLATFORM, product_id)
        if existing is None:
            # reviews 는 products 를 FK 로 참조하므로, 없으면 최소 정보로 먼저 만든다.
            print(f"\n'{product_id}' 상품이 아직 DB에 없습니다. 최소 정보를 입력하세요.")
            name = _ask("상품명", required=True)
            url = _ask("상품 URL", required=True)
            await product_repo.upsert(
                Product(platform=PLATFORM, product_id=product_id, name=name, url=url)
            )
        await ReviewRepository(session).upsert_many(PLATFORM, product_id, reviews)
        await product_repo.mark_reviews_collected(PLATFORM, product_id)
        from review_data.core.db.analysis_repository import AnalysisRepository
        from review_data.core.settings import get_settings

        await AnalysisRepository(session).enqueue(PLATFORM, product_id, get_settings())


def main() -> None:
    kind = sys.argv[1] if len(sys.argv) > 1 else None
    if kind not in ("products", "reviews"):
        print("사용법: python -m review_data.collectors.auction.manual_entry <products|reviews>")
        raise SystemExit(1)

    try:
        items = collect_products() if kind == "products" else collect_reviews()
    except KeyboardInterrupt:
        print("\n입력이 취소되었습니다.")
        raise SystemExit(1) from None

    if not items:
        print("입력된 항목이 없습니다. 저장하지 않습니다.")
        return

    if kind == "products":
        asyncio.run(_save_products(items))
    else:
        asyncio.run(_save_reviews(items[0].product_id, items))
    print(f"\n{len(items)}건 DB 저장 완료")


if __name__ == "__main__":
    main()
