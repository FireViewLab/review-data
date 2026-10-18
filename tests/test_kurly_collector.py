from datetime import datetime
from pathlib import Path

import httpx

from review_data.collectors.kurly.collector import KurlyCollector


def test_platform_name() -> None:
    collector = KurlyCollector()

    assert collector.platform == "kurly"
    assert collector.label == "마켓컬리"


def test_parse_search_review_count() -> None:
    assert KurlyCollector._parse_search_review_count(286) == 286
    assert KurlyCollector._parse_search_review_count("286") == 286
    assert KurlyCollector._parse_search_review_count("1,234") == 1234
    assert KurlyCollector._parse_search_review_count("9,999+") is None
    assert KurlyCollector._parse_search_review_count(None) is None


def test_parse_search_product() -> None:
    collector = KurlyCollector()

    raw_product = {
        "no": 5051972,
        "name": " [애슐리] 오리지널 통살치킨 ",
        "salesPrice": 9900,
        "discountedPrice": 7920,
        "listImageUrl": "https://example.com/product.jpg",
        "reviewCount": "286",
    }

    product = collector._parse_search_product(raw_product)

    assert product is not None
    assert product.platform == "kurly"
    assert product.product_id == "5051972"
    assert product.name == "[애슐리] 오리지널 통살치킨"
    assert product.url == "https://www.kurly.com/goods/5051972"
    assert product.price == 7920
    assert product.thumbnail_url == "https://example.com/product.jpg"
    assert product.review_count == 286


def test_parse_review() -> None:
    collector = KurlyCollector()

    raw_review = {
        "no": 136060804,
        "contents": " 맛있는 리뷰입니다. ",
        "ownerName": "김**",
        "registeredAt": "2026-07-25T22:00:36",
        "likeCount": 3,
        "images": [
            {
                "image": "https://example.com/review-1.jpg",
            },
            {
                "image": "https://example.com/review-2.jpg",
            },
        ],
    }

    review = collector._parse_review(
        product_id="1001196970",
        raw_review=raw_review,
    )

    assert review is not None
    assert review.platform == "kurly"
    assert review.product_id == "1001196970"
    assert review.review_id == "136060804"
    assert review.content == "맛있는 리뷰입니다."
    assert review.author == "김**"
    assert review.written_at == datetime(2026, 7, 25, 22, 0, 36)
    assert review.helpful_count == 3
    assert review.images == [
        "https://example.com/review-1.jpg",
        "https://example.com/review-2.jpg",
    ]


async def test_empty_search_returns_empty_list() -> None:
    collector = KurlyCollector()

    products = await collector.search_products(
        keyword="   ",
        limit=10,
    )

    assert products == []


async def test_zero_review_limit_returns_empty_list() -> None:
    collector = KurlyCollector()

    reviews = await collector.get_reviews(
        product_id="1001196970",
        limit=0,
    )

    assert reviews == []


async def test_product_description_from_saved_page() -> None:
    html = (Path(__file__).parent / "fixtures" / "kurly" / "5139272.html").read_text()
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, text=html)

    collector = KurlyCollector()
    collector._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        product = await collector.get_product("5139272")
    finally:
        await collector._client.aclose()

    assert "무항생제 인증 우유" in product.description_text
    assert "0~10ºC에서 냉장 보관하세요." in product.description_text
    assert "<" not in product.description_text
    assert len(product.description_images) == 4
    assert product.description_images[-1].endswith("gv30000469878_1.jpg")
    assert all(url.startswith("https://") for url in product.description_images)
    assert len(requests) == 1


async def test_product_without_description_defaults_to_empty() -> None:
    collector = KurlyCollector()
    collector._client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                text='<script id="__NEXT_DATA__">'
                '{"props":{"pageProps":{"product":{"no":1,"name":"상품","productDetail":null}}}}'
                "</script>",
            )
        )
    )
    try:
        product = await collector.get_product("1")
    finally:
        await collector._client.aclose()
    assert product.description_text is None
    assert product.description_images == []
