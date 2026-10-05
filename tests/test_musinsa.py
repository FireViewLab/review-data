"""무신사 collector 단위 테스트(네트워크 없이 응답 변환만 검증)."""

import httpx

from review_data.collectors.musinsa.collector import (
    REVIEW_PAGE_SIZE_MAX,
    MusinsaCollector,
    _parse_review,
    _parse_search_items,
)

PRODUCT_ITEM = {
    "goodsNo": 7217502,
    "goodsName": "세미크롭 후드티",
    "goodsLinkUrl": "https://www.musinsa.com/products/7217502",
    "thumbnail": "https://image.msscdn.net/x.jpg",
    "price": 39900,
    "finalPrice": 35900,
    "brand": "yosemite",
    "brandName": "요세미티",
    "reviewCount": 19,
    "reviewScore": 98,
}
# 검색 결과 사이에 섞여 오는 배너. 상품 이름과 링크가 없다.
BANNER_ITEM = {"goodsNo": 110749, "experimentId": "banner", "contentsList": [{"id": 110749}]}
REVIEW_ITEM = {
    "no": 91,
    "content": "핏이 좋아요",
    "grade": "5",
    "createDate": "2026-09-30T10:00:00",
    "goodsOption": "M",
    "likeCount": 3,
    "userProfileInfo": {"userNickName": "작성자"},
    "images": [],
}


def test_search_skips_non_product_items():
    products = _parse_search_items([BANNER_ITEM, PRODUCT_ITEM, BANNER_ITEM], limit=20)

    assert [p.product_id for p in products] == ["7217502"]
    product = products[0]
    assert (product.name, product.price, product.brand) == ("세미크롭 후드티", 35900, "요세미티")
    assert (product.review_count, product.rating) == (19, 4.9)


def test_search_limit_counts_products_not_banners():
    items = [BANNER_ITEM] + [{**PRODUCT_ITEM, "goodsNo": n} for n in range(5)]

    assert len(_parse_search_items(items, limit=3)) == 3


def test_review_maps_fields():
    review = _parse_review(REVIEW_ITEM, "7217502")

    assert (review.review_id, review.content, review.rating) == ("91", "핏이 좋아요", 5.0)
    assert (review.option, review.helpful_count, review.author) == ("M", 3, "작성자")


def test_review_with_null_images():
    assert _parse_review({**REVIEW_ITEM, "images": None}, "7217502").images == []


def test_review_without_content_is_skipped():
    # 별점만 남긴 리뷰가 하나 있다고 상품의 리뷰 수집 전체가 실패하면 안 된다.
    assert _parse_review({**REVIEW_ITEM, "content": "  "}, "7217502") is None
    assert _parse_review({**REVIEW_ITEM, "content": None}, "7217502") is None


async def test_reviews_request_respects_page_size_limit_and_paginates():
    requested: list[tuple[int, int]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        size = int(request.url.params["pageSize"])
        requested.append((page, size))
        if size > REVIEW_PAGE_SIZE_MAX:
            return httpx.Response(400, json={"data": None})
        items = [{**REVIEW_ITEM, "no": page * 100 + i} for i in range(size)]
        return httpx.Response(200, json={"data": {"list": items, "page": {"totalPages": 5}}})

    collector = MusinsaCollector()
    collector._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    collector.polite_wait = lambda: _noop()
    try:
        reviews = await collector.get_reviews("7217502", limit=50)
    finally:
        await collector._client.aclose()

    assert len(reviews) == 50
    assert requested == [(0, 20), (1, 20), (2, 20)]


async def _noop() -> None:
    return None
