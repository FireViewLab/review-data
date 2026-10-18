"""무신사 collector 단위 테스트(네트워크 없이 응답 변환만 검증)."""

import json
from pathlib import Path

import httpx
import pytest

from review_data.collectors.musinsa.collector import (
    REVIEW_PAGE_SIZE_MAX,
    MusinsaCollector,
    _parse_review,
    _parse_search_items,
)
from review_data.core.exceptions import ParseError, ProductNotFoundError
from review_data.core.product_description import parse_description

FIXTURES = Path(__file__).parent / "fixtures" / "musinsa"

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


def _product_data(product_id="6469654"):
    html = (FIXTURES / f"{product_id}.html").read_text()
    return json.loads(html.split(">", 1)[1].rsplit("</script>", 1)[0])


async def _get_product(data, product_id="6469654"):
    html = '<script id="__NEXT_DATA__">' + json.dumps(data).replace("<", "\\u003c") + "</script>"
    collector = MusinsaCollector()
    collector._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text=html))
    )
    try:
        return await collector.get_product(product_id)
    finally:
        await collector._client.aclose()


async def test_invalid_product_page_raises_not_found():
    with pytest.raises(ProductNotFoundError, match="DISPLAY_000_0006"):
        await _get_product(_product_data("3534051"), "3534051")


async def test_normal_product_page_preserves_fields():
    product = await _get_product(_product_data())

    assert (product.product_id, product.name) == ("6469654", "로우 무드 스커트 팬츠 (블랙)")
    assert (product.brand, product.price) == ("서울어패럴", 46800)
    assert (product.review_count, product.rating) == (2, 5)
    assert product.category == "Clothing > 스커트 > 미니스커트"
    assert product.thumbnail_url.startswith("https://image.msscdn.net/images/goods_img/")


@pytest.mark.parametrize(
    "path",
    [
        ("props",),
        ("props", "pageProps"),
        ("props", "pageProps", "dehydratedState"),
        ("props", "pageProps", "dehydratedState", "queries"),
        ("props", "pageProps", "dehydratedState", "queries", 0),
        ("props", "pageProps", "dehydratedState", "queries", 0, "queryKey"),
        ("props", "pageProps", "dehydratedState", "queries", 0, "state"),
        ("props", "pageProps", "dehydratedState", "queries", 0, "state", "data"),
        ("props", "pageProps", "dehydratedState", "queries", 0, "state", "data", "data"),
    ],
)
async def test_null_product_paths_raise_parse_error(path):
    data = _product_data()
    parent = data
    for key in path[:-1]:
        parent = parent[key]
    parent[path[-1]] = None

    with pytest.raises(ParseError) as error:
        await _get_product(data)
    assert not isinstance(error.value, ProductNotFoundError)


async def test_unknown_error_page_is_not_classified_as_missing_product():
    data = _product_data("3534051")
    data["props"]["pageProps"]["meta"]["meta"]["errorCode"] = "UNKNOWN"

    with pytest.raises(ParseError) as error:
        await _get_product(data, "3534051")
    assert not isinstance(error.value, ProductNotFoundError)


async def test_meta_product_data_without_queries_is_readable():
    data = _product_data()
    props = data["props"]["pageProps"]
    props["meta"] = props["dehydratedState"]["queries"][0]["state"]["data"]
    props["dehydratedState"] = None

    assert (await _get_product(data)).price == 46800


async def test_optional_product_fields_may_be_null():
    data = _product_data()
    props = data["props"]["pageProps"]
    props["meta"] = None
    goods = props["dehydratedState"]["queries"][0]["state"]["data"]["data"]
    for key in ("goodsPrice", "goodsReview", "brandInfo"):
        goods[key] = None

    product = await _get_product(data)
    assert (product.price, product.brand, product.review_count, product.rating) == (None,) * 4


@pytest.mark.parametrize("name", [None, "", " "])
async def test_missing_product_name_raises_parse_error(name):
    data = _product_data()
    data["props"]["pageProps"]["dehydratedState"]["queries"][0]["state"]["data"]["data"][
        "goodsNm"
    ] = name

    with pytest.raises(ParseError, match="상품명"):
        await _get_product(data)


@pytest.mark.parametrize("data", [None, []])
async def test_invalid_next_data_root_raises_parse_error(data):
    with pytest.raises(ParseError, match="__NEXT_DATA__"):
        await _get_product(data)


async def test_product_description_images_from_saved_page():
    product = await _get_product(_product_data())

    assert product.description_text is None
    assert product.description_images == [
        f"https://worksby00.cafe24.com/SEOULAPE/{path}.jpg"
        for path in [
            "TOP/SA26SS03", "MODEL/SA26SS03", "INFLU/SA26SS03",
            "INFO/SA26SS0301", "DETAIL/SA26SS0301",
        ]
    ]


async def test_product_description_normalizes_text_and_image_urls():
    data = _product_data()
    goods = data["props"]["pageProps"]["dehydratedState"]["queries"][0]["state"]["data"]["data"]
    goods["goodsContents"] = (
        "<h2>상품 소개</h2><p> 부드러운\n 소재&nbsp;입니다. </p>"
        "<script>숨길 코드</script><style>숨길 스타일</style>"
        '<img src="//images.example.com/a.jpg"><img src="http://images.example.com/a.jpg">'
        '<img data-src="/detail/b.jpg" src="data:image/png;base64,x">'
        '<img src="javascript:alert(1)"><img src="ftp://example.com/a.jpg">'
    )
    product = await _get_product(data)

    assert product.description_text == "상품 소개 부드러운 소재 입니다."
    assert product.description_images == [
        "https://images.example.com/a.jpg", "https://www.musinsa.com/detail/b.jpg",
    ]


def test_description_limits_and_extra_images():
    text, images = parse_description(
        "<p>" + "가" * 20_001 + '</p><img src="//example.com/0.jpg">',
        "https://example.com/products/1",
        [f"http://example.com/{i}.jpg" for i in range(110)],
    )
    assert text == "가" * 20_000
    assert len(images) == 100
    assert images[0] == "https://example.com/0.jpg"
    assert images[-1] == "https://example.com/99.jpg"


@pytest.mark.parametrize("html", [None, "", " \n ", "<p></p>"])
def test_empty_description(html):
    assert parse_description(html, "https://example.com/products/1") == (None, [])
