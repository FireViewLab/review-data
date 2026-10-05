"""
무신사(musinsa.com) collector.

검색/상세 페이지는 Next.js SSR 로 렌더링되며, 응답 HTML 의 `__NEXT_DATA__`
스크립트 안에 react-query 의 dehydratedState 로 상품 데이터가 그대로 내려옵니다.
Cloudflare 챌린지가 없어 httpx 로 바로 받을 수 있어 BaseCollector 만으로 충분합니다.

리뷰는 상세 페이지가 호출하는 공개 API(goods.musinsa.com)를 그대로 사용합니다.
"""

import json
import re
from datetime import datetime
from typing import Any

from review_data.core.base import BaseCollector
from review_data.core.exceptions import ParseError
from review_data.core.models import Product, Review

IMAGE_BASE = "https://image.msscdn.net"
SEARCH_URL = "https://www.musinsa.com/search/goods"
PRODUCT_URL = "https://www.musinsa.com/products/{product_id}"
# 리뷰 API 는 pageSize 가 20 을 넘으면 400 을 준다 (2026-10 확인).
REVIEW_PAGE_SIZE_MAX = 20
REVIEW_API = "https://goods.musinsa.com/api2/review/v1/view/list"

_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


def _extract_next_data(html: str) -> dict[str, Any]:
    match = _NEXT_DATA_RE.search(html)
    if match is None:
        raise ParseError("musinsa: __NEXT_DATA__ 를 찾을 수 없습니다. (페이지 구조 변경 가능성)")
    return json.loads(match.group(1))


def _find_query_data(queries: list[dict[str, Any]], predicate) -> Any | None:
    for query in queries:
        if predicate(query.get("queryKey", [])):
            return query.get("state", {}).get("data")
    return None


def _is_goods_list_query(key: list[Any]) -> bool:
    return len(key) > 2 and key[0] == "search" and key[1] == "goods" and isinstance(key[2], dict)


def _image_url(path: str | None) -> str | None:
    if not path:
        return None
    return path if path.startswith("http") else f"{IMAGE_BASE}{path}"


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _parse_search_items(items: list[dict[str, Any]], limit: int) -> list[Product]:
    """검색 결과 항목을 상품으로 바꾼다.

    목록에는 상품 사이에 배너 같은 항목이 섞여 온다. 그런 항목에는 상품 이름과 링크가
    없으므로 건너뛴다. 하나 때문에 검색 전체가 실패하면 안 된다.
    """
    products: list[Product] = []
    for item in items:
        if not item.get("goodsName") or not item.get("goodsLinkUrl"):
            continue
        review_score = item.get("reviewScore")
        products.append(
            Product(
                platform="musinsa",
                product_id=str(item["goodsNo"]),
                name=item["goodsName"],
                url=item["goodsLinkUrl"],
                brand=item.get("brandName") or item.get("brand"),
                price=item.get("finalPrice") or item.get("price"),
                thumbnail_url=item.get("thumbnail"),
                review_count=item.get("reviewCount"),
                rating=round(review_score / 20, 1) if review_score is not None else None,
            )
        )
        if len(products) >= limit:
            break
    return products


def _parse_review(item: dict[str, Any], product_id: str) -> Review | None:
    """리뷰 한 건을 표준 모델로 바꾼다. 내용이 없는 리뷰(별점만)는 저장할 수 없어 버린다."""
    content = (item.get("content") or "").strip()
    if not content:
        return None
    grade = item.get("grade")
    profile = item.get("userProfileInfo") or {}
    return Review(
        platform="musinsa",
        product_id=product_id,
        review_id=str(item["no"]),
        content=content,
        rating=float(grade) if grade is not None else None,
        author=profile.get("userNickName"),
        written_at=_parse_datetime(item.get("createDate")),
        option=item.get("goodsOption"),
        images=[
            url for img in item.get("images", []) if (url := _image_url(img.get("imageUrl")))
        ],
        helpful_count=item.get("likeCount"),
    )


class MusinsaCollector(BaseCollector):
    platform = "musinsa"
    label = "무신사"

    async def search_products(self, keyword: str, limit: int = 20) -> list[Product]:
        response = await self.client.get(SEARCH_URL, params={"keyword": keyword, "gf": "A"})
        response.raise_for_status()

        next_data = _extract_next_data(response.text)
        queries = next_data["props"]["pageProps"]["dehydratedState"]["queries"]
        goods_data = _find_query_data(queries, _is_goods_list_query)

        items: list[dict[str, Any]] = []
        if goods_data:
            for page in goods_data.get("pages", []):
                items.extend(page.get("items", []))

        return _parse_search_items(items, limit)

    async def get_product(self, product_id: str) -> Product:
        url = PRODUCT_URL.format(product_id=product_id)
        response = await self.client.get(url)
        response.raise_for_status()

        next_data = _extract_next_data(response.text)
        queries = next_data["props"]["pageProps"]["dehydratedState"]["queries"]
        detail = _find_query_data(
            queries,
            lambda key: len(key) >= 2 and key[0] == "Detail" and key[1] == int(product_id),
        )
        if detail is None:
            raise ParseError(f"musinsa: {product_id} 상품 정보를 찾을 수 없습니다.")

        goods = detail["data"]
        price_info = goods.get("goodsPrice") or {}
        review_info = goods.get("goodsReview") or {}
        brand_info = goods.get("brandInfo") or {}

        return Product(
            platform=self.platform,
            product_id=product_id,
            name=goods.get("goodsNm", ""),
            url=url,
            brand=brand_info.get("brandName") or goods.get("brand"),
            price=price_info.get("salePrice"),
            thumbnail_url=_image_url(goods.get("thumbnailImageUrl")),
            category=goods.get("baseCategoryFullPath"),
            review_count=review_info.get("totalCount"),
            rating=review_info.get("satisfactionScore"),
        )

    async def get_reviews(self, product_id: str, limit: int = 50) -> list[Review]:
        reviews: list[Review] = []
        page_size = max(min(limit, REVIEW_PAGE_SIZE_MAX), 1)
        page = 0

        while len(reviews) < limit:
            response = await self.client.get(
                REVIEW_API,
                params={
                    "page": page,
                    "pageSize": page_size,
                    "goodsNo": product_id,
                    "sort": "up_cnt_desc",
                    "selectedSimilarNo": product_id,
                    "myFilter": "false",
                    "hasPhoto": "false",
                    "isExperience": "false",
                },
            )
            response.raise_for_status()
            payload = response.json()["data"]
            batch = payload.get("list", [])
            if not batch:
                break

            for item in batch:
                review = _parse_review(item, product_id)
                if review is not None:
                    reviews.append(review)

            page += 1
            total_pages = payload.get("page", {}).get("totalPages", page)
            if page >= total_pages:
                break
            await self.polite_wait()

        return reviews[:limit]
